"""Concrete LLM backends.

OllamaClient  -> runs on the TARGET machine (Linux Mint + Ollama).
MockLLM       -> runs anywhere; scripted replies for deterministic tests.

factory() picks the backend from config — the rest of the codebase
never knows which one it got.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Callable

from ..core.config import settings
from .adapter import LLMClient, LLMReply, ToolCall, with_retries

logger = logging.getLogger(__name__)


# ── Real backend (lazy import: langchain_ollama only needed at runtime) ──
class OllamaClient(LLMClient):
    def __init__(self, model: str):
        self.model = model

    def chat(self, messages, *, tools=None, json_mode=False,
             temperature=0.0) -> LLMReply:
        from langchain_ollama import ChatOllama  # imported here on purpose

        def _call() -> LLMReply:
            llm = ChatOllama(
                model=self.model,
                base_url=settings.ollama_base_url,
                temperature=temperature,
                format="json" if json_mode else None,
                timeout=settings.request_timeout_s,
            )
            if tools:
                llm = llm.bind_tools(tools)
            t0 = time.perf_counter()
            resp = llm.invoke(_to_langchain(messages))
            latency = time.perf_counter() - t0
            calls = tuple(
                ToolCall(tc.get("name", ""), tc.get("args", {}), tc.get("id", ""))
                for tc in (getattr(resp, "tool_calls", None) or [])
            )
            return LLMReply(text=resp.content or "", tool_calls=calls,
                            model=self.model, latency_s=latency)

        return with_retries(_call, max_retries=settings.max_retries,
                            backoff_s=settings.retry_backoff_s,
                            label=f"ollama:{self.model}")


def _to_langchain(messages: list[dict[str, str]]):
    from langchain_core.messages import (AIMessage, HumanMessage,
                                         SystemMessage, ToolMessage)
    role_map = {"system": SystemMessage, "user": HumanMessage,
                "assistant": AIMessage}
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append(ToolMessage(content=m["content"],
                                   tool_call_id=m.get("tool_call_id", "")))
        else:
            out.append(role_map[m["role"]](content=m["content"]))
    return out


# ── Mock backend (tests / dev on weak machines) ────────────────────
class MockLLM(LLMClient):
    """Deterministic scripted backend.

    queue(reply_or_text, ...) enqueues replies; chat() pops them in order.
    If the queue is empty, echoes a canned reply — tests stay predictable.
    """

    def __init__(self, model: str = "mock"):
        self.model = model
        self._script: deque[LLMReply] = deque()
        self.calls: list[dict[str, Any]] = []  # spy: every request recorded

    def queue(self, *replies: LLMReply | str) -> "MockLLM":
        for r in replies:
            self._script.append(
                r if isinstance(r, LLMReply) else LLMReply(text=r, model=self.model)
            )
        return self

    def chat(self, messages, *, tools=None, json_mode=False,
             temperature=0.0) -> LLMReply:
        self.calls.append({
            "messages": messages,
            "tools": [getattr(t, "__name__", str(t)) for t in (tools or [])],
            "json_mode": json_mode,
            "temperature": temperature,
        })
        if self._script:
            return self._script.popleft()
        return LLMReply(text='{"tasks": []}' if json_mode else "mock-reply",
                        model=self.model)


# ── Factory ────────────────────────────────────────────────────────
_mock_singletons: dict[str, MockLLM] = {}


def get_llm(tier: str = "main") -> LLMClient:
    """tier: 'main' (reasoning) or 'fast' (classification/extraction)."""
    model = settings.main_model if tier == "main" else settings.fast_model
    if settings.llm_backend == "mock":
        return _mock_singletons.setdefault(tier, MockLLM(f"mock-{tier}"))
    return OllamaClient(model)
