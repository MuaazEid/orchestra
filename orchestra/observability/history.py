"""Conversation history — durable chat sessions across restarts.

Design choices, consistent with the rest of Orchestra:
- SQLite, one file, zero external services. Same store philosophy as
  telemetry.py (fresh connection per call — no threading footguns).
- One "session" = one continuing chat. Sessions have short auto-generated
  IDs and human-readable titles (derived from the first message).
- Messages are stored in insertion order; a run_id ties each assistant
  turn back to the telemetry span, so /audit stays useful across sessions.
- The wire model here is deliberately small — the same shape the browser
  sees. If it doesn't help the UI or the audit trail, it's not stored.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass

from ..core.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,     -- 'user' | 'assistant'
    text        TEXT NOT NULL,
    run_id      TEXT,              -- present on assistant turns only
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msgs_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


def _db_path():
    # Sit next to metrics.db under the data dir — same convention as telemetry.
    return settings.data_dir / "conversations.db"


def _conn() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(_SCHEMA)
    return c


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _derive_title(first_message: str) -> str:
    # Trim whitespace, cap length, strip a trailing punctuation cluster —
    # nothing clever. A model-written title would look better but would
    # burn a call on every new session, and this is called synchronously.
    t = " ".join(first_message.split())[:60].rstrip(".!?،؟ ")
    return t or "New chat"


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Message:
    id: str
    session_id: str
    role: str
    text: str
    run_id: str | None
    created_at: float


def create_session(first_message: str) -> Session:
    now = time.time()
    sid = _short_id()
    title = _derive_title(first_message)
    with _conn() as c:
        c.execute("INSERT INTO sessions(id, title, created_at, updated_at) "
                  "VALUES(?, ?, ?, ?)", (sid, title, now, now))
    return Session(id=sid, title=title, created_at=now, updated_at=now)


def list_sessions(limit: int = 100) -> list[Session]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, created_at, updated_at FROM sessions "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [Session(*r) for r in rows]


def get_session(session_id: str) -> Session | None:
    with _conn() as c:
        r = c.execute(
            "SELECT id, title, created_at, updated_at FROM sessions "
            "WHERE id = ?", (session_id,)).fetchone()
    return Session(*r) if r else None


def list_messages(session_id: str) -> list[Message]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, session_id, role, text, run_id, created_at "
            "FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,)).fetchall()
    return [Message(*r) for r in rows]


def add_message(session_id: str, role: str, text: str,
                run_id: str | None = None) -> Message:
    assert role in ("user", "assistant")
    now = time.time()
    mid = _short_id()
    with _conn() as c:
        c.execute(
            "INSERT INTO messages(id, session_id, role, text, run_id, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (mid, session_id, role, text, run_id, now))
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                  (now, session_id))
    return Message(id=mid, session_id=session_id, role=role, text=text,
                   run_id=run_id, created_at=now)


def rename_session(session_id: str, title: str) -> None:
    with _conn() as c:
        c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                  (title[:120].strip() or "Untitled", time.time(), session_id))


def delete_session(session_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
