import json
import threading
import time

from fastapi.testclient import TestClient

from returns_desk.api import create_app
from tests.conftest import ScriptedModel, response, text_block, tool_use_block


def test_chat_keeps_conversation_between_requests(db_path):
    model = ScriptedModel(response(text_block("Hello!")), response(text_block("Still here.")))
    client = TestClient(create_app(model=model, db_path=db_path))

    first = client.post("/chat", json={"message": "hi"}).json()
    second = client.post("/chat", json={"message": "again", "conversation_id": first["conversation_id"]}).json()

    assert first["reply"] == "Hello!"
    assert second["reply"] == "Still here."
    assert second["conversation_id"] == first["conversation_id"]

    transcript = client.get(f"/conversations/{first['conversation_id']}/transcript").json()
    assert len(transcript["steps"]) == 2


def test_turns_run_one_at_a_time(db_path):
    class SlowModel:
        name = "slow"
        active = most = 0

        def create(self, *, system, messages, tools):
            SlowModel.active += 1
            SlowModel.most = max(SlowModel.most, SlowModel.active)
            time.sleep(0.2)
            SlowModel.active -= 1
            return response(text_block("ok"))

    app = create_app(model=SlowModel(), db_path=db_path)
    # Two clients so the requests really overlap, like two browser tabs.
    threads = [threading.Thread(target=TestClient(app).post, args=("/chat",), kwargs={"json": {"message": "hi"}})
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert SlowModel.most == 1


def test_overlong_message_is_rejected(db_path):
    client = TestClient(create_app(model=ScriptedModel(), db_path=db_path))
    assert client.post("/chat", json={"message": "x" * 2001}).status_code == 422


def test_unknown_conversation_is_404(db_path):
    client = TestClient(create_app(model=ScriptedModel(), db_path=db_path))
    assert client.post("/chat", json={"message": "hi", "conversation_id": "nope"}).status_code == 404


def test_stream_sends_tool_steps_then_the_reply(db_path):
    model = ScriptedModel(
        response(tool_use_block("search_policy", {"query": "opened headphones"})),
        response(text_block("Opened headphones can be returned.")),
    )
    client = TestClient(create_app(model=model, db_path=db_path))

    body = client.post("/chat/stream", json={"message": "can I return opened headphones?"}).text
    events = [(e.split("\n")[0], json.loads(e.split("data: ", 1)[1])) for e in body.strip().split("\n\n")]

    assert [kind for kind, _ in events] == ["event: step", "event: done"]
    assert events[0][1]["tool_calls"][0]["name"] == "search_policy"
    assert events[1][1]["reply"] == "Opened headphones can be returned."
    assert len(events[1][1]["steps"]) == 2
