"""Aggregator — the front desk.

v1 just joined results with newlines. v2:
- One task  -> return its result directly (no wasted LLM call, no latency).
- Many      -> a 'fast'-tier LLM weaves them into ONE coherent reply.
- LLM down  -> graceful fallback to the labeled join (v1 behavior as the
               safety net, not the ceiling).
Failures are reported honestly — never hidden from the user.
"""
from __future__ import annotations

import logging

from ..core.contracts import Task, TaskStatus
from ..llm.adapter import LLMClient, LLMError
from ..observability.telemetry import Telemetry

logger = logging.getLogger(__name__)

_AGG_PROMPT = (
    "You are the spokesperson of a multi-agent assistant. Below are the "
    "results of the sub-tasks executed for the user's request. Combine them "
    "into ONE natural, concise reply in the user's language. Keep every "
    "concrete value (numbers, saved facts, creative text) intact. If any "
    "task failed, mention it briefly and honestly. No headers, no bullet "
    "lists unless the content demands it."
)


def _labeled_join(tasks: list[Task]) -> str:
    parts = []
    for t in tasks:
        mark = "" if t.status == TaskStatus.DONE else " [FAILED]"
        parts.append(f"[{t.assigned_to or t.category}{mark}]: {t.result}")
    return "\n\n".join(parts)


def aggregate(user_message: str, tasks: list[Task],
              llm: LLMClient, tel: Telemetry) -> str:
    done = [t for t in tasks if t.status == TaskStatus.DONE]
    failed = [t for t in tasks if t.status != TaskStatus.DONE]

    if not tasks:
        return "Nothing to do."
    if len(tasks) == 1 and done:
        return tasks[0].result

    with tel.span("aggregator", n=len(tasks), failed=len(failed)):
        try:
            reply = llm.chat(
                [{"role": "system", "content": _AGG_PROMPT},
                 {"role": "user", "content":
                     f"User request: {user_message}\n\n"
                     f"Sub-task results:\n{_labeled_join(tasks)}"}],
                temperature=0.2,
            )
            text = reply.text.strip()
            if text:
                return text
        except LLMError as exc:
            logger.warning("AGGREGATOR | fallback to join: %s", exc)
        return _labeled_join(tasks)
