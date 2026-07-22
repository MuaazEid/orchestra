"""Phase 3 tests: planner hardening, dependency executor, aggregator, pipeline."""
import json
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

from orchestra.agents.factory import default_registry
from orchestra.core.contracts import Task, TaskStatus
from orchestra.engine.aggregator import aggregate
from orchestra.engine.executor import execute_all
from orchestra.engine.pipeline import Orchestra
from orchestra.engine.planner import plan
from orchestra.llm.adapter import LLMReply, ToolCall
from orchestra.llm.backends import MockLLM, _mock_singletons
from orchestra.observability.telemetry import Telemetry

REG = default_registry()


def _tel():
    return Telemetry.new_run()


# ── Planner ────────────────────────────────────────────────────────
def test_planner_builds_typed_tasks_with_dependencies():
    llm = MockLLM().queue(json.dumps({"tasks": [
        {"description": "multiply 6 by 7", "category": "math", "needs": []},
        {"description": "write a haiku about the result", "category": "writing",
         "needs": [0]},
    ]}))
    tasks = plan("multiply 6 by 7 then write a haiku about it", REG, llm, _tel())
    assert len(tasks) == 2
    assert tasks[0].category == "math" and tasks[0].depends_on == []
    assert tasks[1].depends_on == [tasks[0].id]


def test_planner_coerces_unknown_category_to_general():
    llm = MockLLM().queue(json.dumps({"tasks": [
        {"description": "launch a rocket", "category": "aerospace", "needs": []}]}))
    tasks = plan("launch a rocket", REG, llm, _tel())
    assert tasks[0].category == "general"


def test_planner_degrades_to_single_task_on_garbage():
    llm = MockLLM().queue("garbage", "more garbage")  # chat_json exhausts retries
    tasks = plan("hello there", REG, llm, _tel())
    assert len(tasks) == 1
    assert tasks[0].category == "general"
    assert tasks[0].description == "hello there"


def test_planner_ignores_self_and_out_of_range_needs():
    llm = MockLLM().queue(json.dumps({"tasks": [
        {"description": "a", "category": "general", "needs": [0, 5, -1]}]}))
    tasks = plan("a", REG, llm, _tel())
    assert tasks[0].depends_on == []


# ── Executor ───────────────────────────────────────────────────────
def _prime_mocks(main_replies, fast_replies):
    """Route scripted replies into the factory's mock singletons."""
    _mock_singletons.clear()
    _mock_singletons["main"] = MockLLM("mock-main").queue(*main_replies)
    _mock_singletons["fast"] = MockLLM("mock-fast").queue(*fast_replies)
    return _mock_singletons["main"], _mock_singletons["fast"]


def test_executor_runs_independent_tasks_and_keeps_plan_order():
    main, fast = _prime_mocks(
        main_replies=[
            LLMReply(text="", tool_calls=(ToolCall("multiply", {"a": 6, "b": 7}, "c1"),)),
            LLMReply(text="42"),
            LLMReply(text="Moon haiku here."),
        ],
        fast_replies=[LLMReply(text="Saved your name.",
                               tool_calls=())],
    )
    tasks = [
        Task(description="multiply 6 by 7", category="math"),
        Task(description="write a haiku about the moon", category="writing"),
    ]
    done = execute_all(tasks, REG, _tel())
    assert [t.description.split()[0] for t in done] == ["multiply", "write"]
    assert all(t.status == TaskStatus.DONE for t in done)


def test_executor_injects_dependency_results_as_context():
    main, _ = _prime_mocks(
        main_replies=[LLMReply(text="", tool_calls=(
                          ToolCall("multiply", {"a": 6, "b": 7}, "c1"),)),
                      LLMReply(text="42"),
                      LLMReply(text="Haiku about 42.")],
        fast_replies=[],
    )
    t1 = Task(description="multiply 6 by 7", category="math")
    t2 = Task(description="write a haiku about the result", category="writing",
              depends_on=[t1.id])
    done = execute_all([t1, t2], REG, _tel())
    assert done[1].status == TaskStatus.DONE
    # the writer's request must contain the math result as context
    writer_call = main.calls[-1]
    assert "42" in writer_call["messages"][-1]["content"]
    assert "Context from earlier steps" in writer_call["messages"][-1]["content"]


def test_executor_marks_dependents_blocked_when_prerequisite_fails():
    # math task loops forever -> FAILED after retries; writer must be blocked
    loop = LLMReply(text="", tool_calls=(ToolCall("multiply", {"a": 1, "b": 1}, "c"),))
    _prime_mocks(main_replies=[loop] * 60, fast_replies=[])
    t1 = Task(description="impossible math", category="math")
    t2 = Task(description="haiku about it", category="writing", depends_on=[t1.id])
    done = execute_all([t1, t2], REG, _tel())
    assert done[0].status == TaskStatus.FAILED
    assert done[1].status == TaskStatus.FAILED
    assert "prerequisite" in done[1].result


# ── Aggregator ─────────────────────────────────────────────────────
def test_aggregator_single_task_skips_llm():
    llm = MockLLM()
    t = Task(description="x", category="math", status=TaskStatus.DONE,
             result="The answer is 42.")
    out = aggregate("what is 6*7", [t], llm, _tel())
    assert out == "The answer is 42."
    assert llm.calls == []          # zero wasted LLM calls


def test_aggregator_weaves_multiple_results():
    llm = MockLLM().queue("Saved your name, and 6x7 = 42. Here's your haiku: ...")
    tasks = [
        Task(description="save name", category="memory_save",
             status=TaskStatus.DONE, result="Saved: name = Muaaz",
             assigned_to="Memory Keeper"),
        Task(description="math", category="math", status=TaskStatus.DONE,
             result="42", assigned_to="Math Solver"),
    ]
    out = aggregate("compound request", tasks, llm, _tel())
    assert "42" in out
    # the LLM saw both labeled results
    sent = llm.calls[0]["messages"][-1]["content"]
    assert "Memory Keeper" in sent and "Math Solver" in sent


def test_aggregator_falls_back_to_labeled_join_when_llm_empty():
    llm = MockLLM().queue("")       # empty reply -> fallback
    tasks = [
        Task(description="a", category="math", status=TaskStatus.DONE,
             result="42", assigned_to="Math Solver"),
        Task(description="b", category="writing", status=TaskStatus.FAILED,
             result="LLM unavailable", assigned_to="Writer"),
    ]
    out = aggregate("req", tasks, llm, _tel())
    assert "[Math Solver]" in out and "[Writer [FAILED]]" in out


# ── Full pipeline (end-to-end with mocks) ─────────────────────────
def test_pipeline_end_to_end_compound_request():
    _mock_singletons.clear()
    _mock_singletons["main"] = MockLLM("mock-main").queue(
        # planner
        json.dumps({"tasks": [
            {"description": "save fact: user's name is Muaaz",
             "category": "memory_save", "needs": []},
            {"description": "multiply 6 by 7", "category": "math", "needs": []},
        ]}),
        # math specialist
        LLMReply(text="", tool_calls=(ToolCall("multiply", {"a": 6, "b": 7}, "c1"),)),
        LLMReply(text="6 x 7 = 42"),
    )
    _mock_singletons["fast"] = MockLLM("mock-fast").queue(
        # memory keeper — terminal tool ends the task on this call, no 2nd turn
        LLMReply(text="", tool_calls=(
            ToolCall("remember_about_user",
                     {"key": "name", "fact": "Muaaz"}, "m1"),)),
        # aggregator
        LLMReply(text="Done! Saved your name (Muaaz), and 6 x 7 = 42."),
    )
    report = Orchestra(registry=REG).handle("My name is Muaaz, multiply 6 by 7")
    assert report.ok
    assert "42" in report.reply and "Muaaz" in report.reply
    assert len(report.tasks) == 2
    assert "task(s)" in report.audit()


# ── Deterministic routing backstop (Phase 5 hardening) ─────────────
def test_planner_corrects_counting_misrouted_as_math():
    llm = MockLLM().queue(json.dumps({"tasks": [
        {"description": "Count occurrences of the letter s in 'mississippi'",
         "category": "math", "needs": []}]}))
    tasks = plan("how many times does s appear in mississippi", REG, llm, _tel())
    assert tasks[0].category == "text_analysis"


def test_planner_corrects_recall_misrouted_as_general():
    llm = MockLLM().queue(json.dumps({"tasks": [
        {"description": "Retrieve the user's favorite programming language",
         "category": "general", "needs": []}]}))
    tasks = plan("what is my favorite programming language", REG, llm, _tel())
    assert tasks[0].category == "memory_recall"


def test_planner_backstop_leaves_correct_categories_untouched():
    llm = MockLLM().queue(json.dumps({"tasks": [
        {"description": "Multiply 25 by 17", "category": "math", "needs": []}]}))
    tasks = plan("multiply 25 by 17", REG, llm, _tel())
    assert tasks[0].category == "math"


# ── Smart context injection (fix + counter-fix of web+file failures) ──
def test_context_injected_trimmed_for_file_save_tasks():
    # a save task NEEDS upstream content — but trimmed
    from orchestra.engine.executor import _context_for, _CTX_CAP
    from orchestra.core.contracts import Task, TaskStatus
    fetched = Task(description="fetch page", category="web",
                   status=TaskStatus.DONE, result="X" * 5000)
    save = Task(description="save a summary of the fetched content",
                category="files", depends_on=[fetched.id])
    out = _context_for(save, {fetched.id: fetched})
    assert "Context from earlier steps" in out.description
    assert "[trimmed]" in out.description
    assert len(out.description) < _CTX_CAP + 300


def test_context_skipped_for_memory_recall_tasks():
    from orchestra.engine.executor import _context_for
    from orchestra.core.contracts import Task, TaskStatus
    prev = Task(description="anything", category="general",
                status=TaskStatus.DONE, result="Z" * 3000)
    recall = Task(description="what is my favorite language",
                  category="memory_recall", depends_on=[prev.id])
    out = _context_for(recall, {prev.id: prev})
    assert "Context from earlier steps" not in out.description


def test_context_injected_but_trimmed_for_writing_tasks():
    from orchestra.engine.executor import _context_for, _CTX_CAP
    from orchestra.core.contracts import Task, TaskStatus
    src = Task(description="fetch page", category="web",
               status=TaskStatus.DONE, result="Y" * 5000)
    write = Task(description="summarize the page", category="writing",
                 depends_on=[src.id])
    out = _context_for(write, {src.id: src})
    assert "Context from earlier steps" in out.description
    assert "[trimmed]" in out.description
    assert len(out.description) < _CTX_CAP + 200


def test_read_file_tasks_skip_context_but_save_tasks_get_it():
    from orchestra.engine.executor import _context_for
    from orchestra.core.contracts import Task, TaskStatus
    prev = Task(description="save summary", category="files",
                status=TaskStatus.DONE, result="Wrote 500 chars to x.txt")
    read = Task(description="Read the contents of ollama_news.txt",
                category="files", depends_on=[prev.id])
    assert "Context" not in _context_for(read, {prev.id: prev}).description
    save = Task(description="Save a summary of the fetched content",
                category="files", depends_on=[prev.id])
    assert "Context" in _context_for(save, {prev.id: prev}).description
