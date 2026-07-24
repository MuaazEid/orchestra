"""Language guard tests — detection primitives plus the aggregator's
CJK-leak retry policy. All scripted via MockLLM, no Ollama needed.

Covers the failure this shipped to fix: qwen answering an Arabic request
by drifting into Chinese. The guard must (a) name the right language in
the prompt, (b) catch a leaked reply, (c) retry once, and (d) fall back
to the labeled join rather than return Chinese to the user.
"""
import os
os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"

from orchestra.core.contracts import Task, TaskStatus
from orchestra.engine.aggregator import aggregate
from orchestra.llm.backends import MockLLM
from orchestra.llm.language import (
    detect_language, has_cjk_leak, language_instruction,
)
from orchestra.observability.telemetry import Telemetry


# ── detection ──────────────────────────────────────────────────────
def test_detect_language_arabic():
    assert detect_language("اديني احدث فرص عمل في السعودية") == "ar"


def test_detect_language_english():
    assert detect_language("give me the latest AI jobs") == "en"


def test_detect_language_chinese():
    assert detect_language("给我最新的工作机会") == "cjk"


def test_detect_language_mixed_latin_arabic_counts_by_majority():
    # "AI Engineer" is Latin but the sentence is mostly Arabic
    assert detect_language("اديني احدث فرص عمل AI Engineer في السعودية") == "ar"


def test_detect_language_empty_or_symbols_defaults_english():
    assert detect_language("") == "en"
    assert detect_language("123 !!! ???") == "en"


# ── leak detection ─────────────────────────────────────────────────
def test_cjk_leak_flagged_when_user_wrote_none():
    reply = "بالنسبة للطيران، يمكنك استخدام 出发城市 利雅得 目的城市 吉达 请填写"
    assert has_cjk_leak(reply, "ابحث لي عن تذكرة طيران") is True


def test_no_leak_for_pure_arabic_reply():
    assert has_cjk_leak("لم أتمكن من إيجاد وظائف مطابقة", "ابحث عن وظائف") is False


def test_no_leak_when_user_writes_cjk():
    # user speaks Chinese -> Chinese reply is correct, not a leak
    assert has_cjk_leak("这是最新的工作机会", "给我最新的工作") is False


def test_single_stray_glyph_is_tolerated():
    # a quoted name is not the sentence-level drift we guard against
    assert has_cjk_leak("The author 李 wrote this note", "who wrote it") is False


# ── prompt instruction ─────────────────────────────────────────────
def test_language_instruction_names_arabic_and_bans_cjk():
    instr = language_instruction("اديني وظائف")
    assert "Arabic" in instr
    assert "Chinese" in instr


def test_language_instruction_names_english():
    assert "English" in language_instruction("give me jobs")


# ── aggregator integration ─────────────────────────────────────────
def _tasks():
    return [
        Task(id="t1", category="web", description="find jobs",
             status=TaskStatus.DONE, result="no jobs found", assigned_to="Web Reader"),
        Task(id="t2", category="general", description="advise",
             status=TaskStatus.DONE, result="try job boards", assigned_to="General Assistant"),
    ]


def test_aggregator_retries_once_on_cjk_leak_then_succeeds():
    llm = MockLLM()
    llm.queue(
        "لم أجد وظائف. 请填写以下信息 出发城市 利雅得",   # leaked: retried
        "لم أجد وظائف مطابقة. يمكنك البحث في مواقع التوظيف.",  # clean
    )
    out = aggregate("اديني وظائف في السعودية", _tasks(), llm, Telemetry.new_run())
    assert has_cjk_leak(out, "اديني وظائف") is False
    assert "وظائف" in out
    assert len(llm.calls) == 2               # exactly one retry


def test_aggregator_falls_back_to_join_when_leak_persists():
    llm = MockLLM()
    llm.queue(
        "请填写 出发城市 利雅得 目的城市 吉达 旅行日期",   # leak
        "至于机票 您可以使用 Kayak 查找 请填写以下信息",   # still leaking
    )
    out = aggregate("ابحث عن تذكرة طيران", _tasks(), llm, Telemetry.new_run())
    # gave up on the model -> labeled join, which is in the tasks' language
    assert has_cjk_leak(out, "ابحث عن تذكرة") is False
    assert "Web Reader" in out and "no jobs found" in out
    assert len(llm.calls) == 2               # tried twice, no third attempt


def test_aggregator_passes_clean_reply_through_untouched():
    llm = MockLLM()
    llm.queue("لم أجد وظائف مطابقة لخبرتك، جرّب مواقع التوظيف المتخصصة.")
    out = aggregate("اديني وظائف", _tasks(), llm, Telemetry.new_run())
    assert out == "لم أجد وظائف مطابقة لخبرتك، جرّب مواقع التوظيف المتخصصة."
    assert len(llm.calls) == 1               # no needless retry


def test_aggregator_pins_language_in_system_prompt():
    llm = MockLLM()
    llm.queue("رد نظيف بالعربية")
    aggregate("اديني وظائف بالعربي", _tasks(), llm, Telemetry.new_run())
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "Arabic" in system_prompt         # the pin reached the model
