"""Hand-label eval transcripts, then measure how often the LLM judge agrees with you.

    python -m evals.label evals/results/<commit>.json    # label up to 15 transcripts, blind to the judge
    python -m evals.label --report                       # agreement so far

You see each conversation and the scenario's criterion, but not the judge's score, and give your own 1-5
using the same rubric. Labels go to evals/labels.json with the judge's score beside them. Run it again to
carry on where you stopped; transcripts you've labelled are skipped.
"""

import argparse
import json
from pathlib import Path

from evals.judge import PASS_SCORE, RUBRIC, format_transcript
from evals.run import load_scenarios

LABELS = Path(__file__).with_name("labels.json")
TARGET = 15


def agreement(labels: list[dict]) -> dict:
    """How often the judge's pass/fail matches yours, and how often its score is exactly yours."""
    pairs = [(lab["human"], lab["judge"]) for lab in labels if lab["judge"] is not None]
    if not pairs:
        return {"labelled": 0}
    return {
        "labelled": len(pairs),
        "pass_fail_agreement": round(sum((h >= PASS_SCORE) == (j >= PASS_SCORE) for h, j in pairs) / len(pairs), 3),
        "exact_score_agreement": round(sum(h == j for h, j in pairs) / len(pairs), 3),
        "judge_minus_human_mean": round(sum(j - h for h, j in pairs) / len(pairs), 2),
    }


def _load() -> list[dict]:
    return json.loads(LABELS.read_text(encoding="utf-8")) if LABELS.exists() else []


def _label(results_path: Path) -> None:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    criteria = {s["id"]: s["judge"] for s in load_scenarios()}
    labels = _load()
    done = {(lab["results"], lab["scenario"], lab["run"]) for lab in labels}
    print(RUBRIC.split("Judge only")[0])

    for sc in results["scenarios"]:
        for n, run in enumerate(sc["runs"]):
            judged = next((c for c in run["checks"] if c["name"] == "judge"), None)
            key = (results_path.name, sc["id"], n)
            if judged is None or judged["score"] is None or key in done:
                continue
            if len(labels) >= TARGET:
                print(f"{TARGET} labelled. Run with --report to see agreement.")
                return
            print("=" * 100, f"\n{sc['id']} (run {n + 1})   [{len(labels) + 1}/{TARGET}]")
            print(f"Criterion: {criteria[sc['id']]}\n\n{format_transcript(run['transcript'])}\n")
            answer = input("Your score 1-5 (s to skip, q to quit): ").strip().lower()
            if answer == "q":
                return
            if answer not in {"1", "2", "3", "4", "5"}:
                continue
            labels.append({"results": key[0], "scenario": key[1], "run": n,
                           "human": int(answer), "judge": judged["score"]})
            # Saved after every label, so quitting halfway loses nothing.
            LABELS.write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    print("No more judged transcripts in this file.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", type=Path, help="a results file from evals/results")
    parser.add_argument("--report", action="store_true", help="print agreement with the labels so far")
    args = parser.parse_args()
    if args.report or args.results is None:
        print(json.dumps(agreement(_load()), indent=2))
    else:
        _label(args.results)


if __name__ == "__main__":
    main()
