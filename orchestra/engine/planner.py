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
from ..core.contracts import Task
from ..llm.adapter import LLMClient, LLMError
from ..observability.telemetry import Telemetry

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
        except LLMError as exc:
            logger.warning("PLANNER | degraded to single task: %s", exc)
            raw = []

        if not raw:
            detail["degraded"] = True
            return [Task(description=user_message, category="general")]

        tasks: list[Task] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("description"):
                continue
            cat = item.get("category", "general")
            cat = cat if cat in cats else "general"
            cat = _correct_category(str(item["description"]), cat, cats)
            tasks.append(Task(
                description=str(item["description"]),
                category=cat,
            ))
        # resolve integer "needs" -> task ids (after all ids exist)
        for i, item in enumerate(raw):
            if i >= len(tasks) or not isinstance(item, dict):
                continue
            needs = item.get("needs") or []
            tasks[i].depends_on = [
                tasks[j].id for j in needs
                if isinstance(j, int) and 0 <= j < len(tasks) and j != i
            ]
        detail["n_tasks"] = len(tasks)
        return tasks or [Task(description=user_message, category="general")]
