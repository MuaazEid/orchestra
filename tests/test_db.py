"""The shared SQLite helper: connections are closed, transactions behave,
and the stores built on it do not leave unclosed connections behind."""
import gc
import os
import warnings

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"

import pytest
import sqlite3

from orchestra.core.config import settings
from orchestra.core.db import connect
from orchestra.observability import history
from orchestra.observability.telemetry import Telemetry, stats_report

SCHEMA = "CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT);"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)


def test_connection_is_closed_after_the_block(tmp_path):
    with connect(tmp_path / "a.db", SCHEMA) as c:
        c.execute("INSERT INTO t VALUES ('a', '1')")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_commit_on_success_and_rollback_on_error(tmp_path):
    db = tmp_path / "a.db"
    with connect(db, SCHEMA) as c:
        c.execute("INSERT INTO t VALUES ('kept', '1')")
    with pytest.raises(RuntimeError):
        with connect(db, SCHEMA) as c:
            c.execute("INSERT INTO t VALUES ('lost', '2')")
            raise RuntimeError("boom")
    with connect(db, SCHEMA) as c:
        keys = [r[0] for r in c.execute("SELECT k FROM t")]
    assert keys == ["kept"]


def test_wal_mode_and_foreign_keys_are_enabled(tmp_path):
    with connect(tmp_path / "a.db", SCHEMA) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_recreates_schema_if_the_file_is_deleted(tmp_path):
    db = tmp_path / "a.db"
    with connect(db, SCHEMA) as c:
        c.execute("INSERT INTO t VALUES ('a', '1')")
    for f in tmp_path.glob("a.db*"):
        f.unlink()
    with connect(db, SCHEMA) as c:
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_stores_leave_no_unclosed_connections():
    """Python 3.13 emits a ResourceWarning for every sqlite connection that
    is garbage-collected without close(). The old `with sqlite3.connect()`
    pattern did exactly that on every call."""
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always", ResourceWarning)
        s = history.create_session("hello")
        history.add_message(s.id, "user", "hi")
        history.list_messages(s.id)
        history.list_sessions()
        history.delete_session(s.id)
        tel = Telemetry.new_run()
        with tel.span("probe"):
            pass
        stats_report()
        gc.collect()
    leaks = [w for w in seen if issubclass(w.category, ResourceWarning)
             and "sqlite" in str(w.message).lower()]
    assert not leaks, [str(w.message) for w in leaks]
