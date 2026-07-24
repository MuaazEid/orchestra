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


# ── Health (what the browser's status line reads) ─────────────────
def test_health_reports_backend_and_models():
    r = client.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert h["ok"] is True
    assert h["backend"] == "mock"        # set at the top of this module
    assert h["main_model"] and h["fast_model"]
    assert h["specialists"] >= 9
    assert h["concurrency"] >= 1


# ── Regenerate ────────────────────────────────────────────────────
def test_regenerate_replaces_the_last_reply_without_duplicating_the_prompt():
    sid = client.post("/api/chat", json={"message": "what is 2 + 2"}).json()["session_id"]
    before = client.get(f"/api/sessions/{sid}").json()
    assert [m["role"] for m in before] == ["user", "assistant"]

    r = client.post("/api/chat", json={"session_id": sid, "regenerate": True})
    assert r.status_code == 200
    assert r.json()["session_id"] == sid

    after = client.get(f"/api/sessions/{sid}").json()
    # still exactly one exchange — the prompt was replayed, not re-appended
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[0]["text"] == "what is 2 + 2"
    assert after[0]["id"] == before[0]["id"]      # same user turn
    assert after[1]["id"] != before[1]["id"]      # freshly generated reply


def test_regenerate_streams_and_replays_the_last_user_message():
    sid = client.post("/api/chat", json={"message": "streamed prompt"}).json()["session_id"]
    with client.stream("POST", "/api/chat/stream",
                       json={"session_id": sid, "regenerate": True}) as r:
        events = [json.loads(line[6:])
                  for line in r.iter_lines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["session_id"] == sid
    assert [m["role"] for m in client.get(f"/api/sessions/{sid}").json()] == \
        ["user", "assistant"]


def test_regenerate_without_session_is_rejected():
    assert client.post("/api/chat", json={"regenerate": True}).status_code == 400


def test_regenerate_unknown_session_is_404():
    r = client.post("/api/chat", json={"session_id": "nope", "regenerate": True})
    assert r.status_code == 404


def test_empty_message_is_rejected():
    assert client.post("/api/chat", json={"message": "   "}).status_code == 400


# ── Live routing trace ────────────────────────────────────────────
def test_progress_events_carry_the_specialist_name():
    with client.stream("POST", "/api/chat/stream",
                       json={"message": "multiply 6 by 7"}) as r:
        events = [json.loads(line[6:])
                  for line in r.iter_lines() if line.startswith("data: ")]
    progress = [e for e in events if e["type"] == "progress"]
    assert progress, "expected progress events"
    # every progress event carries the key, and at least one names a real
    # specialist — that tag is what lights the roster in the browser
    assert all("specialist" in e for e in progress)
    named = {e["specialist"] for e in progress if e["specialist"]}
    roster = {s["name"] for s in client.get("/api/roster").json()}
    assert named and named <= roster


# ── The UI must keep working with no network ──────────────────────
def test_ui_makes_no_external_requests():
    """Orchestra's premise is zero cloud. A CDN font or script would make
    the UI degrade (or hang) on the offline machine this is built for, so
    it's a regression worth failing the build over."""
    for path in ("/", "/app.css", "/app.js", "/markdown.js"):
        body = client.get(path).text
        assert "//fonts.googleapis" not in body
        assert "//cdn." not in body and "//unpkg" not in body
        assert "//cdnjs" not in body and "//jsdelivr" not in body


def test_ui_assets_are_served():
    for path, marker in (("/app.css", "--accent"),
                         ("/app.js", "AbortController"),
                         ("/markdown.js", "escapeHtml")):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text
