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


# ── Sessions endpoints (Phase 6: history in the browser) ──────────
def test_chat_creates_session_and_persists_messages():
    r = client.post("/api/chat", json={"message": "start a chat"})
    data = r.json()
    assert data["session_id"]
    sid = data["session_id"]

    # session shows up in the listing
    r2 = client.get("/api/sessions")
    assert any(s["id"] == sid for s in r2.json())

    # messages endpoint returns both turns in order
    r3 = client.get(f"/api/sessions/{sid}")
    msgs = r3.json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "start a chat"
    # assistant turn carries the run_id for linking back to telemetry
    assert msgs[1]["run_id"] is not None


def test_chat_reuses_existing_session_when_id_provided():
    r1 = client.post("/api/chat", json={"message": "hello"})
    sid = r1.json()["session_id"]
    r2 = client.post("/api/chat", json={"message": "follow up", "session_id": sid})
    assert r2.json()["session_id"] == sid

    msgs = client.get(f"/api/sessions/{sid}").json()
    assert len(msgs) == 4   # user1, assistant1, user2, assistant2


def test_stream_emits_session_id_up_front():
    with client.stream("POST", "/api/chat/stream",
                       json={"message": "streamed hi"}) as r:
        events = [json.loads(line[6:])
                  for line in r.iter_lines() if line.startswith("data: ")]
    # first event must be the session binding
    assert events[0]["type"] == "session"
    assert events[0]["id"]
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["session_id"] == events[0]["id"]


def test_rename_session():
    r = client.post("/api/chat", json={"message": "will be renamed"})
    sid = r.json()["session_id"]
    r2 = client.patch(f"/api/sessions/{sid}", json={"title": "My Renamed Chat"})
    assert r2.status_code == 200
    assert r2.json()["title"] == "My Renamed Chat"


def test_delete_session_removes_it_from_listing():
    r = client.post("/api/chat", json={"message": "throwaway"})
    sid = r.json()["session_id"]
    client.delete(f"/api/sessions/{sid}")
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert not any(s["id"] == sid for s in client.get("/api/sessions").json())


def test_unknown_session_returns_404():
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.patch("/api/sessions/nope",
                        json={"title": "x"}).status_code == 404
    assert client.delete("/api/sessions/nope").status_code == 404
