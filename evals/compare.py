"""Compare a PR's eval results with main's, for CI.

The suite runs locally on a GPU and the results file is committed: on GitHub's free runners a local model
would take longer than the 6-hour job limit. So CI doesn't rerun the suite. It checks the committed results
belong to this code, compares them with the latest results on main, and fails the build on a drop.

    python -m evals.compare --base origin/main --summary diff.md

Fails if the PR changes the agent but adds no results file, if the results were run on different code than
the PR's, if any guardrail was violated, or if the success rate is below main's.
"""

import argparse
import json
import subprocess
import sys

RESULTS = "evals/results"
# What the results depend on. Results from a commit are only valid while none of these have changed since.
AGENT_PATHS = ["returns_desk", "evals/scenarios", "evals/checks.py", "evals/judge.py", "evals/run.py",
               "pyproject.toml"]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, encoding="utf-8").stdout


def compare(new: dict, base: dict | None) -> tuple[str, list[str]]:
    """A markdown report and the reasons to fail, if any. Pure, so it's tested without git."""
    s = new["summary"]
    failures = []
    if s["guardrail_violations"]:
        failures.append(f"{s['guardrail_violations']} guardrail violation(s)")

    lines = [f"### Eval results for `{new['commit']}`", "",
             f"Agent `{new['agent_model']}`, customer `{new['customer_model']}`, "
             f"judge `{new.get('judge_model')}`, tool descriptions v{new['tool_descriptions_version']}.", ""]
    if base is None:
        lines += ["No results on the base branch yet, so there's nothing to compare against.", ""]
        base_s, base_runs = {}, {}
    else:
        base_s = base["summary"]
        base_runs = {sc["id"]: sc["runs"] for sc in base["scenarios"]}
        if base["agent_model"] != new["agent_model"]:
            lines += [f"The agent model changed (`{base['agent_model']}` before), so a drop doesn't fail the build.", ""]
        elif s["success_rate"] < base_s["success_rate"]:
            failures.append(f"success rate fell from {base_s['success_rate']:.0%} to {s['success_rate']:.0%}")

    lines += ["| | base | this PR |", "|---|---|---|"]
    for key, value in s.items():
        lines.append(f"| {key} | {base_s.get(key, '–')} | {value} |")

    flips = []
    for sc in new["scenarios"]:
        now = all(r["passed"] for r in sc["runs"])
        before = base_runs.get(sc["id"])
        if before is None:
            flips.append(f"- `{sc['id']}`: new, {'passes' if now else 'fails'}")
        elif all(r["passed"] for r in before) != now:
            flips.append(f"- `{sc['id']}`: {'now passes' if now else '**now fails**'}")
    if flips:
        lines += ["", "Changed scenarios:", *flips]
    lines += ["", f"**{'Failed: ' + '; '.join(failures) if failures else 'Passed'}**"]
    return "\n".join(lines), failures


def _latest(ref: str, paths: list[str]) -> dict | None:
    """The most recent results file among these paths at this ref, by the date inside it."""
    found = [json.loads(git("show", f"{ref}:{p}")) for p in paths if p.endswith(".json")]
    return max(found, key=lambda r: r["date"], default=None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="the branch to compare with")
    parser.add_argument("--summary", help="also write the markdown report to this file")
    args = parser.parse_args()

    merge_base = git("merge-base", args.base, "HEAD").strip()
    added = git("diff", "--name-only", "--diff-filter=A", merge_base, "HEAD", "--", RESULTS).split()
    agent_changed = bool(git("diff", "--name-only", merge_base, "HEAD", "--", *AGENT_PATHS).strip())
    new = _latest("HEAD", added)

    if new is None:
        report = ("No new eval results in this PR, and the agent changed. Run `python -m evals.run --runs 3`, "
                  "commit the results file, and push." if agent_changed else
                  "No eval results needed: nothing the agent depends on changed.")
        failures = ["no eval results"] if agent_changed else []
    elif new["commit"].startswith("uncommitted") or new["commit"].endswith("-dirty"):
        report, failures = f"Results `{new['commit']}` came from uncommitted code. Commit first, then run.", ["dirty"]
    elif git("diff", "--name-only", new["commit"], "HEAD", "--", *AGENT_PATHS).strip():
        report = f"The agent changed after `{new['commit']}`, the commit these results were run on. Rerun them."
        failures = ["stale results"]
    else:
        # ls-tree with a path prints full paths, and nothing (not an error) when main has no results yet.
        report, failures = compare(new, _latest(args.base, git("ls-tree", "--name-only", args.base, f"{RESULTS}/").split()))

    print(report)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
