"""Telemetry — because 'it feels fast' is not engineering.

Every span (planner run, specialist run, tool call) is timed and written to
SQLite on the target machine. `orchestra stats` reads it back so Muaaz sees
REAL numbers: success rate, p50/p95 latency, error counts per node.

Zero external services. Zero cost. One file-based DB.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from ..core.config import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,          -- ok | error
    started_at REAL NOT NULL,
    duration_s REAL NOT NULL,
    detail TEXT                    -- JSON blob (model, task, error, ...)
);
CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id);
"""


def _conn() -> sqlite3.Connection:
    # Fresh connection per call: sidesteps SQLite threading issues
    # (lesson #12 from the v1 error log).
    c = sqlite3.connect(settings.metrics_db, timeout=10)
    c.executescript(_SCHEMA)
    return c


@dataclass
class Telemetry:
    run_id: str

    @classmethod
    def new_run(cls) -> "Telemetry":
        return cls(run_id=uuid.uuid4().hex[:12])

    @contextmanager
    def span(self, name: str, **detail):
        t0 = time.perf_counter()
        status = "ok"
        try:
            yield detail  # nodes may add keys, e.g. detail["model"] = ...
        except Exception as exc:
            status = "error"
            detail["error"] = repr(exc)[:500]
            raise
        finally:
            dur = time.perf_counter() - t0
            try:
                with _conn() as c:
                    c.execute(
                        "INSERT INTO spans VALUES (?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, self.run_id, name, status,
                         time.time(), dur, json.dumps(detail, default=str)),
                    )
            except sqlite3.Error as exc:  # telemetry must never kill a run
                logger.warning("telemetry write failed: %s", exc)
            logger.info("SPAN %-22s %-5s %6.2fs", name, status, dur)


def stats_report() -> str:
    """Human-readable report over all recorded spans."""
    with _conn() as c:
        rows = c.execute("""
            SELECT name,
                   COUNT(*)                                   AS n,
                   SUM(status = 'ok')                         AS ok,
                   ROUND(AVG(duration_s), 2)                  AS avg_s,
                   ROUND(MAX(duration_s), 2)                  AS max_s
            FROM spans GROUP BY name ORDER BY n DESC
        """).fetchall()
    if not rows:
        return "No telemetry recorded yet."
    lines = [f"{'node':<24}{'runs':>6}{'ok%':>7}{'avg_s':>8}{'max_s':>8}"]
    for name, n, ok, avg_s, max_s in rows:
        lines.append(f"{name:<24}{n:>6}{100 * ok / n:>6.0f}%{avg_s:>8}{max_s:>8}")
    return "\n".join(lines)
