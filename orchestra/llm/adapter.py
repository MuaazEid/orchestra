"""LLM Adapter layer — the wall between our logic and any model backend.

Why this exists (the professional pattern):
- Business logic NEVER imports ChatOllama directly. It talks to `LLMClient`.
- Swap backends (Ollama today, anything tomorrow) by changing ONE config line.
- Retries, timeouts and structured-output hardening live HERE, once,
  instead of being copy-pasted into every node.
- MockLLM implements the same interface -> the whole graph is unit-testable
  on a machine with no GPU and no Ollama (like the one this was written on).
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Data contracts ─────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass(frozen=True)
class LLMReply:
    """Normalized reply — same shape no matter which backend produced it."""
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    latency_s: float = 0.0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Raised after retries are exhausted — callers decide how to degrade."""


# ── The interface every backend must honor ─────────────────────────
class LLMClient(ABC):
    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[Callable] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMReply:
        """messages: [{"role": "system"|"user"|"assistant"|"tool", "content": str}]"""

    # Hardened structured output — the #1 weakness of small local models.
    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema_hint: str = "",
        temperature: float = 0.0,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        """Ask for JSON, validate, repair, retry. Never trust; always verify."""
        last_text = ""
        for attempt in range(1, max_attempts + 1):
            reply = self.chat(messages, json_mode=True, temperature=temperature)
            last_text = reply.text
            parsed = _extract_json(reply.text)
            if parsed is not None:
                return parsed
            logger.warning("chat_json | attempt %d: unparseable JSON, retrying", attempt)
            messages = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content":
                    "Your previous reply was not valid JSON. "
                    f"Reply with ONLY valid JSON. {schema_hint}"},
            ]
        raise LLMError(f"JSON parse failed after {max_attempts} attempts: {last_text[:200]}")


def _extract_json(text: str) -> dict | None:
    """Tolerant JSON extraction: raw, fenced, or embedded object."""
    for candidate in (
        text,
        re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M),
        (m.group(0) if (m := re.search(r"\{.*\}", text, re.S)) else ""),
    ):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            continue
    return None


# ── Retry wrapper (shared by real backends) ────────────────────────
def with_retries(fn: Callable[[], LLMReply], *, max_retries: int,
                 backoff_s: float, label: str) -> LLMReply:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:  # network / timeout / backend errors
            last_exc = exc
            if attempt < max_retries:
                sleep = backoff_s * (2 ** attempt)
                logger.warning("%s | attempt %d failed (%s), retrying in %.1fs",
                               label, attempt + 1, exc, sleep)
                time.sleep(sleep)
    raise LLMError(f"{label} failed after {max_retries + 1} attempts") from last_exc
