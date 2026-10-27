import pytest

from evals.compare import compare
from evals.judge import judge
from evals.label import agreement
from tests.conftest import ScriptedModel, response, text_block

TURNS = [{"customer": "hi", "agent": "hello", "steps": [{"tool_calls": []}]}]


@pytest.mark.parametrize("reply, passed, score", [
    ('{"score": 4, "reason": "fine"}', True, 4),
    ('Here you go: {"score": 3, "reason": "missed the date"}', False, 3),
    ("I think it was good.", False, None),
    # Forms qwen3:4b replied with in the first full run.
    ('{"answer": "4", "reason": "asks for a clear yes"}', True, 4),
    # Prose is never read as a score: this one meant the opposite of 5.
    ("The response contains a critical error, so it falls short of a score of 5.", False, None),
    ("3  \nThe agent failed to explain policy 3.2.", False, 3),
    ("On a scale of 1 to 5 this is hard to say.", False, None),
])
def test_judge_scores_and_threshold(reply, passed, score):
    result = judge("Says hello.", TURNS, ScriptedModel(response(text_block(reply))))
    assert (result["passed"], result["score"]) == (passed, score)


def test_judge_outage_is_a_failed_check_not_a_crash():
    class Down:
        name = "down"

        def create(self, **_):
            raise ConnectionError("no server")
    result = judge("Says hello.", TURNS, Down())
    assert not result["passed"] and "no server" in result["detail"]


def results(rate, violations=0, model="qwen3:4b", passed=(True, True)):
    return {"commit": "abc1234", "date": "2026-10-27T10:00:00", "agent_model": model, "customer_model": "m",
            "judge_model": "m", "tool_descriptions_version": "6",
            "summary": {"success_rate": rate, "guardrail_violations": violations},
            "scenarios": [{"id": f"s{i}", "runs": [{"passed": p}]} for i, p in enumerate(passed)]}


def test_compare_fails_on_a_drop_and_names_the_scenario():
    report, failures = compare(results(0.5, passed=(True, False)), results(1.0))
    assert failures == ["success rate fell from 100% to 50%"]
    assert "`s1`: **now fails**" in report


def test_compare_fails_on_any_violation_even_without_a_drop():
    assert compare(results(1.0, violations=1), results(1.0))[1] == ["1 guardrail violation(s)"]


def test_compare_doesnt_gate_a_model_change_or_a_first_run():
    assert compare(results(0.5), results(1.0, model="claude-opus-5-5"))[1] == []
    assert compare(results(0.5), None)[1] == []


def test_agreement_on_pass_fail_and_exact_scores():
    labels = [{"human": 5, "judge": 4}, {"human": 2, "judge": 4}, {"human": 3, "judge": 3}, {"human": 4, "judge": None}]
    assert agreement(labels) == {"labelled": 3, "pass_fail_agreement": 0.667, "exact_score_agreement": 0.333,
                                 "judge_minus_human_mean": 0.33}
