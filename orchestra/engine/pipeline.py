"""The company, assembled: plan -> execute -> aggregate.

`Orchestra.handle(message)` is the ONLY entry point the outside world
needs. The CLI, tests and the web server all call this one method, so
behaviour (progress events, error handling) cannot drift between them.

`handle` never raises: a backend that is down or an unexpected bug becomes
a RunReport with `error` set and a reply that says what to do, so a caller
always has something honest to show the user.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ..agents.factory import SpecialistRegistry, default_registry
from ..core.contracts import Task, TaskStatus
from ..llm.adapter import LLMUnavailable
from ..llm.backends import get_llm
from ..observability.telemetry import Telemetry
from .aggregator import aggregate
from .executor import execute_all
from .planner import plan

logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    reply: str
    tasks: list[Task]
    run_id: str
    error: str | None = None   # set when the run could not complete at all

    @property
    def ok(self) -> bool:
        return self.error is None and all(
            t.status == TaskStatus.DONE for t in self.tasks)

    def audit(self) -> str:
        lines = [f"run {self.run_id} — {len(self.tasks)} task(s)"]
        for t in self.tasks:
            lines.append(f"  [{t.status.value:>7}] ({t.category}) "
                         f"{t.assigned_to or '-'}: {t.description[:70]}")
        if self.error:
            lines.append(f"  run error: {self.error}")
        return "\n".join(lines)


@dataclass
class Orchestra:
    registry: SpecialistRegistry = field(default_factory=default_registry)

    def handle(self, message: str,
               on_event: Callable[[str], None] | None = None) -> RunReport:
        """Run one request. `on_event` receives short real progress strings
        (planning, routing, per-specialist completion); it may be called
        from worker threads."""
        emit = on_event or (lambda _msg: None)
        tel = Telemetry.new_run()
        try:
            with tel.span("run", input=message[:80]):
                emit("Planning your request…")
                tasks = plan(message, self.registry, get_llm("main"), tel)
                cats = ", ".join(t.category for t in tasks) or "nothing to do"
                emit(f"Planned {len(tasks)} task(s): {cats}")
                logger.info("RUN %s | %d task(s) planned", tel.run_id, len(tasks))
                tasks = execute_all(tasks, self.registry, tel, on_event=emit)
                emit("Composing the final reply…")
                reply = aggregate(message, tasks, get_llm("fast"), tel)
        except LLMUnavailable as exc:
            logger.error("RUN %s | backend unavailable: %s", tel.run_id, exc)
            return RunReport(reply=f"⚠️ {exc}", tasks=[],
                             run_id=tel.run_id, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - last-resort boundary
            logger.exception("RUN %s | unexpected failure", tel.run_id)
            return RunReport(
                reply=("⚠️ Something went wrong while handling this "
                       f"request (run {tel.run_id}). The details are in the log."),
                tasks=[], run_id=tel.run_id, error=repr(exc))
        return RunReport(reply=reply, tasks=tasks, run_id=tel.run_id)
