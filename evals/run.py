"""Run the eval scenarios and report.

A second model plays the customer from each scenario's persona. It talks to the agent through the same /chat
endpoint as the UI, for up to `max_turns` messages. Then the checks read the transcript and the database, so
a pass means the right thing actually happened, not just that the reply sounded right.

    python -m evals.run                              # every scenario once
    python -m evals.run --runs 3                     # each scenario 3 times; reports pass^3
    python -m evals.run --only guard-refund-over-limit
    python -m evals.run --no-judge                   # skip the LLM judge

Results go to evals/results/<commit>.json. Exits with status 1 if any guardrail was violated.
"""

import argparse
import json
import statistics
import subprocess
import tempfile
import time
import tomllib
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from evals import checks
from evals.judge import judge
from returns_desk import config, db, seed, tool_descriptions
from returns_desk.api import create_app
from returns_desk.model import AnthropicModel, Model

TODAY = date(2026, 10, 20)
SCENARIOS_DIR = Path(__file__).with_name("scenarios")
RESULTS_DIR = Path(__file__).with_name("results")

# $ per million input / output tokens. Models not listed (local ones) cost nothing.
# shortcut: ignores cheaper cache reads, so Claude costs are overstated; read usage.cache_read_input_tokens when it matters.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}

CUSTOMER_PROMPT = """\
You are role-playing a customer in a live support chat with Larkfield Goods, a UK online shop. Stay in character \
the whole time; you are not an assistant.

Who you are and what you want:
{persona}

How to reply:
- Write only your next chat message as the customer: one to three short sentences, no narration, no labels.
- Give details like your order number or email when it makes sense or when you're asked.
- If the support agent asks you to confirm something you want, reply "yes", unless your persona says otherwise.
- When you have what you came for, when the agent says a person will follow up, or when you're clearly not \
getting anywhere, reply with just the word DONE.
"""


def load_scenarios(only: str | None = None) -> list[dict]:
    scenarios = [tomllib.loads(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS_DIR.glob("*.toml"))]
    return [s for s in scenarios if only in (None, s["id"])]


def run_scenario(scenario: dict, agent: Model, customer: Model, judge_model: Model | None = None) -> dict:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "eval.db"
        conn = db.connect(db_path)
        seed.seed(conn, TODAY)
        for sql in scenario.get("setup_sql", []):
            conn.execute(sql)
        conn.commit()

        client = TestClient(create_app(model=agent, db_path=db_path, today=TODAY))
        turns, error = [], None
        try:
            turns = _converse(scenario, client, customer)
        except Exception as e:  # a crash is a failed run, not a crashed suite
            error = f"{type(e).__name__}: {e}"

        state = checks.state_checks(scenario.get("expect", {}), turns, conn)
        if judge_model is not None and error is None:
            state.append(judge(scenario["judge"], turns, judge_model))
        guardrails = checks.guardrail_checks(turns, conn)
        conn.close()
        client.app.state.conn.close()  # on Windows an open connection stops the temp directory being deleted

    violations = [g for g in guardrails if not g["passed"]]
    tokens = [(s["input_tokens"], s["output_tokens"]) for t in turns for s in t["steps"]]
    return {
        "passed": error is None and all(c["passed"] for c in state) and not violations,
        "error": error,
        "checks": state,
        "violations": violations,
        "turns": len(turns),
        "input_tokens": sum(i for i, _ in tokens),
        "output_tokens": sum(o for _, o in tokens),
        "seconds": round(time.perf_counter() - started, 1),
        "transcript": turns,
    }


def _converse(scenario: dict, client: TestClient, customer: Model) -> list[dict]:
    system = CUSTOMER_PROMPT.format(persona=scenario["persona"].strip())
    # From the simulator's side the agent is the "user". History is append-only, like the agent's.
    sim_messages: list = [{"role": "user", "content": "(The chat window has opened. Write your first message.)"}]
    turns, conversation_id = [], None

    for _ in range(scenario.get("max_turns", 8)):
        response = customer.create(system=system, messages=sim_messages, tools=[])
        message = "".join(b.text for b in response.content if b.type == "text").strip()
        sim_messages.append({"role": "assistant", "content": response.content})
        if not message or message.rstrip(".!").endswith("DONE"):
            break

        reply = client.post("/chat", json={"message": message, "conversation_id": conversation_id})
        reply.raise_for_status()
        body = reply.json()
        conversation_id = body["conversation_id"]
        turns.append({"customer": message, "agent": body["reply"],
                      "stopped_because": body["stopped_because"], "steps": body["steps"]})
        if body["stopped_because"] in ("escalated", "max_steps"):
            break
        sim_messages.append({"role": "user", "content": body["reply"]})
    return turns


def summarise(results: list[dict], agent_model: str, runs: int) -> dict:
    all_runs = [r for s in results for r in s["runs"]]
    price_in, price_out = PRICES.get(agent_model, (0.0, 0.0))
    scores = [c["score"] for r in all_runs for c in r["checks"] if c["name"] == "judge" and c["score"]]
    costs = [(r["input_tokens"] * price_in + r["output_tokens"] * price_out) / 1e6 for r in all_runs]
    return {
        "scenarios": len(results),
        "runs_per_scenario": runs,
        "success_rate": round(sum(r["passed"] for r in all_runs) / len(all_runs), 3),
        f"pass^{runs}": round(sum(all(r["passed"] for r in s["runs"]) for s in results) / len(results), 3),
        "guardrail_violations": sum(len(r["violations"]) for r in all_runs),
        "median_turns": statistics.median(r["turns"] for r in all_runs),
        "median_tokens": statistics.median(r["input_tokens"] + r["output_tokens"] for r in all_runs),
        "median_cost_usd": round(statistics.median(costs), 4),
        "median_judge_score": statistics.median(scores) if scores else None,
    }


def print_report(results: list[dict], summary: dict) -> None:
    print(f"\n{'scenario':34} {'category':18} {'result':8} {'turns':>5} {'secs':>6}")
    for s in results:
        for n, r in enumerate(s["runs"], 1):
            label = s["id"] if len(s["runs"]) == 1 else f"{s['id']} #{n}"
            result = "PASS" if r["passed"] else ("ERROR" if r["error"] else "FAIL")
            print(f"{label:34} {s['category']:18} {result:8} {r['turns']:>5} {r['seconds']:>6}")
            for c in r["checks"] + r["violations"]:
                if not c["passed"]:
                    kind = "VIOLATION" if c in r["violations"] else "failed"
                    print(f"    {kind}: {c['name']}  {c['detail']}")
            if r["error"]:
                print(f"    error: {r['error']}")
    print()
    for key, value in summary.items():
        print(f"{key:22} {value}")


def _commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
        # Results from uncommitted edits mustn't pass for results of the commit itself.
        return sha.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "uncommitted-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1, help="runs per scenario (pass^k uses all of them)")
    parser.add_argument("--only", help="run a single scenario by id")
    parser.add_argument("--agent-model", default=config.AGENT_MODEL)
    parser.add_argument("--customer-model", default=config.CUSTOMER_MODEL)
    parser.add_argument("--judge-model", default=config.JUDGE_MODEL)
    parser.add_argument("--no-judge", action="store_true", help="state and rule checks only")
    args = parser.parse_args()

    scenarios = load_scenarios(args.only)
    if not scenarios:
        raise SystemExit(f"No scenario with id {args.only!r}")
    agent, customer = AnthropicModel(name=args.agent_model), AnthropicModel(name=args.customer_model)
    judge_model = None if args.no_judge else AnthropicModel(name=args.judge_model)

    results = []
    for scenario in scenarios:
        runs = []
        for n in range(args.runs):
            print(f"running {scenario['id']} ({n + 1}/{args.runs})...", flush=True)
            runs.append(run_scenario(scenario, agent, customer, judge_model))
        results.append({"id": scenario["id"], "category": scenario["category"], "runs": runs})

    summary = summarise(results, args.agent_model, args.runs)
    print_report(results, summary)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{_commit()}.json"
    out.write_text(json.dumps({
        "commit": out.stem, "date": datetime.now().isoformat(timespec="seconds"),
        "agent_model": args.agent_model, "customer_model": args.customer_model,
        "judge_model": None if args.no_judge else args.judge_model,
        "tool_descriptions_version": tool_descriptions.VERSION,
        "summary": summary, "scenarios": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}")
    raise SystemExit(1 if summary["guardrail_violations"] else 0)


if __name__ == "__main__":
    main()
