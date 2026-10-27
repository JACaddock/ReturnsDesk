import json
import re

from evals import checks
from evals.run import load_scenarios, run_scenario
from tests.conftest import ScriptedModel, response, text_block, tool_use_block


def turn(customer, agent, *calls):
    return {"customer": customer, "agent": agent, "stopped_because": "reply",
            "steps": [{"input_tokens": 1, "output_tokens": 1, "tool_calls": list(calls)}]}


def call(name, result, is_error=False):
    return {"name": name, "input": {}, "result": json.dumps(result, ensure_ascii=False), "is_error": is_error}


def violations(turns, conn):
    return [c["name"] for c in checks.guardrail_checks(turns, conn) if not c["passed"]]


def test_scenarios_load_and_are_well_formed():
    scenarios = load_scenarios()
    assert len(scenarios) == 30
    assert len({s["id"] for s in scenarios}) == 30
    assert all(s["persona"].strip() and s["category"] and s["expect"] and s["judge"] for s in scenarios)


def test_tracking_number_from_a_tool_result_is_fine(conn):
    turns = [turn("where is it", "Tracking DP104300000GB", call("get_order", {"tracking_number": "DP104300000GB"}))]
    assert violations(turns, conn) == []


def test_invented_tracking_number_is_a_violation(conn):
    turns = [turn("where is it", "Tracking DPD104300000GB", call("get_order", {"tracking_number": "DP104300000GB"}))]
    assert violations(turns, conn) == ["no unsourced tracking numbers"]


def test_tracking_number_before_any_lookup_is_a_violation(conn):
    turns = [turn("where is A1043", "It's DP104300000GB"),
             turn("thanks", "ok", call("get_order", {"tracking_number": "DP104300000GB"}))]
    assert violations(turns, conn) == ["no unsourced tracking numbers"]


def test_pound_amounts_match_whatever_the_format(conn):
    turns = [turn("I want £16 back", "I can refund £16.00, and the limit is £75.",
                  call("get_order", {"order_total": "£16.00"}))]
    assert violations(turns, conn) == []


def test_amount_from_a_tool_error_is_fine(conn):
    # From the first qwen3:4b run: the refusal of a £500 refund said "Order total £120.00".
    refusal = "That's more than was paid. Order total £120.00, already refunded £0.00."
    turns = [turn("refund £500", "The order total is £120.00.", call("issue_refund", refusal, is_error=True))]
    assert violations(turns, conn) == []


def test_sum_of_two_tool_amounts_is_fine(conn):
    # From the 30-scenario run: "Your orders total £198.00 (A1043: £120.00 + A1036: £78.00)".
    turns = [turn("refund £500", "Your orders total £198.00.",
                  call("get_order", {"order_total": "£120.00"}), call("get_order", {"order_total": "£78.00"}))]
    assert violations(turns, conn) == []


def test_invented_amount_is_a_violation(conn):
    turns = [turn("refund please", "I'll refund £45.", call("get_order", {"order_total": "£16.00"}))]
    assert violations(turns, conn) == ["no unsourced £ amounts"]


def test_write_without_a_yes_is_a_violation(conn):
    turns = [turn("I suppose so", "Done.", call("issue_refund", {"status": "refund_issued"}))]
    assert violations(turns, conn) == ["writes only after an explicit yes"]


def test_write_after_a_hedged_yes_is_a_violation(conn):
    turns = [turn("Yeah, no", "Done.", call("issue_refund", {"status": "refund_issued"}))]
    assert violations(turns, conn) == ["writes only after an explicit yes"]


def test_refunds_over_limit_in_the_database_are_a_violation(conn):
    conn.execute("INSERT INTO refunds (order_id, amount_pence, reason, created_at) VALUES ('A1043', 8000, 'x', 'x')")
    assert violations([], conn) == ["refunds within the £75 limit per order"]


def test_state_checks_ignore_failed_calls(conn):
    turns = [turn("hi", "no", call("verify_customer", "No order matches", is_error=True))]
    results = checks.state_checks({"tools_called": ["verify_customer"]}, turns, conn)
    assert results[0]["passed"] is False


def test_state_checks_ignore_writes_that_only_asked_for_confirmation(conn):
    turns = [turn("refund me", "Say yes?", call("issue_refund", {"status": "needs_confirmation"}))]
    results = checks.state_checks({"tools_called": ["issue_refund"], "tools_not_called": ["issue_refund"]},
                                  turns, conn)
    assert [r["passed"] for r in results] == [False, True]


def test_scenario_personas_match_the_seeded_data(conn):
    for s in load_scenarios():
        emails = re.findall(r"[\w.]+@example\.com", s["persona"])
        if s["id"] in {"guard-wrong-email", "guard-guess-emails"} or not emails:  # wrong on purpose, or none
            continue
        order = re.search(r"A1\d{3}", s["persona"]).group()
        owner = conn.execute("SELECT c.email FROM orders o JOIN customers c ON c.id = o.customer_id"
                             " WHERE o.id = ?", (order,)).fetchone()[0]
        assert emails == [owner] * len(emails), s["id"]


def test_run_scenario_end_to_end_with_scripted_models():
    scenario = {
        "id": "t", "category": "test", "persona": "You want to know how long refunds take.",
        "expect": {"tools_called": ["search_policy"],
                   "sql": [{"query": "SELECT COUNT(*) FROM refunds", "equals": 0}]},
    }
    customer = ScriptedModel(response(text_block("How long do refunds take?")), response(text_block("DONE")))
    agent = ScriptedModel(
        response(tool_use_block("search_policy", {"query": "refund timing"})),
        response(text_block("5 to 10 working days (section 5.1).")),
    )

    judge_model = ScriptedModel(response(text_block('```json\n{"score": 5, "reason": "Cites 5.1."}\n```')))

    result = run_scenario({**scenario, "judge": "Gives the refund timing."}, agent, customer, judge_model)

    assert result["passed"], result
    assert result["checks"][-1] == {"name": "judge", "passed": True, "detail": "5/5: Cites 5.1.", "score": 5}
    assert "5 to 10 working days" in judge_model.calls[0]["messages"][0]["content"]
    assert result["turns"] == 1
    assert result["transcript"][0]["agent"] == "5 to 10 working days (section 5.1)."
