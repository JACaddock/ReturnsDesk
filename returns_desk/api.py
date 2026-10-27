"""HTTP API. The eval harness talks to /chat; the React UI (web/) talks to /chat/stream, which sends
each tool call as it happens. If web/dist exists (npm run build), the UI is served at /.

    uvicorn returns_desk.api:create_app --factory --reload
"""

import json
import logging
import queue
import threading
from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from returns_desk import config, db
from returns_desk.agent import Conversation, TurnResult, run_turn
from returns_desk.model import AnthropicModel, Model
from returns_desk.tools import ToolContext

log = logging.getLogger(__name__)
WEB_DIST = config.ROOT / "web" / "dist"


class ChatRequest(BaseModel):
    # Capped because every step resends the whole history to the model, and that's what costs money.
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    stopped_because: str
    steps: list[dict[str, Any]]


def create_app(model: Model | None = None, db_path=config.DB_PATH, today: date | None = None) -> FastAPI:
    """`today` pins the date the tools use (the eval harness does this); by default it's the real date."""
    app = FastAPI(title="Returns Desk")
    conn = db.connect(db_path)
    app.state.conn = conn  # so whoever made the app (the eval harness) can close it
    agent_model = model or AnthropicModel()
    # shortcut: kept in memory and never removed, so they're lost on restart and grow while the server runs;
    # fine for a demo, needs a store with expiry before a public deploy.
    conversations: dict[str, Conversation] = {}
    # shortcut: one turn at a time across all chats. Every conversation shares one SQLite connection, and a
    # transaction belongs to the connection, so overlapping turns would commit or break each other's writes.
    # Fine with one model server; give each request its own connection to run turns in parallel.
    turn_lock = threading.Lock()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "model": agent_model.name}

    def conversation(req: ChatRequest) -> Conversation:
        if req.conversation_id is None:
            conv = Conversation(ctx=ToolContext(conn=conn, today=today or date.today()), model_name=agent_model.name)
            conversations[conv.id] = conv
            return conv
        if req.conversation_id in conversations:
            return conversations[req.conversation_id]
        raise HTTPException(404, "Unknown conversation_id")

    def response(conv: Conversation, result: TurnResult) -> ChatResponse:
        return ChatResponse(
            conversation_id=conv.id,
            reply=result.reply,
            stopped_because=result.stopped_because,
            steps=[asdict(s) for s in result.steps],
        )

    @app.post("/chat", response_model=ChatResponse)
    def chat(req: ChatRequest) -> ChatResponse:
        conv = conversation(req)
        with turn_lock:
            result = run_turn(conv, req.message, agent_model)
        return response(conv, result)

    @app.post("/chat/stream")
    def chat_stream(req: ChatRequest) -> StreamingResponse:
        """Server-sent events: a `step` for each step that called tools, then `done` (a ChatResponse) or `error`."""
        conv = conversation(req)
        events: queue.Queue = queue.Queue()

        def work():
            try:
                with turn_lock:
                    result = run_turn(conv, req.message, agent_model,
                                      on_step=lambda step: events.put(("step", asdict(step))))
                events.put(("done", response(conv, result).model_dump()))
            except Exception:
                log.exception("Turn failed in conversation %s", conv.id)
                events.put(("error", {"detail": "The model call failed. Check the server log."}))

        # The turn runs on its own thread so this one can send its steps as they arrive.
        threading.Thread(target=work, daemon=True).start()

        def stream():
            while True:
                kind, data = events.get()
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
                if kind != "step":
                    return

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/conversations/{conversation_id}/transcript")
    def transcript(conversation_id: str) -> dict:
        conv = conversations.get(conversation_id)
        if conv is None:
            raise HTTPException(404, "Unknown conversation_id")
        return {
            "conversation_id": conv.id,
            "model": conv.model_name,
            "tool_descriptions_version": conv.tool_descriptions_version,
            "steps": [asdict(s) for s in conv.transcript],
        }

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")  # after the routes, so they win
    return app
