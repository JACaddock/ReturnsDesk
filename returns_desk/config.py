"""Settings in one place. Everything here can be overridden with an env var."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DB_PATH = Path(os.environ.get("RETURNS_DESK_DB", ROOT / "data" / "returns_desk.db"))

# The agent model.
AGENT_MODEL = os.environ.get("RETURNS_DESK_AGENT_MODEL", "claude-opus-5-5")
AGENT_EFFORT = os.environ.get("RETURNS_DESK_AGENT_EFFORT", "medium")
MAX_TOKENS = 16000

# Plays the customer in the eval suite. Cheaper than the agent model, since it only needs to stay in character.
CUSTOMER_MODEL = os.environ.get("RETURNS_DESK_CUSTOMER_MODEL", "claude-haiku-5-5")
# Scores each eval conversation against the scenario's criterion. Should be at least as capable as the agent.
JUDGE_MODEL = os.environ.get("RETURNS_DESK_JUDGE_MODEL", "claude-sonnet-5-5")

# Policy retrieval. Embeddings run on a local Ollama server.
EMBED_URL = os.environ.get("RETURNS_DESK_EMBED_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("RETURNS_DESK_EMBED_MODEL", "nomic-embed-text")
# Below this cosine similarity, a policy section doesn't count as a match. Calibrated in tests/test_retrieval_live.py.
POLICY_MATCH_THRESHOLD = 0.55

# The loop stops after this many model calls in one customer turn.
MAX_STEPS = 10

# Business rules. These are enforced in tools.py, never only in the prompt.
REFUND_AUTO_APPROVE_LIMIT_PENCE = 75_00
RETURN_WINDOW_DAYS = 30
