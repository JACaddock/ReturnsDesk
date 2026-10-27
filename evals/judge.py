"""The LLM judge: a model reads a finished conversation and scores it 1-5 against the scenario's criterion.

It's the least trusted of the three kinds of check, so it only covers what the others can't: whether the
answer was right and clearly put. How far to trust it is measured, not assumed: `python -m evals.label`
compares its scores with hand labels.
"""

import re

from evals.checks import check, tool_calls
from returns_desk.model import Model

# Scores at or above this count as a pass, for the scenario and for agreement with the hand labels.
PASS_SCORE = 4

RUBRIC = """\
You grade conversations between a customer and the support agent of Larkfield Goods, a UK online shop.

You get the conversation, including every tool the agent called and what the tool returned, and one \
criterion. Tool results are the truth: if the agent says something they don't support, that's a mistake.

Score the agent from 1 to 5 against the criterion:
5 = does everything the criterion asks, accurately, clearly and politely
4 = right outcome, with small problems of wording or tone
3 = mostly right, but leaves out something important or is confusing
2 = wrong or misleading on an important point
1 = ignores the customer, makes things up, or breaks a rule

Judge only the agent, not the customer. Reply with JSON and nothing else:
{"score": <1-5>, "reason": "<one sentence>"}
"""

# Tried in order: the JSON (qwen3:4b sometimes says "answer"), then a bare '3' starting the reply. Not
# "score of 5" in prose: that also matches "falls short of a score of 5", and a misread is worse than none.
SCORES = [re.compile(r'"(?:score|answer)"\s*:\s*"?([1-5])\b'),
          re.compile(r"^\W*([1-5])\b")]
REASON = re.compile(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"')


def format_transcript(turns: list[dict], result_chars: int = 600) -> str:
    """The conversation as plain text, tool calls included. Shown to the judge and to the hand labeller."""
    lines = []
    for turn in turns:
        lines.append(f"Customer: {turn['customer']}")
        for c in tool_calls([turn]):
            status = "error" if c["is_error"] else "ok"
            lines.append(f"  [tool {c['name']} {c['input']} -> {status}] {c['result'][:result_chars]}")
        lines.append(f"Agent: {turn['agent']}")
    return "\n".join(lines)


def judge(criterion: str, turns: list[dict], model: Model) -> dict:
    """A check like the others, plus the score and the judge's reason."""
    prompt = f"Criterion: {criterion}\n\nConversation:\n{format_transcript(turns)}"
    try:
        response = model.create(system=RUBRIC, messages=[{"role": "user", "content": prompt}], tools=[])
    except Exception as e:  # a judge outage is a failed check, not a crashed suite
        return {**check("judge", False, f"judge error: {type(e).__name__}: {e}"), "score": None}
    text = "".join(b.text for b in response.content if b.type == "text")
    # Parsed with regexes, not json.loads: small models wrap the JSON in prose or code fences.
    score = next((m for pattern in SCORES if (m := pattern.search(text))), None)
    reason = REASON.search(text)
    if score is None:
        return {**check("judge", False, f"judge reply had no score: {text[:200]!r}"), "score": None}
    n = int(score.group(1))
    detail = f"{n}/5: {reason.group(1) if reason else text.strip()[:200]}"
    return {**check("judge", n >= PASS_SCORE, detail), "score": n}
