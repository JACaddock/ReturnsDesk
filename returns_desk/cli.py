"""Chat with the agent in a terminal. Tool calls are printed as they happen.

    python -m returns_desk.cli
"""

import argparse
import json

import anthropic

from returns_desk import config, db
from returns_desk.agent import Conversation, run_turn
from returns_desk.model import AnthropicModel
from returns_desk.tools import ToolContext

DIM, RESET = "\033[2m", "\033[0m"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=config.AGENT_MODEL)
    parser.add_argument("--quiet", action="store_true", help="hide tool calls and token counts")
    args = parser.parse_args()

    if not config.DB_PATH.exists():
        raise SystemExit(f"No database at {config.DB_PATH}. Run `python -m returns_desk.seed` first.")

    model = AnthropicModel(name=args.model)
    conv = Conversation(ctx=ToolContext(conn=db.connect(config.DB_PATH)), model_name=model.name)
    print(f"Returns Desk ({model.name}). Ctrl+C or an empty line to quit.\n")

    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not message:
            break

        try:
            result = run_turn(conv, message, model)
        except anthropic.APIError as e:
            # Rate limits, no credit, model server down: report it and keep the conversation.
            print(f"[model error: {e}]\n")
            continue
        if not args.quiet:
            for step in result.steps:
                for call in step.tool_calls:
                    status = "error" if call.is_error else "ok"
                    print(f"{DIM}  [{call.name} {json.dumps(call.input)} -> {status}] {call.result[:200]}{RESET}")
                print(f"{DIM}  step {step.step}: {step.stop_reason}, {step.latency_ms} ms, "
                      f"{step.input_tokens} in / {step.output_tokens} out{RESET}")
        print(f"agent> {result.reply}\n")


if __name__ == "__main__":
    main()
