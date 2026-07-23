"""Core contracts — the company's org chart, as types.

v1 lesson applied: vague strings caused half the bugs (vague tasks,
name mismatches, overwritten results). So v2 makes everything a TYPED
contract validated by pydantic. A Task is not a string anymore — it is
an object with an id, a category, explicit inputs, status and result.
"""
from __future__ import annotations

import operator
import uuid
from enum import Enum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Task(BaseModel):
    """One unit of work. Small, explicit, self-contained."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    description: str                      # explicit, value-filled instruction
    category: str = "general"             # matched against specialist skills
    depends_on: list[str] = []            # task ids that must finish first
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    attempts: int = 0
    assigned_to: str = ""                 # specialist name (filled by supervisor)

    @property
    def ready(self) -> bool:
        return self.status == TaskStatus.PENDING


class SpecialistSpec(BaseModel):
    """A job description: what a specialist is, knows, and may use."""
    name: str
    categories: list[str]                 # task categories it accepts
    system_prompt: str
    tool_names: list[str] = []
    llm_tier: str = "main"                # 'fast' for trivial skills
    max_steps: int = 6
    # One-line "when to route to me" — injected into the planner's prompt
    # automatically. New hires teach the planner about themselves; nobody
    # edits the planner by hand.
    hint: str = ""
    # Tools whose successful run COMPLETES the task immediately — no extra
    # LLM turn for a verbal "confirmation". Breaks small-model tool loops
    # deterministically (Phase 5 fix). The tool's own output becomes the
    # result, so the user still sees what happened.
    terminal_tools: list[str] = []
    # Tools whose FAILURE ends the task immediately as FAILED, verbatim —
    # for calls a retry can't fix (missing API key, network/service down).
    # Without this, a small model that sees an error sometimes "helpfully"
    # invents a fake result instead of reporting the failure (observed live
    # with Job Scout + a missing Tavily key: it fabricated example.com job
    # postings). Argument-shape errors are NOT fatal — those stay retriable
    # so the model can self-correct using the corrected-signature hint.
    fatal_tools: list[str] = []


class OrchestraState(TypedDict, total=False):
    """State flowing through the graph.

    Reducer discipline (v1 lesson #14):
      - `results` and `events` APPEND (operator.add)
      - everything else overwrites intentionally
    """
    messages: Annotated[list, operator.add]
    tasks: list[dict]                     # serialized Task objects (JSON-safe)
    results: Annotated[list[str], operator.add]
    events: Annotated[list[str], operator.add]   # audit trail for the user
    run_meta: dict[str, Any]              # run_id, timings, model info
