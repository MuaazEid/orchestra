"""Built-in tools — the employees' actual equipment.

Same capabilities as v1 (memory, math, counting, time) but each tool is
pure, documented, and registered by decorator. User memory is SQLite with
a fresh connection per call (v1 lesson #12: never share connections
across threads).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from ..core.config import settings
from .toolbox import tool

_MEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _mem() -> sqlite3.Connection:
    c = sqlite3.connect(settings.memory_db, timeout=10)
    c.executescript(_MEM_SCHEMA)
    return c


# ── Memory ─────────────────────────────────────────────────────────
@tool
def remember_about_user(key: str, fact: str) -> str:
    """Save one fact about the user. key: short label like 'name' or
    'favorite_language'. fact: the exact information to store."""
    with _mem() as c:
        c.execute(
            "INSERT INTO facts VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, fact, datetime.now().isoformat(timespec="seconds")),
        )
    return f"Saved: {key} = {fact}"


@tool
def recall_about_user(query: str = "") -> str:
    """Retrieve stored facts about the user. query: optional keyword filter;
    empty returns everything."""
    with _mem() as c:
        rows = c.execute("SELECT key, value FROM facts ORDER BY key").fetchall()
    if not rows:
        return "No stored facts found."
    if query:
        # Word-level matching: any query word hitting key or value counts.
        # ("favorite programming language" must match key "favorite_language")
        words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if len(w) > 2]
        hits = [r for r in rows
                if any(w in r[0].lower() or w in r[1].lower() for w in words)]
        # A miss on a SMALL store means our filter failed, not the data:
        # return everything and let the model pick the relevant fact.
        rows = hits or rows
    return "\n".join(f"- {k}: {v}" for k, v in rows)


# ── Math ───────────────────────────────────────────────────────────
@tool
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@tool
def multiply(a: float, b: float) -> float:
    """Multiply two numbers and return the product."""
    return a * b


@tool
def divide(a: float, b: float) -> float:
    """Divide a by b. Returns an error message if b is zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


# ── Text analysis ──────────────────────────────────────────────────
@tool
def count_letters(text: str, letter: str) -> int:
    """Count how many times `letter` appears in `text` (case-insensitive)."""
    return text.lower().count(letter.lower())


@tool
def word_count(text: str) -> int:
    """Count the number of words in `text`."""
    return len(text.split())


# ── Time ───────────────────────────────────────────────────────────
@tool
def get_current_time() -> str:
    """Return the current local date and time."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
