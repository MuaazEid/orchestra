"""Planner guardrails: no invented tool tasks, aligned dependencies, no
cycles, bounded plans."""
import json
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import pytest

from orchestra.agents.factory import default_registry
from orchestra.core.config import settings
from orchestra.engine.guardrails import (MAX_PLAN_TASKS, break_cycles,
                                         needs_planner, sanitize_plan, signals)
from orchestra.engine.planner import plan
from orchestra.llm.backends import MockLLM
from orchestra.observability.telemetry import Telemetry

REG = default_registry()


def _plan(message, tasks_json):
    llm = MockLLM().queue(json.dumps({"tasks": tasks_json}))
    tasks = plan(message, REG, llm, Telemetry.new_run())
    return tasks, llm


# ── Fast path ──────────────────────────────────────────────────────
@pytest.mark.parametrize("msg", [
    "hello there",
    "Explain what an API is",
    "ما هي عاصمة فرنسا",
    "Tell me a joke about programmers",
])
def test_messages_without_any_specialist_signal_skip_the_planner(msg):
    assert not needs_planner(msg)
    llm = MockLLM()
    tasks = plan(msg, REG, llm, Telemetry.new_run())
    assert llm.calls == []                       # zero LLM calls
    assert len(tasks) == 1 and tasks[0].category == "general"
    assert tasks[0].description == msg           # the user's own words


@pytest.mark.parametrize("msg,group", [
    ("Multiply 25 by 17", "math"),
    ("My name is Muaaz", "memory"),
    ("اسمي معاذ", "memory"),
    ("Summarize https://example.com/a", "web"),
    ("save this into notes.txt", "files"),
    ("What time is it?", "time"),
    ("كم عدد الحروف في هذه الكلمة", "text"),
    ("Write a haiku about the moon", "writing"),
])
def test_real_requests_have_signals_and_reach_the_planner(msg, group):
    assert group in signals(msg)
    assert needs_planner(msg)


def test_fast_path_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "planner_fast_path", False)
    tasks, llm = _plan("hello there",
                       [{"description": "greet", "category": "general", "needs": []}])
    assert len(llm.calls) == 1


# ── Demotion of invented work ──────────────────────────────────────
def test_invented_memory_tasks_are_demoted_and_collapsed_to_the_users_words():
    # "3" makes this reach the planner, but nothing in the message is a
    # personal fact or a recall question, so memory_* tasks are invented.
    msg = "Explain why the build fails with 3 errors"
    tasks, _ = _plan(msg, [
        {"description": "Remember the build fails", "category": "memory_save", "needs": []},
        {"description": "Look up stored build notes", "category": "memory_recall", "needs": []},
        {"description": "Explain the build failure", "category": "general", "needs": [0, 1]},
    ])
    assert len(tasks) == 1
    assert tasks[0].category == "general" and tasks[0].description == msg


def test_web_task_without_a_url_is_demoted_but_kept_when_a_url_exists():
    msg = "Tell me about 3 recent papers on agents"
    tasks, _ = _plan(msg, [{"description": "Fetch latest papers", "category": "web", "needs": []}])
    assert [t.category for t in tasks] == ["general"]

    msg = "Summarize https://example.com/post in 3 lines"
    tasks, _ = _plan(msg, [{"description": "Fetch https://example.com/post", "category": "web", "needs": []},
                           {"description": "Summarize it in 3 lines", "category": "writing", "needs": [0]}])
    assert [t.category for t in tasks] == ["web", "writing"]
    assert tasks[1].depends_on == [tasks[0].id]


def test_a_legitimate_compound_plan_is_left_alone():
    msg = "My name is Muaaz. Multiply 6 by 7, and write a one-line haiku about the moon."
    tasks, _ = _plan(msg, [
        {"description": "Save the user's name: Muaaz", "category": "memory_save", "needs": []},
        {"description": "Multiply 6 by 7", "category": "math", "needs": []},
        {"description": "Write a one-line haiku about the moon", "category": "writing", "needs": []},
    ])
    assert [t.category for t in tasks] == ["memory_save", "math", "writing"]


# ── Structure ──────────────────────────────────────────────────────
def test_skipped_invalid_entries_do_not_misalign_dependencies():
    msg = "Fetch https://example.com then multiply 2 by 3"
    tasks, _ = _plan(msg, [
        {"description": "Fetch https://example.com", "category": "web", "needs": []},
        {"category": "math"},                                   # invalid: no description
        {"description": "Multiply 2 by 3", "category": "math", "needs": [0]},
    ])
    assert len(tasks) == 2
    assert tasks[1].depends_on == [tasks[0].id]   # pointed at raw[0], not "whatever is at index 0 after skipping"


def test_duplicate_tasks_are_merged_and_dependencies_redirected():
    msg = "multiply 2 by 3 then write it as a haiku"
    tasks, _ = _plan(msg, [
        {"description": "Multiply 2 by 3", "category": "math", "needs": []},
        {"description": "multiply 2  by 3", "category": "math", "needs": []},
        {"description": "Write a haiku about the result", "category": "writing", "needs": [1]},
    ])
    assert [t.category for t in tasks] == ["math", "writing"]
    assert tasks[1].depends_on == [tasks[0].id]


def test_plan_size_is_capped():
    msg = "count the words in 20 different sentences"
    raw = [{"description": f"Count words in sentence {i}", "category": "text_analysis", "needs": []}
           for i in range(20)]
    tasks, _ = _plan(msg, raw)
    assert len(tasks) == MAX_PLAN_TASKS


def test_dependency_cycles_are_broken_so_the_plan_can_finish():
    msg = "multiply 2 by 3 and add 4 and 5"
    tasks, _ = _plan(msg, [
        {"description": "Multiply 2 by 3", "category": "math", "needs": [1]},
        {"description": "Add 4 and 5", "category": "math", "needs": [0]},
    ])
    assert len(tasks) == 2
    # at most one direction survives -> acyclic
    a, b = tasks
    assert not (b.id in a.depends_on and a.id in b.depends_on)


def test_break_cycles_leaves_acyclic_graphs_untouched():
    g = {"a": [], "b": ["a"], "c": ["a", "b"]}
    assert break_cycles(g, ["a", "b", "c"]) == g


def test_self_and_unknown_references_are_ignored():
    tasks, _ = _plan("add 1 and 2", [
        {"description": "Add 1 and 2", "category": "math", "needs": [0, 7, "x"]}])
    assert tasks[0].depends_on == []


def test_garbage_planner_output_degrades_to_one_general_task():
    llm = MockLLM().queue('{"tasks": "not a list"}')
    tasks = plan("add 1 and 2", REG, llm, Telemetry.new_run())
    assert len(tasks) == 1 and tasks[0].category == "general"


def test_sanitize_never_returns_an_empty_plan():
    out = sanitize_plan([], "anything")
    assert len(out) == 1 and out[0].category == "general"
