"""Deterministic guardrails around the planner's LLM output.

A 7B planner invents work: asked a plain question it will happily emit a
`memory_save` or `web` task that nothing in the message justifies, and a
tool-less request then fails (or fabricates) inside a tool specialist.
Prompts cannot fully prevent that on a small model, so these checks run in
plain code, before any task executes:

1. needs_planner()  - if the message has no signal for ANY specialist, skip
                      the planner call entirely and answer with the general
                      assistant (one LLM call saved, nothing to hallucinate).
2. sanitize_plan()  - demote a task whose category the message gives no
                      evidence for, dedupe, cap the plan size, and fall
                      back to one general task when nothing real is left.
3. break_cycles()   - a plan whose dependencies loop could never finish.

Signals are deliberately generous (English and Arabic): a false "yes" only
costs one extra planner call, a false "no" would drop real work.
Categories without an entry in _GUARDED (e.g. `writing`, or specialists
added later) pass through untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_PLAN_TASKS = 8

_FLAGS = re.I | re.U

_SIGNALS: dict[str, re.Pattern[str]] = {
    "web": re.compile(r"https?://|www\.", _FLAGS),
    "files": re.compile(
        r"\b(?:files?|folders?|director(?:y|ies)|workspace|save|append|notes?)\b"
        r"|\.(?:txt|md|csv|json|log)\b|ملف|مجلد|احفظ|خزن", _FLAGS),
    "memory": re.compile(
        r"\b(?:my|mine|myself|i am|i'm|i like|i love|i prefer|i live|i work|"
        r"remember|recall|forget|favou?rite|call me|about me)\b"
        r"|اسمي|أنا|انا|تذكر|تتذكر|المفضل|مفضل|أحب|احب|عمري|أعيش|اعيش|عني|نفسي", _FLAGS),
    "math": re.compile(
        r"\d|\b(?:add|plus|minus|subtract|multipl\w*|divid\w*|times|sum|product|"
        r"calculate|percent\w*|total|zero|one|two|three|four|five|six|seven|"
        r"eight|nine|ten|hundred|thousand|dozen|half|double|triple)\b"
        r"|اضرب|اجمع|احسب|اطرح|قسم|ضرب|جمع|طرح", _FLAGS),
    "text": re.compile(
        r"\b(?:letters?|characters?|words?|count\w*|how many|vowels?|spell\w*|"
        r"occurrences?|appears?)\b|حرف|حروف|كلمة|كلمات|كم عدد|عدد", _FLAGS),
    "time": re.compile(
        r"\b(?:time|date|today|tonight|now|clock|day|month|year|o'clock)\b"
        r"|الساعة|الوقت|التاريخ|اليوم|الآن|الان", _FLAGS),
    "writing": re.compile(
        r"\b(?:write|compose|draft|poem|haiku|story|email|letter|essay|"
        r"summar\w*|rewrite|translate|paragraph|caption|slogan|tweet)\b"
        r"|اكتب|قصيدة|قصة|لخص|ترجم|رسالة|مقال", _FLAGS),
}

# category -> the signal group that must be present in the user's message
_GUARDED: dict[str, str] = {
    "web": "web", "files": "files",
    "memory_save": "memory", "memory_recall": "memory",
    "math": "math", "text_analysis": "text", "time": "time",
}


def signals(message: str) -> set[str]:
    return {name for name, rx in _SIGNALS.items() if rx.search(message)}


def needs_planner(message: str) -> bool:
    """False when the message shows no sign of needing any specialist."""
    return bool(signals(message))


@dataclass
class PlanItem:
    """One planner entry, before ids exist. `ref` is the entry's index in
    the planner's raw list — what other entries' `needs` point at."""
    ref: int
    description: str
    category: str
    needs: list[int] = field(default_factory=list)


def sanitize_plan(items: list[PlanItem], message: str) -> list[PlanItem]:
    present = signals(message)

    # 1. demote categories the message gives no evidence for
    for it in items:
        group = _GUARDED.get(it.category)
        if group and group not in present:
            it.category = "general"

    # 2. cap the plan size
    items = items[:MAX_PLAN_TASKS]

    # 3. dedupe identical work, redirecting dependencies to the survivor
    seen: dict[tuple[str, str], int] = {}
    redirect: dict[int, int] = {}
    kept: list[PlanItem] = []
    for it in items:
        key = (it.category, " ".join(it.description.lower().split()))
        if key in seen:
            redirect[it.ref] = seen[key]
        else:
            seen[key] = it.ref
            kept.append(it)
    live = {it.ref for it in kept}
    for it in kept:
        it.needs = sorted({redirect.get(n, n) for n in it.needs
                           if redirect.get(n, n) in live and redirect.get(n, n) != it.ref})

    # 4. nothing but "general" left -> one task carrying the user's own words
    if not kept or all(it.category == "general" for it in kept):
        return [PlanItem(ref=0, description=message, category="general")]
    return kept


def break_cycles(depends: dict[str, list[str]], order: list[str]) -> dict[str, list[str]]:
    """Return `depends` with cycles removed. Any dependency on an entry at
    the same or a later position in `order` is dropped when it is part of a
    cycle, which leaves only backward edges (acyclic by construction)."""
    pos = {tid: i for i, tid in enumerate(order)}
    remaining = {t: set(d) for t, d in depends.items()}
    # Kahn's algorithm: peel off tasks whose dependencies are all resolved
    resolved: set[str] = set()
    progress = True
    while progress:
        progress = False
        for t, deps in remaining.items():
            if t not in resolved and deps <= resolved:
                resolved.add(t)
                progress = True
    cyclic = set(remaining) - resolved
    if not cyclic:
        return depends
    out = {}
    for t, deps in depends.items():
        if t in cyclic:
            out[t] = [d for d in deps if not (d in cyclic and pos[d] >= pos[t])]
        else:
            out[t] = list(deps)
    return out
