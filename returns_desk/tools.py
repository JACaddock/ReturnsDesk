"""Tools the agent can call.

Each tool has a Pydantic input model (which doubles as the JSON schema sent to the
model) and a function that does the work. Business rules are checked here, inside
the tool, so a model that has been talked into breaking a rule still can't.

A tool signals a problem by raising ToolError. The agent loop turns that into an
`is_error` tool result, so the model sees what went wrong and can recover.

Write tools (start_return, issue_refund) run in two steps. The first call only
records what would happen and asks the model to get a "yes". The action runs on
a later call with the same arguments, and only if the customer's very next
message was a yes. The model can't skip this by claiming the customer agreed.
"""

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from returns_desk import config, retrieval
from returns_desk import tool_descriptions as desc

log = logging.getLogger(__name__)

MAX_VERIFY_ATTEMPTS = 3
YES = re.compile(r"^\W*(yes|yeah|yep|yup|confirm|confirmed|go ahead)\b", re.IGNORECASE)
# "Yeah, no", "Yes? what for", "Confirm what exactly?" start like a yes but aren't one.
NOT_YES = re.compile(r"\b(no|nah|not|don['’]?t|wait|stop|cancel)\b|\?\s*$", re.IGNORECASE)


def said_yes(message: str) -> bool:
    """True only for a plain yes: starts with one, with no negative and no question."""
    return bool(YES.match(message)) and not NOT_YES.search(message)


class ToolError(Exception):
    """A failure the model should see and can act on (bad input, rule not met)."""


@dataclass
class ToolContext:
    """What a tool can touch, and what it knows about the conversation so far. One per conversation."""

    conn: sqlite3.Connection
    # shortcut: defaults to the real clock, so seeded orders age; the evals and tests pass a fixed date.
    today: date = field(default_factory=date.today)
    verified_customer_id: int | None = None
    failed_verifications: int = 0
    escalated: bool = False
    # Set by the agent loop at the start of each customer turn.
    turn: int = 0
    last_user_message: str = ""
    # Write actions waiting for a yes: action key -> the turn they were proposed in.
    pending: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[ToolContext, Any], dict]

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _clean_schema(self.input_model.model_json_schema()),
            "strict": True,
        }


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- verify_customer ---------------------------------------------------------

class VerifyCustomerInput(_Input):
    order_id: str = Field(description=desc.VERIFY_CUSTOMER_PARAMS["order_id"])
    email: str = Field(description=desc.VERIFY_CUSTOMER_PARAMS["email"])


def verify_customer(ctx: ToolContext, args: VerifyCustomerInput) -> dict:
    order_id = _normalise_order_id(args.order_id)
    # Counted per conversation and per order per day, so starting a new conversation doesn't reset the
    # guesses. The cost: three bad guesses lock that order for the day, and the customer goes to a person.
    failures_today = ctx.conn.execute(
        "SELECT COUNT(*) FROM verification_failures WHERE order_id = ? AND created_at = ?",
        (order_id, ctx.today.isoformat()),
    ).fetchone()[0]
    if ctx.failed_verifications >= MAX_VERIFY_ATTEMPTS or failures_today >= MAX_VERIFY_ATTEMPTS:
        raise ToolError("Too many failed attempts. Don't try again; offer to pass the customer to a person.")

    row = ctx.conn.execute(
        "SELECT c.id, c.name, c.email FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id = ?",
        (order_id,),
    ).fetchone()

    # Same message whether the order is missing or the email is wrong, so the
    # tool can't be used to find out which order numbers exist.
    if row is None or row["email"].lower() != args.email.strip().lower():
        ctx.failed_verifications += 1
        ctx.conn.execute("INSERT INTO verification_failures (order_id, created_at) VALUES (?, ?)",
                         (order_id, ctx.today.isoformat()))
        ctx.conn.commit()
        raise ToolError("No order matches that order number and email address.")

    ctx.verified_customer_id = row["id"]
    orders = ctx.conn.execute(
        "SELECT id FROM orders WHERE customer_id = ? ORDER BY placed_at DESC", (row["id"],)
    ).fetchall()
    return {"verified": True, "first_name": row["name"].split()[0], "order_ids": [o["id"] for o in orders]}


# --- get_order ---------------------------------------------------------------

class GetOrderInput(_Input):
    order_id: str = Field(description=desc.GET_ORDER_PARAMS["order_id"])


def get_order(ctx: ToolContext, args: GetOrderInput) -> dict:
    order = _customer_order(ctx, args.order_id)
    items = _items(ctx, order["id"])
    return {
        "order_id": order["id"],
        "status": order["status"],
        "placed_at": order["placed_at"],
        "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
        "carrier": order["carrier"],
        "tracking_number": order["tracking_number"],
        "items": [
            {
                "item_id": item["id"],
                "name": item["name"],
                "quantity": item["quantity"],
                "unit_price": format_pence(item["price_pence"]),
                "final_sale": bool(item["final_sale"]),
            }
            for item in items
        ],
        "order_total": format_pence(_order_total(items)),
        "already_refunded": format_pence(_refunded(ctx, order["id"])),
    }


# --- search_policy -----------------------------------------------------------

class SearchPolicyInput(_Input):
    query: str = Field(description=desc.SEARCH_POLICY_PARAMS["query"])


def search_policy(ctx: ToolContext, args: SearchPolicyInput) -> dict:
    try:
        results = retrieval.search(args.query)
    except httpx.HTTPError:
        raise ToolError("Policy search isn't available right now. Don't answer from memory; "
                        "offer to pass the customer to a person.")
    matches = [
        {"section": chunk.section, "heading": chunk.heading, "text": chunk.text, "score": round(score, 2)}
        for score, chunk in results
        if score >= config.POLICY_MATCH_THRESHOLD
    ]
    if not matches:
        return {"matches": [], "note": "The policy doesn't cover this. Say so rather than guessing."}
    return {"matches": matches}


# --- start_return ------------------------------------------------------------

class StartReturnInput(_Input):
    order_id: str = Field(description=desc.START_RETURN_PARAMS["order_id"])
    item_id: int = Field(description=desc.START_RETURN_PARAMS["item_id"])
    reason: str = Field(description=desc.START_RETURN_PARAMS["reason"])


def start_return(ctx: ToolContext, args: StartReturnInput) -> dict:
    order = _customer_order(ctx, args.order_id)
    item = next((i for i in _items(ctx, order["id"]) if i["id"] == args.item_id), None)
    if item is None:
        raise ToolError(f"Item {args.item_id} isn't on order {order['id']}.")
    if order["status"] != "delivered":
        raise ToolError(f"Order {order['id']} is {order['status']}, so it can't be returned yet.")
    if ctx.today > date.fromisoformat(order["delivered_at"]) + timedelta(days=config.RETURN_WINDOW_DAYS):
        raise ToolError(f"Delivered on {order['delivered_at']}, more than {config.RETURN_WINDOW_DAYS} days ago. "
                        "Outside the return window; a faulty item needs a person to review it.")
    if item["final_sale"]:
        raise ToolError("This is a final-sale item and can't be returned through this tool. "
                        "If it's faulty, pass the customer to a person.")
    if ctx.conn.execute("SELECT 1 FROM returns WHERE item_id = ?", (item["id"],)).fetchone():
        raise ToolError("A return has already been started for this item.")

    summary = f"start a return for {item['name']} from order {order['id']}"
    if (ask := _needs_confirmation(ctx, "start_return", args, summary)) is not None:
        return ask

    ctx.conn.execute(
        "INSERT INTO returns (order_id, item_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (order["id"], item["id"], args.reason, ctx.today.isoformat()),
    )
    ctx.conn.commit()
    return {"status": "return_started", "item": item["name"], "next_step": "A prepaid label will be emailed."}


# --- issue_refund ------------------------------------------------------------

class IssueRefundInput(_Input):
    order_id: str = Field(description=desc.ISSUE_REFUND_PARAMS["order_id"])
    amount_pence: int = Field(description=desc.ISSUE_REFUND_PARAMS["amount_pence"])
    reason: str = Field(description=desc.ISSUE_REFUND_PARAMS["reason"])


def issue_refund(ctx: ToolContext, args: IssueRefundInput) -> dict:
    order = _customer_order(ctx, args.order_id)
    # A cancelled order is refunded by the cancellation, and an undelivered one may still arrive.
    if order["status"] != "delivered":
        raise ToolError(f"Order {order['id']} is {order['status']}. Only delivered orders can be refunded here; "
                        "use escalate_to_human instead.")
    if args.amount_pence <= 0:
        raise ToolError("The refund amount must be more than £0.")
    # Returned items are refunded by the warehouse when they arrive (policy 5.1); paying now would pay twice.
    if ctx.conn.execute("SELECT 1 FROM returns WHERE order_id = ?", (order["id"],)).fetchone():
        raise ToolError(f"A return is in progress on order {order['id']}. The refund is paid once the item "
                        "reaches our warehouse (policy 5.1); for anything else on this order, use escalate_to_human.")
    # Checked before asking, so the customer is never asked to confirm a refund that would be refused.
    _check_refund_limits(ctx, order["id"], args.amount_pence)

    summary = f"refund {format_pence(args.amount_pence)} on order {order['id']}"
    if (ask := _needs_confirmation(ctx, "issue_refund", args, summary)) is not None:
        return ask

    # Checked again inside a write lock, so two conversations can't both pass the limit at the same moment.
    with _immediate_transaction(ctx.conn):
        _check_refund_limits(ctx, order["id"], args.amount_pence)
        ctx.conn.execute(
            "INSERT INTO refunds (order_id, amount_pence, reason, created_at) VALUES (?, ?, ?, ?)",
            (order["id"], args.amount_pence, args.reason, ctx.today.isoformat()),
        )
    return {"status": "refund_issued", "amount": format_pence(args.amount_pence),
            "arrives": "5 to 10 working days, to the original payment method"}


# --- escalate_to_human -------------------------------------------------------

class EscalateInput(_Input):
    reason: str = Field(description=desc.ESCALATE_PARAMS["reason"])
    order_id: str | None = Field(description=desc.ESCALATE_PARAMS["order_id"])


def escalate_to_human(ctx: ToolContext, args: EscalateInput) -> dict:
    record_escalation(ctx, args.reason, args.order_id)
    return {"status": "escalated", "follow_up": "A member of the team will reply within 2 working days."}


def record_escalation(ctx: ToolContext, reason: str, order_id: str | None = None) -> None:
    order_id = _normalise_order_id(order_id) if order_id else None
    # Only link the verified customer's own orders, so a stranger can't file requests against someone else's.
    owned = order_id and ctx.verified_customer_id is not None and ctx.conn.execute(
        "SELECT 1 FROM orders WHERE id = ? AND customer_id = ?", (order_id, ctx.verified_customer_id)
    ).fetchone()
    if order_id and not owned:
        reason, order_id = f"{reason} (unverified: customer mentioned order {order_id})", None
    ctx.conn.execute(
        "INSERT INTO escalations (order_id, reason, created_at) VALUES (?, ?, ?)",
        (order_id, reason, ctx.today.isoformat()),
    )
    ctx.conn.commit()
    ctx.escalated = True


# --- shared checks -----------------------------------------------------------

def _customer_order(ctx: ToolContext, order_id: str) -> sqlite3.Row:
    """The order, if it belongs to the verified customer. The one gate in front of all order data."""
    if ctx.verified_customer_id is None:
        raise ToolError("The customer isn't verified. Call verify_customer with their order number and email first.")
    order = ctx.conn.execute(
        "SELECT * FROM orders WHERE id = ? AND customer_id = ?",
        (_normalise_order_id(order_id), ctx.verified_customer_id),
    ).fetchone()
    if order is None:
        raise ToolError("That order isn't on the verified customer's account.")
    return order


def _needs_confirmation(ctx: ToolContext, action: str, args: BaseModel, summary: str) -> dict | None:
    """None if the customer said yes to exactly this action in their last message; otherwise park it and ask."""
    # The free-text reason is left out so the model rewording it doesn't reset the confirmation.
    key = f"{action}:{args.model_dump_json(exclude={'reason'})}"
    if ctx.pending.get(key) == ctx.turn - 1 and said_yes(ctx.last_user_message):
        del ctx.pending[key]
        return None
    ctx.pending[key] = ctx.turn
    return {"status": "needs_confirmation",
            "message": f"Nothing has been done yet. Ask the customer to reply yes to confirm: {summary}. "
                       "Call this tool again with the same arguments after they reply."}


def _check_refund_limits(ctx: ToolContext, order_id: str, amount_pence: int) -> None:
    paid = _order_total(_items(ctx, order_id))
    refunded = _refunded(ctx, order_id)
    if refunded + amount_pence > paid:
        raise ToolError(f"That's more than was paid. Order total {format_pence(paid)}, "
                        f"already refunded {format_pence(refunded)}.")
    # Counted across the whole order, so two £60 refunds can't get round the limit.
    if refunded + amount_pence > config.REFUND_AUTO_APPROVE_LIMIT_PENCE:
        raise ToolError(f"Refunds over {format_pence(config.REFUND_AUTO_APPROVE_LIMIT_PENCE)} per order need a "
                        "person to approve them. Don't issue a smaller amount instead; use escalate_to_human.")


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection):
    """Take SQLite's write lock up front, so no other connection can write between our read and our insert."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _items(ctx: ToolContext, order_id: str) -> list[sqlite3.Row]:
    return ctx.conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()


def _order_total(items: list[sqlite3.Row]) -> int:
    return sum(i["price_pence"] * i["quantity"] for i in items)


def _refunded(ctx: ToolContext, order_id: str) -> int:
    return ctx.conn.execute(
        "SELECT COALESCE(SUM(amount_pence), 0) FROM refunds WHERE order_id = ?", (order_id,)
    ).fetchone()[0]


def _normalise_order_id(order_id: str) -> str:
    return order_id.strip().upper()


# --- registry ----------------------------------------------------------------

TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool("verify_customer", desc.VERIFY_CUSTOMER, VerifyCustomerInput, verify_customer),
        Tool("get_order", desc.GET_ORDER, GetOrderInput, get_order),
        Tool("search_policy", desc.SEARCH_POLICY, SearchPolicyInput, search_policy),
        Tool("start_return", desc.START_RETURN, StartReturnInput, start_return),
        Tool("issue_refund", desc.ISSUE_REFUND, IssueRefundInput, issue_refund),
        Tool("escalate_to_human", desc.ESCALATE, EscalateInput, escalate_to_human),
    ]
}


def tool_schemas() -> list[dict]:
    return [tool.to_api() for tool in TOOLS.values()]


def execute(name: str, raw_input: Any, ctx: ToolContext) -> ToolResult:
    """Validate the model's arguments, run the tool, and never raise."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(f"Unknown tool: {name}", is_error=True)
    try:
        args = tool.input_model.model_validate(raw_input)
    except ValidationError as e:
        return ToolResult(f"Invalid arguments for {name}: {e.errors(include_url=False)}", is_error=True)
    try:
        # ensure_ascii=False keeps "£" readable, for the model and for the eval checks that look for amounts.
        return ToolResult(json.dumps(tool.run(ctx, args), ensure_ascii=False))
    except ToolError as e:
        return ToolResult(str(e), is_error=True)
    except Exception:
        # Raising here would leave a tool_use with no tool_result, and the API rejects every later message.
        log.exception("Tool %s failed", name)
        return ToolResult(f"Internal error in {name}. Don't retry; offer to pass the customer to a person.",
                          is_error=True)


def format_pence(pence: int) -> str:
    return f"£{pence / 100:.2f}"


def _clean_schema(schema: dict) -> dict:
    """Drop Pydantic's auto-generated titles; the model only needs descriptions."""
    schema = {k: v for k, v in schema.items() if k != "title"}
    if "properties" in schema:
        schema["properties"] = {
            name: {k: v for k, v in prop.items() if k != "title"} for name, prop in schema["properties"].items()
        }
    return schema
