import dataclasses
import json
from datetime import timedelta

import httpx
import pytest

from returns_desk import config, db, retrieval, tools
from returns_desk.tools import MAX_VERIFY_ATTEMPTS, ToolContext, execute, said_yes, tool_schemas


def call(ctx, name, **args):
    result = execute(name, args, ctx)
    return result.is_error, (result.content if result.is_error else json.loads(result.content))


def customer_says(ctx, message):
    ctx.turn += 1
    ctx.last_user_message = message


@pytest.fixture
def verified(ctx, a1043_email):
    assert not call(ctx, "verify_customer", order_id="A1043", email=a1043_email)[0]
    return ctx


def refunds(ctx):
    return ctx.conn.execute("SELECT order_id, amount_pence FROM refunds").fetchall()


# --- verification ------------------------------------------------------------

def test_verify_then_get_order(ctx, a1043_email):
    error, out = call(ctx, "verify_customer", order_id=" a1043 ", email=a1043_email.upper())
    assert not error and "A1043" in out["order_ids"]

    error, order = call(ctx, "get_order", order_id="A1043")
    assert not error
    assert order["order_total"] == "£120.00"
    assert order["items"][0]["name"] == "Noise-cancelling headphones"


def test_get_order_needs_verification(ctx):
    error, message = call(ctx, "get_order", order_id="A1043")
    assert error and "verify_customer" in message


def test_get_order_refuses_someone_elses_order(verified, conn):
    other = conn.execute(
        "SELECT id FROM orders WHERE customer_id != ? LIMIT 1", (verified.verified_customer_id,)
    ).fetchone()[0]
    error, message = call(verified, "get_order", order_id=other)
    assert error and "isn't on the verified customer's account" in message


def test_wrong_email_and_missing_order_look_the_same(ctx):
    _, wrong_email = call(ctx, "verify_customer", order_id="A1043", email="x@example.com")
    _, missing = call(ctx, "verify_customer", order_id="A9999", email="x@example.com")
    assert wrong_email == missing
    assert ctx.verified_customer_id is None


def test_verification_locks_after_repeated_failures(ctx, a1043_email):
    for _ in range(MAX_VERIFY_ATTEMPTS):
        call(ctx, "verify_customer", order_id="A1043", email="guess@example.com")
    error, message = call(ctx, "verify_customer", order_id="A1043", email=a1043_email)
    assert error and "Too many" in message


def test_new_conversation_does_not_reset_the_lock(ctx, conn, a1043_email):
    for _ in range(MAX_VERIFY_ATTEMPTS):
        call(ctx, "verify_customer", order_id="A1043", email="guess@example.com")
    fresh = ToolContext(conn=conn, today=ctx.today)
    error, message = call(fresh, "verify_customer", order_id="A1043", email=a1043_email)
    assert error and "Too many" in message

    # Other orders aren't affected.
    other = ToolContext(conn=conn, today=ctx.today)
    row = conn.execute("SELECT o.id, c.email FROM orders o JOIN customers c ON c.id = o.customer_id"
                       " WHERE o.id = 'A1017'").fetchone()
    assert not call(other, "verify_customer", order_id=row["id"], email=row["email"])[0]


# --- confirmation gate -------------------------------------------------------

def test_refund_needs_a_yes_on_the_next_turn(verified):
    customer_says(verified, "Can I have £50 back please?")
    _, first = call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")
    assert first["status"] == "needs_confirmation"
    assert refunds(verified) == []

    customer_says(verified, "Yes please")
    _, second = call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="arrived broken")
    assert second["status"] == "refund_issued"
    assert [tuple(r) for r in refunds(verified)] == [("A1043", 5000)]


def test_calling_twice_in_one_turn_does_not_confirm(verified):
    customer_says(verified, "yes refund me")
    call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")
    _, again = call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")
    assert again["status"] == "needs_confirmation"
    assert refunds(verified) == []


@pytest.mark.parametrize("message", ["yes", "Yes please", "yeah, go ahead!", "Yep.", "confirm", "Go ahead"])
def test_plain_yes_counts(message):
    assert said_yes(message)


@pytest.mark.parametrize("message", [
    "Yeah, no", "Yeah nah", "Y'know what, no", "Confirm what exactly?", "yes?", "Yes but don't refund yet",
    "yes, wait", "Yes, cancel that", "y", "I suppose so", "no",
])
def test_hedged_or_negative_replies_do_not_count(message):
    assert not said_yes(message)


@pytest.mark.parametrize("reply", ["no", "hmm, what about £60?", "not yet", "Yeah, no", "Confirm what exactly?"])
def test_anything_but_yes_does_not_confirm(verified, reply):
    customer_says(verified, "refund please")
    call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")
    customer_says(verified, reply)
    call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")
    assert refunds(verified) == []


def test_yes_to_one_amount_does_not_confirm_another(verified):
    customer_says(verified, "refund please")
    call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")
    customer_says(verified, "yes")
    _, out = call(verified, "issue_refund", order_id="A1043", amount_pence=7000, reason="broken")
    assert out["status"] == "needs_confirmation"
    assert refunds(verified) == []


# --- refund limits -----------------------------------------------------------

def test_refund_over_limit_is_refused(verified):
    error, message = call(verified, "issue_refund", order_id="A1043", amount_pence=12000, reason="broken")
    assert error and "escalate_to_human" in message


def test_refunds_cannot_be_split_to_dodge_the_limit(verified):
    customer_says(verified, "refund £60")
    call(verified, "issue_refund", order_id="A1043", amount_pence=6000, reason="broken")
    customer_says(verified, "yes")
    call(verified, "issue_refund", order_id="A1043", amount_pence=6000, reason="broken")

    error, message = call(verified, "issue_refund", order_id="A1043", amount_pence=6000, reason="rest of it")
    assert error and "£75" in message
    assert len(refunds(verified)) == 1


@pytest.mark.parametrize("status", ["processing", "shipped", "cancelled"])
def test_refund_only_on_delivered_orders(ctx, conn, status):
    row = conn.execute(
        "SELECT o.id, c.email FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status = ? LIMIT 1",
        (status,),
    ).fetchone()
    call(ctx, "verify_customer", order_id=row["id"], email=row["email"])

    error, message = call(ctx, "issue_refund", order_id=row["id"], amount_pence=1000, reason="x")

    assert error and status in message and "escalate_to_human" in message
    assert refunds(ctx) == []


def test_refund_is_not_written_while_another_writer_holds_the_lock(verified, db_path):
    verified.conn.execute("PRAGMA busy_timeout = 100")
    customer_says(verified, "refund £50")
    call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="broken")

    other = db.connect(db_path)
    other.execute("BEGIN IMMEDIATE")
    try:
        customer_says(verified, "yes")
        result = execute("issue_refund", {"order_id": "A1043", "amount_pence": 5000, "reason": "broken"}, verified)
    finally:
        other.rollback()
        other.close()

    assert result.is_error and "Internal error" in result.content
    assert refunds(verified) == []


def test_no_refund_while_a_return_is_in_progress(verified, conn):
    conn.execute("INSERT INTO returns (order_id, item_id, reason, created_at) VALUES ('A1043', ?, 'x', '2026-10-19')",
                 (a1043_item_id(conn),))
    conn.commit()
    customer_says(verified, "just refund me now")
    error, message = call(verified, "issue_refund", order_id="A1043", amount_pence=5000, reason="returning it")
    assert error and "5.1" in message
    assert refunds(verified) == []


@pytest.mark.parametrize("amount", [0, -500])
def test_refund_must_be_positive(verified, amount):
    assert call(verified, "issue_refund", order_id="A1043", amount_pence=amount, reason="x")[0]


# --- returns -----------------------------------------------------------------

def a1043_item_id(conn):
    return conn.execute("SELECT id FROM order_items WHERE order_id = 'A1043'").fetchone()[0]


def test_return_within_window_after_yes(verified, conn):
    item = a1043_item_id(conn)
    customer_says(verified, "I'd like to return the headphones")
    call(verified, "start_return", order_id="A1043", item_id=item, reason="changed mind")
    customer_says(verified, "yes")
    _, out = call(verified, "start_return", order_id="A1043", item_id=item, reason="changed mind")

    assert out["status"] == "return_started"
    assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 1
    assert call(verified, "start_return", order_id="A1043", item_id=item, reason="again")[0]


def test_return_after_30_days_is_refused(verified, conn):
    verified.today += timedelta(days=21)  # A1043 was delivered 10 days before TODAY
    error, message = call(verified, "start_return", order_id="A1043", item_id=a1043_item_id(conn), reason="x")
    assert error and "30 days" in message


def test_final_sale_item_cannot_be_returned(ctx, conn):
    row = conn.execute(
        "SELECT o.id, i.id AS item_id, c.email FROM order_items i JOIN orders o ON o.id = i.order_id"
        " JOIN customers c ON c.id = o.customer_id WHERE i.final_sale = 1 AND o.status = 'delivered' LIMIT 1"
    ).fetchone()
    call(ctx, "verify_customer", order_id=row["id"], email=row["email"])
    error, message = call(ctx, "start_return", order_id=row["id"], item_id=row["item_id"], reason="x")
    assert error and "final-sale" in message


def test_item_must_be_on_the_order(verified):
    assert call(verified, "start_return", order_id="A1043", item_id=999999, reason="x")[0]


# --- policy and escalation ---------------------------------------------------

def test_search_policy_returns_cited_sections(ctx, monkeypatch):
    monkeypatch.setattr(config, "POLICY_MATCH_THRESHOLD", 0.1)
    _, out = call(ctx, "search_policy", query="can I return opened headphones and earbuds")
    top = out["matches"][0]
    assert top["section"] == "3.2"
    assert top["heading"].endswith("3.2 Headphones and earbuds")


def test_search_policy_says_when_nothing_matches(ctx):
    _, out = call(ctx, "search_policy", query="xylophone")
    assert out["matches"] == [] and "doesn't cover" in out["note"]


def test_search_policy_fails_safely_when_embeddings_are_down(ctx, monkeypatch):
    def down(texts):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(retrieval, "embed", down)
    error, message = call(ctx, "search_policy", query="refunds")
    assert error and "Don't answer from memory" in message


def test_escalation_is_recorded_without_verification(ctx, conn):
    _, out = call(ctx, "escalate_to_human", reason="Wants £120 back", order_id="A1043")
    assert out["status"] == "escalated" and ctx.escalated
    order_id, reason = conn.execute("SELECT order_id, reason FROM escalations").fetchone()
    assert order_id is None and "unverified" in reason and "A1043" in reason


def test_escalation_links_the_verified_customers_own_order(verified, conn):
    call(verified, "escalate_to_human", reason="Wants £120 back", order_id="a1043")
    assert conn.execute("SELECT order_id FROM escalations").fetchone()[0] == "A1043"


def test_escalation_with_unknown_order_still_succeeds(ctx, conn):
    assert not call(ctx, "escalate_to_human", reason="Missing parcel", order_id="Z1")[0]
    assert conn.execute("SELECT order_id FROM escalations").fetchone()[0] is None


# --- registry ----------------------------------------------------------------

def test_invalid_arguments_become_a_tool_error(ctx):
    error, message = call(ctx, "verify_customer", order_id="A1043")
    assert error and "email" in message


def test_a_crashing_tool_becomes_a_tool_error(ctx, monkeypatch):
    def crash(ctx, args):
        raise RuntimeError("disk on fire")
    monkeypatch.setitem(tools.TOOLS, "get_order", dataclasses.replace(tools.TOOLS["get_order"], run=crash))
    result = execute("get_order", {"order_id": "A1043"}, ctx)
    assert result.is_error and "Internal error in get_order" in result.content
    assert "disk on fire" not in result.content


def test_unknown_tool_is_an_error(ctx):
    assert call(ctx, "delete_everything")[0]


def test_schemas_are_strict_and_closed():
    for schema in tool_schemas():
        assert schema["strict"] is True
        assert schema["input_schema"]["additionalProperties"] is False
        assert set(schema["input_schema"]["required"]) == set(schema["input_schema"]["properties"])
