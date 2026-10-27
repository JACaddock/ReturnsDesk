"""Retrieval quality against the real embedding model. Skipped when Ollama isn't running.

Calibration (nomic-embed-text, policy v1 with 33 subsections): the right section was the top match for all
14 questions below, with top scores from 0.58 to 0.81. Questions the policy doesn't cover scored 0.52 to 0.62,
so cosine similarity alone can't separate them. The threshold is set low enough to keep every real match;
deciding that a returned section doesn't actually answer the question is left to the model, and measured
by the "not covered by policy" eval scenarios.
"""

import httpx
import pytest

from returns_desk import config, retrieval

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True, scope="module")
def require_ollama():
    try:
        retrieval.embed(["ping"])
    except httpx.HTTPError:
        pytest.skip(f"No embedding server at {config.EMBED_URL}")


@pytest.mark.parametrize("query, section", [
    ("can I send back opened earbuds", "3.2"),
    ("my item arrived broken", "4.1"),
    ("how long do refunds take to arrive", "5.1"),
    ("the parcel never turned up", "7.3"),
    ("I want to cancel my order", "8.1"),
    ("do I have to pay for return postage", "6.2"),
    ("there's a scratch on my headphones, can I keep it for some money back", "4.4"),
    ("it's cheaper now than when I bought it, can I get the difference", "9.1"),
    ("can I return something I bought in the clearance sale", "3.1"),
    ("it stopped working after 3 months", "4.2"),
    ("I want to return a gift I was given", "2.5"),
    ("can I return just one item from my order", "2.4"),
    ("I want to make a complaint", "10.2"),
    ("tracking says delivered but I don't have it", "7.4"),
])
def test_right_section_is_in_the_top_three(query, section):
    results = retrieval.search(query)
    assert section in [chunk.section for _, chunk in results]
    assert results[0][0] >= config.POLICY_MATCH_THRESHOLD


def test_clearly_unrelated_question_matches_nothing():
    assert retrieval.search("are you hiring")[0][0] < config.POLICY_MATCH_THRESHOLD
