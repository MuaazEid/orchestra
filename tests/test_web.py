import os
import json
os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"

from fastapi.testclient import TestClient
from orchestra.web.server import app

client = TestClient(app)


def test_roster_lists_all_specialists():
    r = client.get("/api/roster")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "Math Solver" in names and "Memory Keeper" in names


def test_chat_endpoint_returns_reply_and_tasks():
    r = client.post("/api/chat", json={"message": "hello there"})
    assert r.status_code == 200
    data = r.json()
    assert "reply" in data and "tasks" in data and "run_id" in data
    assert isinstance(data["tasks"], list)


def test_index_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "Orchestra" in r.text


def test_chat_stream_emits_progress_then_done():
    with client.stream("POST", "/api/chat/stream", json={"message": "hello"}) as r:
        assert r.status_code == 200
        events = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    types = [e["type"] for e in events]
    assert "progress" in types
    assert types[-1] == "done"
    assert events[-1]["data"]["reply"]
