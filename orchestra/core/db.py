"""One way to open SQLite, used by every store (history, telemetry, memory).

Why this exists: `with sqlite3.connect(...) as c:` commits the transaction
but does NOT close the connection. Every call used to leak a connection
(and on Windows keep the file locked until garbage collection). This helper:

- closes the connection when the block ends (and commits / rolls back),
- turns on WAL mode so readers and writers from different threads do not
  block each other ("database is locked"),
- sets a busy timeout and foreign-key enforcement,
- runs the schema script (idempotent `IF NOT EXISTS`) on every open, so a
  deleted or fresh database file is always usable.

A fresh connection per call stays the rule: sqlite3 connections must not be
shared across threads.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_BUSY_MS = 10_000


@contextmanager
def connect(path: Path, schema: str = "") -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_BUSY_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # e.g. a filesystem without shared-memory support: stay on the default
        if schema:
            conn.executescript(schema)
        with conn:  # one transaction: commit on success, rollback on error
            yield conn
    finally:
        conn.close()
