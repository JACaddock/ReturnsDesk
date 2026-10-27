import re

from returns_desk import retrieval
from returns_desk.retrieval import chunk_policy


def test_every_subsection_becomes_one_cited_chunk():
    chunks = chunk_policy(retrieval.POLICY_PATH.read_text(encoding="utf-8"))
    assert len(chunks) == 33
    assert all(re.fullmatch(r"\d+\.\d+", c.section) for c in chunks)
    assert len({c.section for c in chunks}) == len(chunks)
    assert all(c.text for c in chunks)


def test_chunk_carries_its_parent_heading():
    chunks = chunk_policy("## 3. Things\n\n### 3.1 First\n\nLine one\nline two\n\n### 3.2 Second\n\nMore\n")
    assert chunks[0] == retrieval.Chunk("3.1", "3. Things > 3.1 First", "Line one line two")
    assert chunks[1].section == "3.2"


def test_search_ranks_best_first():
    results = retrieval.search("headphones earbuds hygiene opened")
    assert results[0][1].section == "3.2"
    assert results[0][0] >= results[1][0] >= results[2][0]
