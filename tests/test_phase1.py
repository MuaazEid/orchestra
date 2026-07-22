"""Phase 1 tests: config, adapter hardening, mock backend, contracts, telemetry."""
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import pytest

from orchestra.core.config import settings
from orchestra.core.contracts import Task, TaskStatus, SpecialistSpec
from orchestra.llm.adapter import LLMReply, ToolCall, LLMError, _extract_json
from orchestra.llm.backends import MockLLM, get_llm
from orchestra.observability.telemetry import Telemetry, stats_report


# ── Config ─────────────────────────────────────────────────────────
def test_config_reads_env_and_validates():
    assert settings.llm_backend == "mock"
    assert settings.max_concurrency >= 1
    assert settings.data_dir.exists()          # validator created it


# ── JSON hardening (small-model weakness #1) ───────────────────────
@pytest.mark.parametrize("raw", [
    '{"tasks": ["a"]}',
    '```json\n{"tasks": ["a"]}\n```',
    'Sure! Here you go: {"tasks": ["a"]} hope that helps',
])
def test_extract_json_tolerates_messy_output(raw):
    assert _extract_json(raw) == {"tasks": ["a"]}


def test_extract_json_rejects_garbage():
    assert _extract_json("not json at all") is None


def test_chat_json_repairs_then_succeeds():
    llm = MockLLM().queue("garbage!!", '{"tasks": ["fixed"]}')
    out = llm.chat_json([{"role": "user", "content": "plan"}])
    assert out == {"tasks": ["fixed"]}
    # repair loop appended a correction message on retry
    assert len(llm.calls) == 2
    assert "valid JSON" in llm.calls[1]["messages"][-1]["content"]


def test_chat_json_raises_after_exhausted_attempts():
    llm = MockLLM().queue("bad", "still bad")
    with pytest.raises(LLMError):
        llm.chat_json([{"role": "user", "content": "plan"}], max_attempts=2)


# ── Mock backend & factory ─────────────────────────────────────────
def test_mock_records_calls_and_scripts_replies():
    llm = MockLLM().queue(LLMReply(text="", tool_calls=(ToolCall("add", {"a": 1, "b": 2}),)))
    reply = llm.chat([{"role": "user", "content": "1+2"}], tools=[sum])
    assert reply.wants_tools and reply.tool_calls[0].name == "add"
    assert llm.calls[0]["tools"] == ["sum"]


def test_factory_returns_mock_in_test_mode():
    assert isinstance(get_llm("main"), MockLLM)
    assert isinstance(get_llm("fast"), MockLLM)


# ── Contracts ──────────────────────────────────────────────────────
def test_task_lifecycle_and_serialization():
    t = Task(description="multiply 6 by 7", category="math")
    assert t.ready and t.status == TaskStatus.PENDING
    blob = t.model_dump()                       # JSON-safe for graph state
    restored = Task(**blob)
    assert restored.description == "multiply 6 by 7"


def test_specialist_spec_defaults():
    s = SpecialistSpec(name="Math Solver", categories=["math"],
                       system_prompt="solve", tool_names=["multiply"])
    assert s.llm_tier == "main" and s.max_steps == 6


# ── Telemetry ──────────────────────────────────────────────────────
def test_telemetry_records_ok_and_error_spans():
    tel = Telemetry.new_run()
    with tel.span("planner", model="mock"):
        pass
    with pytest.raises(ValueError):
        with tel.span("specialist"):
            raise ValueError("boom")
    report = stats_report()
    assert "planner" in report and "specialist" in report
