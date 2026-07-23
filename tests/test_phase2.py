"""Phase 2 tests: toolbox safety, built-in tools, registry, ReAct engine."""
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import pytest

from orchestra.agents import builtin_tools  # registers tools
from orchestra.agents.toolbox import get_tools, run_tool, registry_names, tool
from orchestra.agents.factory import (SpecialistRegistry, default_registry,
                                      run_specialist)
from orchestra.core.contracts import SpecialistSpec, Task, TaskStatus
from orchestra.llm.adapter import LLMReply, ToolCall
from orchestra.llm.backends import MockLLM
from orchestra.observability.telemetry import Telemetry


# ── Toolbox ────────────────────────────────────────────────────────
def test_all_builtin_tools_registered():
    assert set(registry_names()) >= {
        "remember_about_user", "recall_about_user", "add", "multiply",
        "divide", "count_letters", "word_count", "get_current_time"}


def test_unknown_tool_fails_loudly_at_assignment():
    with pytest.raises(KeyError):
        get_tools(["definitely_not_a_tool"])


def test_run_tool_validates_invented_argument_names():
    # small models love inventing arg names — must degrade to a message,
    # never a crash
    out = run_tool("add", {"x": 1, "y": 2})
    assert out.startswith("Error: bad arguments")


def test_run_tool_wraps_tool_exceptions():
    out = run_tool("divide", {"a": 1, "b": 0})
    assert out.startswith("Error:") and "zero" in out


def test_tool_without_docstring_rejected():
    with pytest.raises(ValueError):
        @tool
        def undocumented():  # noqa
            return 1


def test_memory_roundtrip():
    assert "Saved" in run_tool("remember_about_user",
                               {"key": "t_lang", "fact": "loves Python"})
    assert "Python" in run_tool("recall_about_user", {"query": "t_lang"})


# ── Registry ───────────────────────────────────────────────────────
def test_registry_hires_and_finds_by_category():
    reg = default_registry.__wrapped__() if hasattr(default_registry, "__wrapped__") \
        else default_registry()
    assert reg.find_for("math").name == "Math Solver"
    assert reg.find_for("nonexistent") is None
    assert "Math Solver" in reg.catalog()
    assert "math" in reg.all_categories


def test_registry_rejects_specialist_with_unknown_tool():
    reg = SpecialistRegistry()
    with pytest.raises(KeyError):
        reg.register(SpecialistSpec(name="Broken", categories=["x"],
                                    system_prompt="p", tool_names=["ghost"]))


def test_registry_rejects_duplicate_hire():
    reg = SpecialistRegistry()
    spec = SpecialistSpec(name="A", categories=["x"], system_prompt="p")
    reg.register(spec)
    with pytest.raises(ValueError):
        reg.register(spec)


# ── ReAct engine ───────────────────────────────────────────────────
def _math_spec():
    return SpecialistSpec(name="Math Solver", categories=["math"],
                          system_prompt="use tools",
                          tool_names=["multiply"], max_steps=4)


def test_specialist_completes_react_loop_with_real_tool_execution():
    llm = MockLLM().queue(
        LLMReply(text="", tool_calls=(ToolCall("multiply", {"a": 6, "b": 7}, "c1"),)),
        LLMReply(text="The answer is 42."),
    )
    done = run_specialist(_math_spec(), Task(description="6 times 7", category="math"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.DONE
    assert done.result == "The answer is 42."
    assert done.assigned_to == "Math Solver"
    # the REAL tool ran and its output went back to the LLM as a tool message
    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "42"


def test_specialist_survives_invented_tool_name():
    llm = MockLLM().queue(
        LLMReply(text="", tool_calls=(ToolCall("teleport", {}, "c1"),)),
        LLMReply(text="Sorry, I used the wrong tool."),
    )
    done = run_specialist(_math_spec(), Task(description="x", category="math"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.DONE  # degraded gracefully, no crash


def test_specialist_hits_step_limit_marks_failed():
    loop_reply = LLMReply(text="", tool_calls=(ToolCall("multiply",
                                                        {"a": 1, "b": 1}, "c"),))
    llm = MockLLM().queue(*[loop_reply] * 10)
    done = run_specialist(_math_spec(), Task(description="loop", category="math"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.FAILED
    assert "step limit" in done.result


# ── Terminal tools: break the small-model save-loop (Phase 5) ──────
def test_terminal_tool_completes_task_without_extra_llm_turn():
    from orchestra.agents import builtin_tools  # noqa
    spec = SpecialistSpec(name="Saver", categories=["memory_save"],
                          system_prompt="save it",
                          tool_names=["remember_about_user"],
                          terminal_tools=["remember_about_user"],
                          max_steps=5)
    # Model tries to loop (keeps calling the tool), but the FIRST successful
    # call must end the task immediately.
    save_call = LLMReply(text="", tool_calls=(
        ToolCall("remember_about_user", {"key": "lang", "fact": "Python"}, "s1"),))
    llm = MockLLM().queue(save_call, save_call, save_call)
    done = run_specialist(spec, Task(description="save lang=Python",
                                     category="memory_save"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.DONE
    assert "Python" in done.result
    assert len(llm.calls) == 1          # loop broken after ONE llm turn


def test_terminal_tool_does_not_fire_on_error():
    spec = SpecialistSpec(name="Saver", categories=["memory_save"],
                          system_prompt="save it",
                          tool_names=["remember_about_user"],
                          terminal_tools=["remember_about_user"],
                          max_steps=3)
    # tool called with bad args -> Error -> must NOT terminate as success
    bad = LLMReply(text="", tool_calls=(
        ToolCall("remember_about_user", {"wrong": "x"}, "s1"),))
    good = LLMReply(text="ok done")
    llm = MockLLM().queue(bad, good)
    done = run_specialist(spec, Task(description="x", category="memory_save"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.DONE
    assert len(llm.calls) == 2          # error did not short-circuit


# ── Recall matching bug fix (Phase 5): word-level, with full fallback ──
def test_recall_matches_across_underscore_vs_space():
    run_tool("remember_about_user",
             {"key": "favorite_language", "fact": "Python"})
    out = run_tool("recall_about_user",
                   {"query": "favorite programming language"})
    assert "Python" in out          # the exact smoke-test failure, now covered


def test_recall_falls_back_to_all_facts_on_filter_miss():
    run_tool("remember_about_user", {"key": "city", "fact": "Riyadh"})
    out = run_tool("recall_about_user", {"query": "zzz nothing matches"})
    assert "Riyadh" in out          # miss -> return everything, model picks


# ── New capability tools (files sandbox + web) ─────────────────────
def test_file_tools_roundtrip_in_workspace():
    assert "Wrote" in run_tool("write_file",
                               {"path": "t/notes.txt", "content": "hello orchestra"})
    assert "hello orchestra" in run_tool("read_file", {"path": "t/notes.txt"})
    assert "Appended" in run_tool("append_file",
                                  {"path": "t/notes.txt", "content": " v2"})
    assert "notes.txt" in run_tool("list_files", {"subfolder": "t"})


def test_file_tools_block_path_escape():
    for bad in ("../evil.txt", "..\\evil.txt", "a/../../evil.txt"):
        out = run_tool("write_file", {"path": bad, "content": "x"})
        assert out.startswith("Error"), bad
    out = run_tool("read_file", {"path": "../../etc/passwd"})
    assert out.startswith("Error")


def test_fetch_webpage_rejects_non_http():
    out = run_tool("fetch_webpage", {"url": "file:///etc/passwd"})
    assert out.startswith("Error")


def test_new_specialists_hired_with_hints():
    from orchestra.agents.factory import default_registry
    reg = default_registry()
    assert reg.find_for("files").name == "File Clerk"
    assert reg.find_for("web").name == "Web Reader"
    hints = reg.hints()
    assert "files" in hints and "web" in hints and "text_analysis" in hints


# ── Context cap: big tool output must not balloon the transcript ───
def test_large_tool_output_is_capped_in_transcript():
    from orchestra.agents.toolbox import tool as _tool
    from orchestra.agents.factory import _TOOL_CTX_CAP

    @_tool
    def big_dummy_tool() -> str:
        """Return a very large string to test transcript capping."""
        return "X" * 50_000

    spec = SpecialistSpec(name="Bigger", categories=["big"],
                          system_prompt="use the tool then answer",
                          tool_names=["big_dummy_tool"], max_steps=3)
    llm = MockLLM().queue(
        LLMReply(text="", tool_calls=(ToolCall("big_dummy_tool", {}, "b1"),)),
        LLMReply(text="done reading"),
    )
    done = run_specialist(spec, Task(description="read big", category="big"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.DONE
    # the 2nd LLM call's transcript must contain the CAPPED tool message
    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and len(tool_msgs[0]["content"]) < _TOOL_CTX_CAP + 100
    assert "truncated" in tool_msgs[0]["content"]


# ── File Clerk ops are terminal: one successful op ends the task ───
def test_file_read_terminates_with_content_as_result():
    from orchestra.agents.factory import default_registry
    run_tool("write_file", {"path": "term.txt", "content": "orchestra rocks"})
    reg = default_registry()
    clerk = reg.find_for("files")
    assert set(clerk.terminal_tools) == set(clerk.tool_names)
    llm = MockLLM().queue(
        LLMReply(text="", tool_calls=(ToolCall("read_file",
                                               {"path": "term.txt"}, "r1"),)),
    )
    done = run_specialist(clerk, Task(description="read term.txt",
                                      category="files"),
                          llm, Telemetry.new_run())
    assert done.status == TaskStatus.DONE
    assert done.result == "orchestra rocks"   # file content IS the result
    assert len(llm.calls) == 1                # no loop possible


def test_bad_args_error_includes_correct_signature():
    out = run_tool("read_file", {"filename": "x.txt"})   # invented arg name
    assert out.startswith("Error: bad arguments")
    assert "read_file(path:" in out and "Required signature" in out                 # self-correction aid


# ── Career tools: deterministic job-fit scoring (Phase 6) ──────────
def test_score_job_fit_errors_without_background_file():
    from orchestra.agents.career_tools import score_job_fit, _safe, _BACKGROUND_FILE
    bg = _safe(_BACKGROUND_FILE)
    if bg.exists():
        bg.unlink()   # ensure a clean precondition in the shared test workspace
    out = score_job_fit("Looking for a Python developer")
    assert out.startswith("Error") and "background.txt" in out


def test_score_job_fit_matches_and_scores_deterministically():
    run_tool("write_file", {
        "path": "background.txt",
        "content": "Python LangGraph Ollama RAG FastAPI Arabic NLP Dahua DSS",
    })
    out1 = run_tool("score_job_fit", {
        "posting_text": "We need a Python engineer with RAG and FastAPI experience"})
    out2 = run_tool("score_job_fit", {
        "posting_text": "We need a Python engineer with RAG and FastAPI experience"})
    assert out1 == out2                      # deterministic: same in -> same out
    assert "STRONG" in out1 or "MODERATE" in out1
    assert "python" in out1 and "rag" in out1 and "fastapi" in out1


def test_score_job_fit_weak_for_unrelated_posting():
    run_tool("write_file", {
        "path": "background.txt",
        "content": "Python LangGraph Ollama RAG FastAPI",
    })
    out = run_tool("score_job_fit", {
        "posting_text": "Seeking a pastry chef with cake decorating experience"})
    assert "WEAK" in out


def test_log_application_creates_and_appends():
    out1 = run_tool("log_application", {
        "company": "SDAIA", "role": "AI Engineer", "fit_summary": "STRONG 80%"})
    assert "SDAIA" in out1
    log = run_tool("read_file", {"path": "applications_log.md"})
    assert "SDAIA" in log and "AI Engineer" in log


def test_career_assistant_registered_with_hint():
    from orchestra.agents.factory import default_registry
    reg = default_registry()
    assert reg.find_for("job_search").name == "Career Assistant"
    assert "job posting" in reg.hints()


# ── Job Scout: real search via Tavily (mocked in tests, no network) ─
def test_search_jobs_errors_without_api_key():
    from orchestra.agents import job_search_tools as jst
    jst.settings.tavily_api_key = None
    out = jst.search_jobs("AI engineer jobs Riyadh")
    assert out.startswith("Error") and "TAVILY_API_KEY" in out


def test_search_jobs_formats_results(monkeypatch):
    from orchestra.agents import job_search_tools as jst
    jst.settings.tavily_api_key = "fake-key-for-test"

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps({"results": [
                {"title": "AI Engineer - SDAIA", "url": "https://x.test/1",
                 "content": "Riyadh based AI role"},
            ]}).encode()

    monkeypatch.setattr(jst.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    out = jst.search_jobs("AI engineer jobs Riyadh")
    assert "SDAIA" in out and "https://x.test/1" in out
    jst.settings.tavily_api_key = None


def test_search_jobs_handles_api_failure(monkeypatch):
    from orchestra.agents import job_search_tools as jst
    jst.settings.tavily_api_key = "fake-key-for-test"

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(jst.urllib.request, "urlopen", boom)
    out = jst.search_jobs("anything")
    assert out.startswith("Error") and "network down" in out
    jst.settings.tavily_api_key = None


def test_job_scout_registered_with_hint():
    from orchestra.agents.factory import default_registry
    reg = default_registry()
    assert reg.find_for("job_discovery").name == "Job Scout"
    assert "search_jobs" in reg.find_for("job_discovery").tool_names


# ── fatal_tools: error stops the task instead of letting the model
#    hallucinate past it (bug found live: Job Scout + missing Tavily key
#    fabricated fake job postings instead of reporting the failure) ────
def test_fatal_tool_error_fails_task_immediately_no_hallucination():
    from orchestra.agents.toolbox import tool as _tool

    @_tool
    def flaky_external_call() -> str:
        """Simulate a non-retriable external-service failure for testing."""
        return "Error: service unavailable (simulated)"

    spec = SpecialistSpec(name="Scout", categories=["x"],
                          system_prompt="find things",
                          tool_names=["flaky_external_call"],
                          fatal_tools=["flaky_external_call"],
                          max_steps=3)
    llm = MockLLM().queue(
        LLMReply(text="", tool_calls=(ToolCall("flaky_external_call", {}, "c1"),)),
        # if the fix didn't work, the model would get ANOTHER turn here and
        # could fabricate a fake answer instead of reporting the error
        LLMReply(text="Found great jobs at FakeCorp!"),
    )
    task = run_specialist(spec, Task(description="find x jobs", category="x"),
                          llm, Telemetry.new_run())
    assert task.status == TaskStatus.FAILED
    assert task.result.startswith("Error")
    assert len(llm.calls) == 1   # never got a second turn to invent anything


def test_non_fatal_tool_error_still_lets_model_retry():
    # bad-argument errors must remain retriable (unchanged behavior)
    run_tool("write_file", {"path": "retry_test.txt", "content": "hi"})
    spec = SpecialistSpec(name="Reader", categories=["files"],
                          system_prompt="read files",
                          tool_names=["read_file"], max_steps=3)
    llm = MockLLM().queue(
        LLMReply(text="", tool_calls=(ToolCall("read_file", {"bad": "arg"}, "c1"),)),
        LLMReply(text="", tool_calls=(ToolCall("read_file", {"path": "retry_test.txt"}, "c2"),)),
        LLMReply(text="It says: hi"),
    )
    task = run_specialist(spec, Task(description="read retry_test.txt", category="files"),
                          llm, Telemetry.new_run())
    assert task.status == TaskStatus.DONE
    assert len(llm.calls) == 3   # bad-args error did NOT stop the task early
