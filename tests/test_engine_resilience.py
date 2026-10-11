"""Failure handling in the engine: no retries for permanent failures, no
run-killing crashes, and a pipeline that always returns something honest."""
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"


from orchestra.agents.factory import SpecialistRegistry, run_specialist
from orchestra.agents.toolbox import tool
from orchestra.core.config import settings
from orchestra.core.contracts import SpecialistSpec, Task, TaskStatus
from orchestra.engine import executor, pipeline
from orchestra.engine.pipeline import Orchestra
from orchestra.llm.adapter import LLMClient, LLMError, LLMReply, LLMUnavailable, ToolCall
from orchestra.llm.backends import MockLLM
from orchestra.observability.telemetry import Telemetry


class _AlwaysDown(LLMClient):
    calls = 0

    def chat(self, messages, **kw):
        type(self).calls += 1
        raise LLMUnavailable("Ollama is not reachable at http://x")


def _one_spec_registry(**spec_kw):
    reg = SpecialistRegistry()
    reg.register(SpecialistSpec(name="Solo", categories=["x"],
                                system_prompt="do it", **spec_kw))
    return reg


# ── Specialist boundary ────────────────────────────────────────────
def test_backend_down_marks_the_task_fatal_with_the_actionable_message():
    spec = _one_spec_registry().find_for("x")
    task = run_specialist(spec, Task(description="t", category="x"),
                          _AlwaysDown(), Telemetry.new_run())
    assert task.status == TaskStatus.FAILED and task.fatal
    assert "not reachable" in task.result


def test_plain_llm_error_stays_retriable():
    class Flaky(LLMClient):
        def chat(self, messages, **kw):
            raise LLMError("gave up")
    spec = _one_spec_registry().find_for("x")
    task = run_specialist(spec, Task(description="t", category="x"),
                          Flaky(), Telemetry.new_run())
    assert task.status == TaskStatus.FAILED and not task.fatal


# ── Executor ───────────────────────────────────────────────────────
def test_executor_does_not_retry_a_fatal_failure(monkeypatch):
    _AlwaysDown.calls = 0
    monkeypatch.setattr(executor, "get_llm", lambda tier: _AlwaysDown())
    monkeypatch.setattr(settings, "max_retries", 3)
    out = executor.execute_all([Task(description="t", category="x")],
                               _one_spec_registry(), Telemetry.new_run())
    assert out[0].status == TaskStatus.FAILED
    assert _AlwaysDown.calls == 1          # not 4


def test_executor_retries_a_transient_failure(monkeypatch):
    class FlakyThenOk(LLMClient):
        n = 0

        def chat(self, messages, **kw):
            type(self).n += 1
            if type(self).n == 1:
                raise LLMError("transient")
            return LLMReply(text="recovered")
    monkeypatch.setattr(executor, "get_llm", lambda tier: FlakyThenOk())
    monkeypatch.setattr(settings, "max_retries", 2)
    out = executor.execute_all([Task(description="t", category="x")],
                               _one_spec_registry(), Telemetry.new_run())
    assert out[0].status == TaskStatus.DONE and out[0].result == "recovered"
    assert FlakyThenOk.n == 2


def test_fatal_tool_error_is_not_retried_by_the_executor(monkeypatch):
    @tool
    def always_broken_service() -> str:
        """Simulated permanent external failure (retry cannot help)."""
        return "Error: service unavailable"

    calls = []

    class CallsTool(LLMClient):
        def chat(self, messages, **kw):
            calls.append(1)
            return LLMReply(text="", tool_calls=(ToolCall("always_broken_service", {}, "c"),))

    reg = _one_spec_registry(tool_names=["always_broken_service"],
                             fatal_tools=["always_broken_service"])
    monkeypatch.setattr(executor, "get_llm", lambda tier: CallsTool())
    monkeypatch.setattr(settings, "max_retries", 3)
    out = executor.execute_all([Task(description="t", category="x")], reg,
                               Telemetry.new_run())
    assert out[0].status == TaskStatus.FAILED and out[0].fatal
    assert len(calls) == 1


def test_a_crash_inside_one_task_does_not_kill_the_run(monkeypatch):
    def boom(spec, task, llm, tel):
        if "bad" in task.description:
            raise RuntimeError("kaboom")
        return task.model_copy(update={"status": TaskStatus.DONE, "result": "fine"})
    monkeypatch.setattr(executor, "run_specialist", boom)
    monkeypatch.setattr(executor, "get_llm", lambda tier: MockLLM())
    tasks = [Task(description="bad one", category="x"),
             Task(description="good one", category="x")]
    out = executor.execute_all(tasks, _one_spec_registry(), Telemetry.new_run())
    assert [t.status for t in out] == [TaskStatus.FAILED, TaskStatus.DONE]
    assert "internal error" in out[0].result


# ── Pipeline ───────────────────────────────────────────────────────
def test_pipeline_reports_backend_down_instead_of_raising(monkeypatch):
    monkeypatch.setattr(pipeline, "get_llm", lambda tier: _AlwaysDown())
    report = Orchestra().handle("Multiply 6 by 7")   # has signal -> planner runs
    assert not report.ok and report.error
    assert "ollama" in report.reply.lower() and report.reply.startswith("⚠")
    assert report.tasks == []


def test_pipeline_survives_an_unexpected_exception(monkeypatch):
    monkeypatch.setattr(pipeline, "plan",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bug")))
    report = Orchestra().handle("hello")
    assert not report.ok and "went wrong" in report.reply
    assert report.run_id in report.reply     # the user can quote it


def test_pipeline_emits_real_progress_events():
    events = []
    report = Orchestra().handle("Multiply 6 by 7", on_event=events.append)
    assert events[0].startswith("Planning")
    assert any(e.startswith("Planned") for e in events)
    assert any("Composing" in e for e in events)
    assert report.run_id


def test_run_report_with_no_tasks_and_no_error_is_ok_only_if_nothing_failed():
    from orchestra.engine.pipeline import RunReport
    assert RunReport(reply="x", tasks=[], run_id="r").ok
    assert not RunReport(reply="x", tasks=[], run_id="r", error="e").ok
