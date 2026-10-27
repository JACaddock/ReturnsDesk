from returns_desk import config
from returns_desk.agent import Conversation, run_turn
from tests.conftest import ScriptedModel, response, text_block, tool_use_block


def test_plain_reply_takes_one_step(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(response(text_block("Hi! What's your order number?")))

    result = run_turn(conv, "hello", model)

    assert result.reply == "Hi! What's your order number?"
    assert result.stopped_because == "reply"
    assert len(result.steps) == 1


def test_tool_call_runs_and_result_goes_back_to_model(ctx, a1043_email):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(
        response(tool_use_block("verify_customer", {"order_id": "A1043", "email": a1043_email})),
        response(text_block("Your order was delivered.")),
    )

    result = run_turn(conv, f"where is A1043? I'm {a1043_email}", model)

    assert result.reply == "Your order was delivered."
    call = result.steps[0].tool_calls[0]
    assert call.name == "verify_customer" and not call.is_error

    tool_result = model.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert "A1043" in tool_result["content"]


def test_tool_error_is_reported_to_model(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(
        response(tool_use_block("verify_customer", {"order_id": "A1043", "email": "wrong@example.com"})),
        response(text_block("That email doesn't match.")),
    )

    run_turn(conv, "where is A1043?", model)

    tool_result = model.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True


def test_parallel_tool_calls_return_in_one_message(ctx, a1043_email):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(
        response(
            tool_use_block("verify_customer", {"order_id": "A1043", "email": a1043_email}, id="toolu_a"),
            tool_use_block("search_policy", {"query": "refunds"}, id="toolu_b"),
        ),
        response(text_block("done")),
    )

    run_turn(conv, "two orders", model)

    results = model.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]


def test_loop_stops_after_max_steps(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    looping = [response(tool_use_block("search_policy", {"query": "refunds"}))
               for _ in range(config.MAX_STEPS)]
    model = ScriptedModel(*looping)

    result = run_turn(conv, "where is my order", model)

    assert result.stopped_because == "max_steps"
    assert len(model.calls) == config.MAX_STEPS
    assert ctx.conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == 1


def test_escalation_ends_the_agents_part(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(
        response(tool_use_block("escalate_to_human", {"reason": "asked for a person", "order_id": None})),
        response(text_block("A colleague will be in touch.")),
    )

    first = run_turn(conv, "I want to speak to a person", model)
    second = run_turn(conv, "hello?", model)

    assert first.stopped_because == "escalated"
    assert second.stopped_because == "escalated" and second.steps == []
    assert len(model.calls) == 2
    # The person picking it up still sees what the customer said after the handoff.
    assert conv.messages[-1] == {"role": "user", "content": "hello?"}


def test_tools_see_the_latest_customer_message(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    run_turn(conv, "first", ScriptedModel(response(text_block("ok"))))
    run_turn(conv, "yes", ScriptedModel(response(text_block("ok"))))
    assert (ctx.turn, ctx.last_user_message) == (2, "yes")


def test_refusal_is_not_added_to_history(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(response(stop_reason="refusal"))

    result = run_turn(conv, "something", model)

    assert result.stopped_because == "refusal"
    assert conv.messages == [{"role": "user", "content": "something"}]


def test_history_carries_across_turns(ctx):
    conv = Conversation(ctx=ctx, model_name="scripted")
    model = ScriptedModel(response(text_block("first")), response(text_block("second")))

    run_turn(conv, "one", model)
    run_turn(conv, "two", model)

    sent = model.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert len(conv.transcript) == 2 and conv.transcript[1].turn == 2
