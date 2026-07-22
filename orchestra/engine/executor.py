"""Executor — the operations floor.

Dependency-aware scheduler:
- Tasks whose dependencies are satisfied run in WAVES.
- Independent tasks in a wave run CONCURRENTLY (ThreadPoolExecutor,
  bounded by settings.max_concurrency). Honest note for the target
  machine: Ollama largely serializes inference, so concurrency here
  overlaps I/O and keeps the pipeline moving — it is a correctness and
  structure win first, a modest speed win second.
- A failed task is retried up to settings.max_retries times. If it still
  fails, tasks depending on it are marked FAILED (blocked) instead of
  hanging forever.
- Supervisor logic (category -> specialist) is deterministic: dict lookup,
  no LLM guessing. v1's best decision, kept and simplified.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..agents.factory import SpecialistRegistry, run_specialist
from ..core.config import settings
from ..core.contracts import Task, TaskStatus
from ..llm.backends import get_llm
from ..observability.telemetry import Telemetry

logger = logging.getLogger(__name__)


_CTX_CAP = 1200   # a dependency result longer than this is trimmed before
                  # injection — a downstream task never needs the full blob.

# Categories that operate on a NAMED artifact and never consume prior
# content. NOTE: "files" is deliberately NOT here — a save task like
# "save a summary of the fetched content" NEEDS the upstream content
# (lesson from over-correcting: skipping files broke saves). The 1200-char
# trim below is what protects file tasks from ballooning instead.
_NO_CONTEXT_CATEGORIES = {"time", "memory_recall"}


def _needs_no_context(task: Task) -> bool:
    """Surgical rule: a files task whose verb is read/list carries its own
    target (the filename) — upstream content only confuses it. Save/append
    tasks DO need upstream content and are not skipped."""
    if task.category in _NO_CONTEXT_CATEGORIES:
        return True
    if task.category == "files":
        head = task.description.strip().lower()[:20]
        return head.startswith(("read", "list", "show", "open"))
    return False


def _context_for(task: Task, done: dict[str, Task]) -> Task:
    """Inject prerequisite results ONLY when the task actually needs them.

    Skips artifact-oriented tasks (file ops, time, recall) that carry their
    own target in the description. Trims long results so a big upstream
    output can't balloon a downstream task."""
    if not task.depends_on or _needs_no_context(task):
        return task
    parts = []
    for d in task.depends_on:
        if d not in done:
            continue
        res = done[d].result
        if len(res) > _CTX_CAP:
            res = res[:_CTX_CAP] + " ...[trimmed]"
        parts.append(f"- Result of '{done[d].description[:50]}': {res}")
    if not parts:
        return task
    return task.model_copy(update={
        "description": f"{task.description}\n\nContext from earlier steps:\n"
                       + "\n".join(parts)
    })


def execute_all(tasks: list[Task], registry: SpecialistRegistry,
                tel: Telemetry) -> list[Task]:
    """Run every task to a final state (DONE or FAILED). Order of the
    returned list matches the input plan order."""
    by_id: dict[str, Task] = {t.id: t for t in tasks}
    finished: dict[str, Task] = {}

    def _run_one(task: Task) -> Task:
        spec = registry.find_for(task.category) or registry.find_for("general")
        if spec is None:
            return task.model_copy(update={
                "status": TaskStatus.FAILED,
                "result": f"no specialist for category '{task.category}'"})
        llm = get_llm(spec.llm_tier)
        current = _context_for(task, finished)
        for attempt in range(settings.max_retries + 1):
            result = run_specialist(spec, current, llm, tel)
            if result.status == TaskStatus.DONE:
                return result
            logger.warning("EXECUTOR | task %s attempt %d failed: %s",
                           task.id, attempt + 1, result.result)
            current = result.model_copy(update={"status": TaskStatus.PENDING})
        return result  # FAILED after retries

    with tel.span("executor", n_tasks=len(tasks)) as detail:
        pool = ThreadPoolExecutor(max_workers=settings.max_concurrency)
        try:
            while len(finished) < len(by_id):
                wave = [
                    t for t in by_id.values()
                    if t.id not in finished
                    and all(d in finished for d in t.depends_on)
                ]
                if not wave:  # remaining tasks blocked by failed/cyclic deps
                    for t in by_id.values():
                        if t.id not in finished:
                            finished[t.id] = t.model_copy(update={
                                "status": TaskStatus.FAILED,
                                "result": "blocked: unmet dependencies"})
                    break
                # drop tasks whose dependency FAILED — fail fast, clearly
                runnable, blocked = [], []
                for t in wave:
                    if any(finished[d].status == TaskStatus.FAILED
                           for d in t.depends_on):
                        blocked.append(t)
                    else:
                        runnable.append(t)
                for t in blocked:
                    finished[t.id] = t.model_copy(update={
                        "status": TaskStatus.FAILED,
                        "result": "blocked: a prerequisite task failed"})
                if not runnable:
                    continue
                futures = {pool.submit(_run_one, t): t.id for t in runnable}
                for fut in as_completed(futures):
                    finished[futures[fut]] = fut.result()
        finally:
            pool.shutdown(wait=True)
        detail["failed"] = sum(
            1 for t in finished.values() if t.status == TaskStatus.FAILED)

    return [finished[t.id] for t in tasks]
