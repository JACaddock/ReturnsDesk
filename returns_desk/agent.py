"""The agent loop.

One customer message in, one reply out. In between:

1. Send the system prompt, conversation history and tool schemas to the model.
2. If the model asks for tools, run each one (tools.execute validates the
   arguments and checks the rules) and send the results back.
3. Repeat until the model replies in plain text, or MAX_STEPS model calls pass
   (which hands the conversation to a person).
4. Record every step in the conversation's transcript. The API, the UI side
   panel and the eval harness all read from it.

History is append-only: the model's responses go back exactly as they came,
including any thinking blocks, which the API needs to see unchanged.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from returns_desk import config, tool_descriptions
from returns_desk.model import Model
from returns_desk.prompts import SYSTEM_PROMPT
from returns_desk.tools import ToolContext, execute, record_escalation, tool_schemas

HANDED_OFF_REPLY = "I've passed this conversation to a member of the team, who will reply within 2 working days."
MAX_STEPS_REPLY = (
    "Sorry, I'm going round in circles on this one. I've stopped here so a member of the team "
    "can pick it up properly."
)
REFUSAL_REPLY = "Sorry, I can't help with that request. Is there anything about an order I can help with?"
TRUNCATED_REPLY = "Sorry, something went wrong putting that reply together. Could you ask again?"


@dataclass
class ToolCall:
    name: str
    input: Any
    result: str
    is_error: bool
    latency_ms: int


@dataclass
class Step:
    """One model call and the tool calls it asked for."""

    turn: int
    step: int
    stop_reason: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Conversation:
    ctx: ToolContext
    model_name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    tool_descriptions_version: str = tool_descriptions.VERSION
    messages: list = field(default_factory=list)
    transcript: list[Step] = field(default_factory=list)
    turns: int = 0


@dataclass
class TurnResult:
    reply: str
    # "reply" | "escalated" | "max_steps" | "refusal" | "max_tokens"
    stopped_because: str
    steps: list[Step]


def run_turn(conv: Conversation, user_message: str, model: Model,
             on_step: Callable[[Step], None] | None = None) -> TurnResult:
    """`on_step` gets each step that called tools, once its tools have run, so the UI can show them live."""
    # Once a person has the conversation, the agent stays out of it, but the message is kept for them.
    if conv.ctx.escalated:
        conv.messages.append({"role": "user", "content": user_message})
        return TurnResult(HANDED_OFF_REPLY, "escalated", [])

    conv.turns += 1
    conv.ctx.turn, conv.ctx.last_user_message = conv.turns, user_message
    conv.messages.append({"role": "user", "content": user_message})
    steps: list[Step] = []

    for step_number in range(1, config.MAX_STEPS + 1):
        started = time.perf_counter()
        response = model.create(system=SYSTEM_PROMPT, messages=conv.messages, tools=tool_schemas())
        step = Step(
            turn=conv.turns,
            step=step_number,
            stop_reason=response.stop_reason,
            latency_ms=_ms_since(started),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            text="".join(b.text for b in response.content if b.type == "text"),
        )
        steps.append(step)
        conv.transcript.append(step)

        # A refused or truncated response isn't added to the history: it may be
        # empty, or end in a half-written tool call that has no result.
        if response.stop_reason == "refusal":
            return TurnResult(REFUSAL_REPLY, "refusal", steps)
        if response.stop_reason == "max_tokens":
            return TurnResult(TRUNCATED_REPLY, "max_tokens", steps)

        conv.messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return TurnResult(step.text, "escalated" if conv.ctx.escalated else "reply", steps)

        # All results go back in a single user message, in the order they were asked for.
        results = []
        for block in tool_uses:
            tool_started = time.perf_counter()
            result = execute(block.name, block.input, conv.ctx)
            step.tool_calls.append(
                ToolCall(block.name, block.input, result.content, result.is_error, _ms_since(tool_started))
            )
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result.content,
                "is_error": result.is_error,
            })
        conv.messages.append({"role": "user", "content": results})
        if on_step:
            on_step(step)

    record_escalation(conv.ctx, f"Agent hit the {config.MAX_STEPS}-step limit in one turn")
    return TurnResult(MAX_STEPS_REPLY, "max_steps", steps)


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
