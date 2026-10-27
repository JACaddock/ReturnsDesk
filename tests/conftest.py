import re
import zlib
from datetime import date
from types import SimpleNamespace

import pytest

from returns_desk import db, retrieval, seed
from returns_desk.tools import ToolContext

TODAY = date(2026, 10, 20)


def fake_embed(texts):
    """Bag-of-words vectors: offline and deterministic, and close enough to test the plumbing."""
    vectors = []
    for text in texts:
        v = [0.0] * 256
        for word in re.findall(r"[a-z]+", text.lower()):
            if len(word) > 3 and word not in {"search", "document", "query"}:
                v[zlib.crc32(word.encode()) % 256] += 1
        vectors.append(v)
    return vectors


@pytest.fixture(autouse=True)
def offline_embeddings(request, monkeypatch):
    """Every test uses fake_embed except those marked `live`, which call the real Ollama model."""
    if request.node.get_closest_marker("live"):
        yield  # keep the real index cached across live tests; it takes seconds to build
        return
    monkeypatch.setattr(retrieval, "embed", fake_embed)
    retrieval._index.cache_clear()
    yield
    retrieval._index.cache_clear()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    seed.seed(conn, TODAY)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    conn = db.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def ctx(conn):
    return ToolContext(conn=conn, today=TODAY)


@pytest.fixture
def a1043_email(conn):
    return conn.execute(
        "SELECT c.email FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id = 'A1043'"
    ).fetchone()[0]


# --- a scripted stand-in for the Anthropic API --------------------------------

def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name, input, id="toolu_1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def response(*content, stop_reason=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0)
    return SimpleNamespace(content=list(content), stop_reason=stop_reason, usage=usage)


class ScriptedModel:
    """Returns the given responses in order and records what it was sent."""

    name = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        return self.responses.pop(0)
