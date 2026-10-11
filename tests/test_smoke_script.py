"""The real-model smoke script itself must stay importable and honest on a
machine without Ollama (CI): it must not crash, and must not pretend."""
import os

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import importlib.util
import pathlib

SPEC = importlib.util.spec_from_file_location(
    "smoke_test", pathlib.Path(__file__).parent.parent / "smoke_test.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_mock_backend_exits_cleanly_with_an_explanation(capsys):
    assert smoke.main() == 0
    assert "mock backend cannot answer" in capsys.readouterr().out


def test_unreachable_ollama_is_reported_with_a_fix(monkeypatch, capsys):
    from orchestra.core.config import settings
    from orchestra.llm import health
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(health, "ollama_status", lambda timeout=2.0: {
        "reachable": False, "installed": [], "missing": [],
        "error": "Ollama is not reachable at http://x. Start it with `ollama serve`."})
    assert smoke.main() == 1
    assert "ollama serve" in capsys.readouterr().out


def test_plain_question_check_rejects_invented_tool_tasks():
    from orchestra.core.contracts import Task
    from orchestra.engine.pipeline import RunReport
    ok = RunReport("An API is an interface.", [Task(description="d", category="general")], "r")
    bad = RunReport("Saved!", [Task(description="d", category="memory_save")], "r")
    assert smoke._plain_question(ok) and not smoke._plain_question(bad)


def test_arabic_check_requires_arabic_and_no_cjk():
    from orchestra.engine.pipeline import RunReport
    mk = lambda text: RunReport(text, [], "r")
    assert smoke._arabic_reply(mk("الذكاء الاصطناعي هو علم"))
    assert not smoke._arabic_reply(mk("AI is a field"))
    assert not smoke._arabic_reply(mk("الذكاء 人工智能 الاصطناعي"))
