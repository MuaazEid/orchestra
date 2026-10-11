"""Planner — the project manager.

v1 lessons applied:
- #15 vague tasks   -> prompt demands EXACT values, and each task is a typed
                       Task with a category the company actually has.
- #17 dropped facts -> "each personal fact = separate task" is a hard rule.
- The planner reads the specialist CATALOG, so it can never plan work the
  company cannot do — unknown categories are coerced to 'general'.
- If the LLM output is broken beyond repair (chat_json already retries),
  we degrade to ONE general task with the raw message. The user always
  gets an answer.
"""
from __future__ import annotations

import logging

from ..agents.factory import SpecialistRegistry
from ..core.config import settings
from ..core.contracts import Task
from ..llm.adapter import LLMClient, LLMError, LLMUnavailable
from ..observability.telemetry import Telemetry
from .guardrails import PlanItem, break_cycles, needs_planner, sanitize_plan

logger = logging.getLogger(__name__)

_PLANNER_PROMPT = """You are the planning manager of a multi-agent company.
Break the user's message into the smallest independent sub-tasks.

The company's staff and their categories:
{catalog}

What each category means (match intent, not surface words):
{category_hints}
- general: only when nothing above fits.

Rules:
1. Use ONLY these categories: {categories}. Anything else -> "general".
2. Each task description must be self-contained with EXACT values copied
   from the user's message (names, numbers, texts). Never write "the number"
   or "the user's name" — write the actual value.
3. Every personal fact (name, likes, origin, job...) is a SEPARATE
   memory_save task.
4. If task B needs task A's output, list A's index in B's "needs".
   Independent tasks must have an empty "needs" list.
5. A simple message = one task. Do not invent work.

Examples:
User: "What is my favorite programming language?"
{{"tasks": [{{"description": "Retrieve the user's favorite programming language", "category": "memory_recall", "needs": []}}]}}

User: "How many times does the letter s appear in 'mississippi'?"
{{"tasks": [{{"description": "Count occurrences of the letter s in 'mississippi'", "category": "text_analysis", "needs": []}}]}}

User: "Multiply 25 by 17"
{{"tasks": [{{"description": "Multiply 25 by 17", "category": "math", "needs": []}}]}}

Reply with ONLY this JSON:
{{"tasks": [{{"description": "...", "category": "...", "needs": [0]}}]}}"""


# Deterministic backstop for the LLM's two most common misroutes.
# Signals are strong and unambiguous; when in doubt we leave the LLM's choice.
def _correct_category(description: str, cat: str, cats: list[str]) -> str:
    d = description.lower()
    # counting letters/characters/words is text_analysis, never math
    if ("text_analysis" in cats and cat in {"math", "general"}
            and any(k in d for k in ("letter", "character", "how many times",
                                     "occurrenc", "appear", "count the word",
                                     "word count", "how many words"))):
        return "text_analysis"
    # questions about the user's own stored info are memory_recall
    if ("memory_recall" in cats and cat == "general"
            and any(k in d for k in ("my favorite", "my name", "remember my",
                                     "user's favorite", "user's name",
                                     "about myself", "do you remember"))):
        return "memory_recall"
    return cat


def plan(user_message: str, registry: SpecialistRegistry,
         llm: LLMClient, tel: Telemetry) -> list[Task]:
    cats = registry.all_categories

    # Fast path: no sign of any specialist in the message -> skip the
    # planner call. Nothing to route, nothing to hallucinate.
    if settings.planner_fast_path and not needs_planner(user_message):
        with tel.span("planner", input=user_message[:80]) as detail:
            detail["fast_path"] = True
        return [Task(description=user_message, category="general")]

    system = _PLANNER_PROMPT.format(catalog=registry.catalog(),
                                    category_hints=registry.hints(),
                                    categories=", ".join(cats))
    with tel.span("planner", input=user_message[:80]) as detail:
        try:
            out = llm.chat_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user_message}],
                schema_hint='{"tasks": [{"description": str, "category": str, "needs": [int]}]}',
            )
            raw = out.get("tasks") or []
        except LLMUnavailable:
            raise  # backend down: degrading to a "general" task cannot help
        except LLMError as exc:
            logger.warning("PLANNER | degraded to single task: %s", exc)
            raw = []

        if not isinstance(raw, list) or not raw:
            detail["degraded"] = True
            return [Task(description=user_message, category="general")]

        items: list[PlanItem] = []
        for ref, item in enumerate(raw):
            if not isinstance(item, dict) or not item.get("description"):
                continue  # `ref` still counts it, so `needs` indexes stay aligned
            cat = item.get("category", "general")
            cat = cat if cat in cats else "general"
            desc = str(item["description"])
            needs = item.get("needs") or []
            items.append(PlanItem(
                ref=ref, description=desc,
                category=_correct_category(desc, cat, cats),
                needs=[n for n in needs if isinstance(n, int) and n != ref]
                if isinstance(needs, list) else []))

        before = len(items)
        items = sanitize_plan(items, user_message)
        detail["sanitized"] = before != len(items) or any(
            it.description == user_message for it in items)

        tasks = [Task(description=it.description, category=it.category)
                 for it in items]
        id_of = {it.ref: t.id for it, t in zip(items, tasks)}
        deps = {t.id: [id_of[n] for n in it.needs if n in id_of]
                for it, t in zip(items, tasks)}
        deps = break_cycles(deps, [t.id for t in tasks])
        for t in tasks:
            t.depends_on = deps[t.id]
        detail["n_tasks"] = len(tasks)
        return tasks
