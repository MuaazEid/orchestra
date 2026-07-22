"""The company, assembled: plan -> execute -> aggregate.

`Orchestra.handle(message)` is the ONLY entry point the outside world
needs. The CLI, tests, a future API server, or a LangGraph wrapper all
call this one method.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..agents.factory import SpecialistRegistry, default_registry
from ..core.contracts import Task, TaskStatus
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

    @property
    def ok(self) -> bool:
        return all(t.status == TaskStatus.DONE for t in self.tasks)

    def audit(self) -> str:
        lines = [f"run {self.run_id} — {len(self.tasks)} task(s)"]
        for t in self.tasks:
            lines.append(f"  [{t.status.value:>7}] ({t.category}) "
                         f"{t.assigned_to or '-'}: {t.description[:70]}")
        return "\n".join(lines)


@dataclass
class Orchestra:
    registry: SpecialistRegistry = field(default_factory=default_registry)

    def handle(self, message: str) -> RunReport:
        tel = Telemetry.new_run()
        with tel.span("run", input=message[:80]):
            tasks = plan(message, self.registry, get_llm("main"), tel)
            logger.info("RUN %s | %d task(s) planned", tel.run_id, len(tasks))
            tasks = execute_all(tasks, self.registry, tel)
            reply = aggregate(message, tasks, get_llm("fast"), tel)
        return RunReport(reply=reply, tasks=tasks, run_id=tel.run_id)
