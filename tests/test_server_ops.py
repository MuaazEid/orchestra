"""Operational behaviour of the web server: health, limits, failure paths,
keep-alive, run slots and warm-up."""
import json
import os
import threading
import time

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestra import __version__
from orchestra.core.config import settings
from orchestra.engine import pipeline
from orchestra.engine.pipeline import RunReport
from orchestra.llm import health
from orchestra.llm.adapter import LLMClient, LLMUnavailable
from orchestra.observability import history
from orchestra.web import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    health._cache.update(at=0.0, value=None)
    health._warm.update(state="idle", detail="")


class _Down(LLMClient):
    def chat(self, messages, **kw):
        raise LLMUnavailable("Ollama is not reachable at http://x. Start it with `ollama serve`.")


def _events(resp_text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in resp_text.splitlines() if line.startswith("data: ")]


# ── Health ─────────────────────────────────────────────────────────
def test_health_reports_version_readiness_and_warm_state():
    h = client.get("/api/health").json()
    assert h["version"] == __version__ and h["ready"] is True
    assert h["llm"] == {"reachable": True, "missing": [], "error": None}
    assert h["warm"]["state"] == "idle"


def test_health_says_not_ready_when_ollama_is_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    def refuse(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", refuse)
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["ready"] is False
    assert h["llm"]["reachable"] is False and "ollama serve" in h["llm"]["error"]


def test_health_lists_missing_models(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "main_model", "qwen2.5:7b")
    monkeypatch.setattr(settings, "fast_model", "qwen2.5:3b")

    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen2.5:7b"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    h = client.get("/api/health").json()
    assert h["ready"] is False and h["llm"]["missing"] == ["qwen2.5:3b"]


def test_model_matching_accepts_the_implicit_latest_tag():
    assert health.model_installed("llama3", {"llama3:latest"})
    assert not health.model_installed("llama3:8b", {"llama3:latest"})


# ── Request limits and routing errors ──────────────────────────────
def test_overlong_message_is_rejected():
    r = client.post("/api/chat", json={"message": "x" * (settings.max_message_chars + 1)})
    assert r.status_code == 422


def test_unknown_session_id_is_404_and_creates_nothing():
    r = client.post("/api/chat", json={"message": "hi", "session_id": "nope"})
    assert r.status_code == 404
    assert history.list_sessions() == []


# ── Failure paths keep history valid and the reply honest ──────────
def test_backend_down_returns_a_readable_reply_and_saves_it(monkeypatch):
    monkeypatch.setattr(pipeline, "get_llm", lambda tier: _Down())
    r = client.post("/api/chat", json={"message": "Multiply 6 by 7"}).json()
    assert r["ok"] is False and r["reply"].startswith("⚠")
    assert "ollama serve" in r["reply"]
    roles = [m["role"] for m in client.get(f"/api/sessions/{r['session_id']}").json()]
    assert roles == ["user", "assistant"]           # still a valid alternation


def test_stream_with_backend_down_ends_with_a_done_event(monkeypatch):
    monkeypatch.setattr(pipeline, "get_llm", lambda tier: _Down())
    with client.stream("POST", "/api/chat/stream", json={"message": "Multiply 6 by 7"}) as resp:
        evs = _events("".join(resp.iter_text()))
    assert evs[-1]["type"] == "done" and evs[-1]["data"]["ok"] is False


def test_a_crashing_worker_becomes_an_error_event_not_a_hung_stream(monkeypatch):
    monkeypatch.setattr(server, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as resp:
        evs = _events("".join(resp.iter_text()))
    assert evs[-1]["type"] == "error" and "boom" not in evs[-1]["text"]   # no internals leaked


def test_deleting_the_chat_mid_run_does_not_crash_the_run(monkeypatch):
    sid = client.post("/api/chat", json={"message": "first"}).json()["session_id"]

    def handle_and_delete(text, on_event=None):
        history.delete_session(sid)
        return RunReport(reply="late answer", tasks=[], run_id="r1")
    monkeypatch.setattr(server._orchestra, "handle", handle_and_delete)
    report = server._run("again", sid)               # must not raise
    assert report.reply == "late answer"


# ── Keep-alive ─────────────────────────────────────────────────────
def test_stream_sends_keepalive_comments_while_a_run_is_slow(monkeypatch):
    monkeypatch.setattr(server, "_HEARTBEAT_S", 0.05)

    def slow(text, on_event=None):
        time.sleep(0.4)
        return RunReport(reply="done", tasks=[], run_id="r")
    monkeypatch.setattr(server._orchestra, "handle", slow)
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as resp:
        raw = "".join(resp.iter_text())
    assert raw.count(": keep-alive") >= 2
    assert _events(raw)[-1]["type"] == "done"


# ── Run slots ──────────────────────────────────────────────────────
def test_extra_runs_wait_for_a_slot_and_say_so(monkeypatch):
    monkeypatch.setattr(server, "_slots", threading.BoundedSemaphore(1))
    release = threading.Event()
    started = threading.Event()

    def blocking(text, on_event=None):
        started.set()
        release.wait(5)
        return RunReport(reply=text, tasks=[], run_id="r")
    monkeypatch.setattr(server._orchestra, "handle", blocking)
    sid = history.create_session("s").id
    seen: list[str] = []
    t1 = threading.Thread(target=server._run, args=("one", sid))
    t1.start()
    assert started.wait(2)
    t2 = threading.Thread(target=server._run, args=("two", sid, seen.append))
    t2.start()
    time.sleep(0.2)
    assert seen == ["Waiting for a free slot…"]    # second run is queued, not running
    release.set()
    t1.join(2); t2.join(2)
    assert not t1.is_alive() and not t2.is_alive()


# ── Warm-up ────────────────────────────────────────────────────────
def test_warmup_loads_each_model_with_the_pipelines_context_window(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "main_model", "m1:7b")
    monkeypatch.setattr(settings, "fast_model", "m2:3b")
    monkeypatch.setattr(settings, "num_ctx", 4096)
    monkeypatch.setattr(settings, "keep_alive", "9m")
    monkeypatch.setattr(health, "ollama_status", lambda timeout=2.0: {
        "reachable": True, "installed": ["m1:7b", "m2:3b"], "missing": [], "error": None})
    posts = []

    class R:
        def raise_for_status(self): pass
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: posts.append((url, json)) or R())
    state = health.warm_models()
    assert state["state"] == "ready"
    assert [p[1]["model"] for p in posts] == ["m1:7b", "m2:3b"]
    assert all(p[1]["options"] == {"num_ctx": 4096} and p[1]["keep_alive"] == "9m" for p in posts)
    assert all(p[0].endswith("/api/generate") for p in posts)


def test_warmup_reports_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "main_model", "m1:7b")
    monkeypatch.setattr(settings, "fast_model", "m2:3b")
    monkeypatch.setattr(health, "ollama_status", lambda timeout=2.0: {
        "reachable": False, "installed": [], "missing": [], "error": "Ollama is not reachable"})
    assert health.warm_models()["state"] == "failed"

    monkeypatch.setattr(health, "ollama_status", lambda timeout=2.0: {
        "reachable": True, "installed": [], "missing": ["m1:7b", "m2:3b"], "error": None})
    s = health.warm_models()
    assert s["state"] == "failed" and "not installed" in s["detail"]


def test_server_starts_warmup_once_on_startup_for_a_real_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "start_warmup", lambda: calls.append(1))
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "warm_on_start", True)
    with TestClient(server.app):
        pass
    assert calls == [1]


def test_server_does_not_warm_up_the_mock_backend_or_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "start_warmup", lambda: calls.append(1))
    with TestClient(server.app):            # mock backend
        pass
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "warm_on_start", False)
    with TestClient(server.app):
        pass
    assert calls == []
