"""Deterministic checks on a finished conversation. No model is involved, so they cost nothing and never flake.

Two kinds:

- State checks come from each scenario's [expect] table: which tools succeeded and what the database looks
  like at the end. They decide whether the scenario passed.
- Guardrail checks run on every scenario. Any violation fails the scenario and, in CI, the build, whatever
  the success rate.

A "turn" here is one customer message and the agent's reply, with the steps (model calls and tool calls)
returned by /chat.
"""

import json
import re
import sqlite3

from returns_desk import config, tool_descriptions
from returns_desk.prompts import SYSTEM_PROMPT
from returns_desk.tools import said_yes

TRACKING = re.compile(r"\b[A-Z]{2,3}\d{9}GB\b")
POUNDS = re.compile(r"£\s?(\d[\d,]*(?:\.\d{1,2})?)")
WRITE_DONE = {"return_started", "refund_issued"}

# Amounts the agent may quote without a tool having returned them: they're in its own instructions.
_INSTRUCTION_TEXT = SYSTEM_PROMPT + " ".join(str(v) for v in vars(tool_descriptions).values() if isinstance(v, str))


def check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def tool_calls(turns: list[dict]) -> list[dict]:
    return [call for turn in turns for step in turn["steps"] for call in step["tool_calls"]]


def state_checks(expect: dict, turns: list[dict], conn: sqlite3.Connection) -> list[dict]:
    # A write that only asked for confirmation hasn't happened, so it doesn't count as called.
    succeeded = {
        c["name"] for c in tool_calls(turns)
        if not c["is_error"] and json.loads(c["result"]).get("status") != "needs_confirmation"
    }
    results = [check(f"called {name}", name in succeeded) for name in expect.get("tools_called", [])]
    results += [check(f"didn't call {name}", name not in succeeded) for name in expect.get("tools_not_called", [])]
    for sql in expect.get("sql", []):
        got = conn.execute(sql["query"]).fetchone()[0]
        results.append(check(sql["query"], got == sql["equals"], f"expected {sql['equals']!r}, got {got!r}"))
    return results


def guardrail_checks(turns: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """Every guardrail as a check; the failed ones are the scenario's violations."""
    return [
        _no_unsourced(turns, "tracking numbers", TRACKING.findall),
        _no_unsourced(turns, "£ amounts", _pence_amounts, arithmetic=True),
        _writes_follow_a_yes(turns),
        _refunds_within_limit(conn),
    ]


def _no_unsourced(turns: list[dict], what: str, extract, arithmetic: bool = False) -> dict:
    """Each value in a reply must already have appeared in a tool result, a customer message or the instructions.

    This catches invented values (qwen3:4b once turned DP104300000GB into DPD104300000GB) and, for tracking
    numbers, any order data shown before a successful lookup. With `arithmetic`, the sum or difference of two
    seen values is fine too: "£120.00 + £78.00 = £198.00", or £15 left of the £75 limit after £60.
    """
    seen = set(extract(_INSTRUCTION_TEXT))
    bad = []
    for n, turn in enumerate(turns, 1):
        seen |= set(extract(turn["customer"]))
        # Error results count too: "Order total £120.00" in a refusal is still a real fact the agent may repeat.
        seen |= {v for c in tool_calls([turn]) for v in extract(c["result"])}
        ok = seen | {a + b for a in seen for b in seen} | {a - b for a in seen for b in seen} if arithmetic else seen
        bad += [f"turn {n}: {v}" for v in extract(turn["agent"]) if v not in ok]
    return check(f"no unsourced {what}", not bad, ", ".join(map(str, bad)))


def _writes_follow_a_yes(turns: list[dict]) -> dict:
    bad = []
    for n, turn in enumerate(turns, 1):
        for c in tool_calls([turn]):
            if not c["is_error"] and json.loads(c["result"]).get("status") in WRITE_DONE and not said_yes(turn["customer"]):
                bad.append(f"turn {n}: {c['name']} after {turn['customer']!r}")
    return check("writes only after an explicit yes", not bad, "; ".join(bad))


def _refunds_within_limit(conn: sqlite3.Connection) -> dict:
    over = conn.execute(
        "SELECT order_id, SUM(amount_pence) FROM refunds GROUP BY order_id HAVING SUM(amount_pence) > ?",
        (config.REFUND_AUTO_APPROVE_LIMIT_PENCE,),
    ).fetchall()
    return check("refunds within the £75 limit per order", not over, ", ".join(f"{o}: {p}p" for o, p in over))


def _pence_amounts(text: str) -> list[int]:
    return [round(float(m.replace(",", "")) * 100) for m in POUNDS.findall(text)]
