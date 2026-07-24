"""Tests for the conversation history store — separate from web/telemetry
because it's a distinct persistence surface. Each test uses a temporary
data dir so runs don't pollute each other."""
import os
import tempfile
os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"

import time
import pytest

from orchestra.core.config import settings
from orchestra.observability import history


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yield


def test_create_session_derives_a_title_from_first_message():
    s = history.create_session("Find AI engineer jobs in Riyadh please")
    assert s.title.startswith("Find AI engineer jobs")
    assert s.id and len(s.id) == 12


def test_create_session_handles_blank_input():
    s = history.create_session("   ")
    assert s.title == "New chat"


def test_add_message_touches_session_updated_at():
    s = history.create_session("hi")
    original_updated = s.updated_at
    time.sleep(0.01)
    history.add_message(s.id, "user", "another line")
    s2 = history.get_session(s.id)
    assert s2.updated_at > original_updated


def test_list_messages_returns_in_insertion_order():
    s = history.create_session("hello")
    history.add_message(s.id, "user", "first user")
    history.add_message(s.id, "assistant", "first reply", run_id="r1")
    history.add_message(s.id, "user", "second user")
    msgs = history.list_messages(s.id)
    assert [m.text for m in msgs] == ["first user", "second user"] or \
           [m.role for m in msgs] == ["user", "assistant", "user"]
    # order test — the last one should be "second user"
    assert msgs[-1].text == "second user"
    # run_id preserved for assistant turns only
    assert msgs[1].run_id == "r1"
    assert msgs[0].run_id is None


def test_list_sessions_orders_by_recency():
    a = history.create_session("first chat")
    time.sleep(0.01)
    b = history.create_session("second chat")
    time.sleep(0.01)
    history.add_message(a.id, "user", "bumping first")   # bumps a to the top
    sessions = history.list_sessions()
    assert sessions[0].id == a.id
    assert sessions[1].id == b.id


def test_rename_session_updates_title_and_touches_updated_at():
    s = history.create_session("original title")
    time.sleep(0.01)
    history.rename_session(s.id, "renamed")
    s2 = history.get_session(s.id)
    assert s2.title == "renamed"
    assert s2.updated_at > s.updated_at


def test_rename_session_rejects_blank_and_uses_placeholder():
    s = history.create_session("original")
    history.rename_session(s.id, "   ")
    assert history.get_session(s.id).title == "Untitled"


def test_delete_session_cascades_to_messages():
    s = history.create_session("goodbye chat")
    history.add_message(s.id, "user", "still here")
    history.delete_session(s.id)
    assert history.get_session(s.id) is None
    # messages table should be empty for that session — no orphans
    assert history.list_messages(s.id) == []


def test_get_session_returns_none_for_unknown_id():
    assert history.get_session("does-not-exist") is None


# ── Rewind (the storage half of "regenerate") ─────────────────────
def test_rewind_drops_trailing_assistant_and_returns_user_text():
    s = history.create_session("first question")
    history.add_message(s.id, "user", "first question")
    history.add_message(s.id, "assistant", "first answer", run_id="r1")

    assert history.rewind_to_last_user(s.id) == "first question"

    roles = [m.role for m in history.list_messages(s.id)]
    assert roles == ["user"]          # the reply is gone, the prompt stays


def test_rewind_keeps_earlier_turns_intact():
    s = history.create_session("q1")
    history.add_message(s.id, "user", "q1")
    history.add_message(s.id, "assistant", "a1")
    history.add_message(s.id, "user", "q2")
    history.add_message(s.id, "assistant", "a2")

    assert history.rewind_to_last_user(s.id) == "q2"
    assert [(m.role, m.text) for m in history.list_messages(s.id)] == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"),
    ]


def test_rewind_is_idempotent_when_reply_already_removed():
    s = history.create_session("only a prompt")
    history.add_message(s.id, "user", "only a prompt")

    assert history.rewind_to_last_user(s.id) == "only a prompt"
    assert history.rewind_to_last_user(s.id) == "only a prompt"


def test_rewind_returns_none_for_empty_or_unknown_session():
    s = history.create_session("empty")
    assert history.rewind_to_last_user(s.id) is None
    assert history.rewind_to_last_user("does-not-exist") is None
