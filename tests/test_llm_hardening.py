"""LLM layer hardening: real timeout, explicit context window, and
permanent-vs-transient error handling."""
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import httpx
import pytest

from orchestra.core.config import settings
from orchestra.llm import adapter
from orchestra.llm.adapter import LLMClient, LLMError, LLMReply, LLMUnavailable, with_retries
from orchestra.llm.backends import OllamaClient, classify_ollama_error


# ── Timeout / context window really reach the Ollama client ────────
class _SpyChat:
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def bind_tools(self, tools):
        return self

    def invoke(self, _messages):
        class _R:
            content = "ok"
            tool_calls = []
        return _R()


def test_ollama_client_passes_timeout_context_and_keepalive(monkeypatch):
    import langchain_ollama
    monkeypatch.setattr(langchain_ollama, "ChatOllama", _SpyChat)
    monkeypatch.setattr(settings, "request_timeout_s", 33.0)
    monkeypatch.setattr(settings, "num_ctx", 4321)
    monkeypatch.setattr(settings, "keep_alive", "7m")
    OllamaClient("qwen2.5:7b").chat([{"role": "user", "content": "hi"}])
    kw = _SpyChat.last_kwargs
    assert kw["client_kwargs"] == {"timeout": 33.0}
    assert kw["num_ctx"] == 4321 and kw["keep_alive"] == "7m"
    assert "timeout" not in kw          # the silently-ignored spelling


def test_real_chatollama_honours_those_kwargs():
    """Guards against a library upgrade silently ignoring our settings
    again: build the REAL class (no network) and read the values back."""
    from langchain_ollama import ChatOllama
    m = ChatOllama(model="x", base_url="http://127.0.0.1:9", num_ctx=2048,
                   keep_alive="5m", client_kwargs={"timeout": 9.0})
    assert m.num_ctx == 2048 and m.keep_alive == "5m"
    assert m.client_kwargs == {"timeout": 9.0}


# ── Error classification ───────────────────────────────────────────
def test_connection_refused_is_permanent_with_a_fix_hint():
    err = classify_ollama_error(httpx.ConnectError("refused"),
                                "qwen2.5:7b", "http://localhost:11434")
    assert isinstance(err, LLMUnavailable)
    assert "ollama serve" in str(err) and "localhost:11434" in str(err)


def test_missing_model_is_permanent_with_pull_command():
    import ollama
    err = classify_ollama_error(ollama.ResponseError("not found", 404),
                                "qwen2.5:7b", "http://localhost:11434")
    assert isinstance(err, LLMUnavailable)
    assert "ollama pull qwen2.5:7b" in str(err)


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("slow"),
    RuntimeError("boom"),
])
def test_transient_errors_are_not_classified_as_fatal(exc):
    assert classify_ollama_error(exc, "m", "http://x") is None


# ── with_retries behaviour ─────────────────────────────────────────
def test_fatal_error_is_raised_immediately_without_backoff(monkeypatch):
    monkeypatch.setattr(adapter.time, "sleep",
                        lambda s: pytest.fail("must not back off on a permanent error"))
    calls = []

    def fn():
        calls.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMUnavailable):
        with_retries(fn, max_retries=3, backoff_s=1, label="t",
                     classify=lambda e: classify_ollama_error(e, "m", "http://x"))
    assert len(calls) == 1


def test_transient_error_is_retried_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(adapter.time, "sleep", sleeps.append)
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise httpx.ReadTimeout("slow")
        return LLMReply(text="finally")

    out = with_retries(fn, max_retries=3, backoff_s=1, label="t",
                       classify=lambda e: classify_ollama_error(e, "m", "http://x"))
    assert out.text == "finally" and sleeps == [1, 2]


def test_exhausted_retries_raise_llmerror_not_unavailable(monkeypatch):
    monkeypatch.setattr(adapter.time, "sleep", lambda s: None)
    with pytest.raises(LLMError) as ei:
        with_retries(lambda: (_ for _ in ()).throw(RuntimeError("x")),
                     max_retries=1, backoff_s=0, label="t")
    assert not isinstance(ei.value, LLMUnavailable)


def test_chat_json_does_not_retry_when_backend_is_unavailable():
    class Down(LLMClient):
        calls = 0

        def chat(self, messages, **kw):
            Down.calls += 1
            raise LLMUnavailable("down")

    with pytest.raises(LLMUnavailable):
        Down().chat_json([{"role": "user", "content": "x"}], max_attempts=3)
    assert Down.calls == 1
