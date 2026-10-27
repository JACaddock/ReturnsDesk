"""Policy retrieval: split policy.md into subsections, embed them, and find the closest to a query.

Each `### 3.2 ...` subsection is one chunk, so a match can always be cited by its
section number. Embeddings come from a local Ollama model (free to run), and the
"vector store" is a list held in memory: with 33 chunks, a plain cosine over all
of them takes well under a millisecond, so Chroma or NumPy would add nothing.
"""

import functools
import math
from dataclasses import dataclass
from pathlib import Path

import httpx

from returns_desk import config

POLICY_PATH = Path(__file__).with_name("policy.md")

# nomic-embed-text was trained with these task prefixes and scores worse without them.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


@dataclass(frozen=True)
class Chunk:
    section: str  # "3.2"
    heading: str  # "3. Items that can't be returned > 3.2 Headphones and earbuds"
    text: str


def chunk_policy(markdown: str) -> list[Chunk]:
    chunks: list[tuple[str, str, list[str]]] = []
    parent = ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            parent = line[3:].strip()
        elif line.startswith("### "):
            heading = line[4:].strip()
            chunks.append((heading.split()[0], f"{parent} > {heading}", []))
        elif chunks and line.strip():
            chunks[-1][2].append(line.strip())
    return [Chunk(section, heading, " ".join(lines)) for section, heading, lines in chunks]


def embed(texts: list[str]) -> list[list[float]]:
    response = httpx.post(
        f"{config.EMBED_URL}/api/embed", json={"model": config.EMBED_MODEL, "input": texts}, timeout=60
    )
    response.raise_for_status()
    return response.json()["embeddings"]


@functools.cache
def _index() -> list[tuple[Chunk, list[float]]]:
    # shortcut: built once per process, so policy edits need a restart; fine until the policy changes at runtime.
    chunks = chunk_policy(POLICY_PATH.read_text(encoding="utf-8"))
    vectors = embed([f"{DOCUMENT_PREFIX}{c.heading}\n{c.text}" for c in chunks])
    return list(zip(chunks, vectors))


def search(query: str, k: int = 3) -> list[tuple[float, Chunk]]:
    """The k closest chunks to the query, best first, with their cosine similarity."""
    q = embed([QUERY_PREFIX + query])[0]
    scored = [(_cosine(q, v), chunk) for chunk, v in _index()]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)[:k]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norms if norms else 0.0
