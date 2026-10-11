"""Smoke test for the TARGET machine (run with real Ollama).

    python smoke_test.py

Checks Ollama is ready first, then runs representative requests through the
full pipeline: tool use, memory, files, a plain question that must NOT
invent tool tasks, and an Arabic request that must be answered in Arabic.
Prints per-case status + latency, then the telemetry table. Send the full
output back - the numbers decide what to tune next.

Exit code 0 only if every case passes.
"""
from __future__ import annotations

import re
import time

_AR = re.compile("[؀-ۿ]")
_CJK = re.compile("[一-鿿぀-ヿ가-힯]")


def _has(*needles):
    """Reply must contain at least one of the needles (case-insensitive)."""
    return lambda r: any(n.lower() in r.reply.lower() for n in needles)


def _plain_question(r):
    """No tool task may be invented for a question that needs no tool."""
    return (bool(r.reply.strip()) and len(r.tasks) == 1
            and r.tasks[0].category == "general")


def _arabic_reply(r):
    return bool(_AR.search(r.reply)) and not _CJK.search(r.reply)


CASES = [
    # (label, message, check(report) -> bool)
    ("simple math", "What is 25 multiplied by 17?", _has("425")),
    ("memory save", "Please remember that my favorite language is Python",
     _has("python", "saved", "remember")),
    ("memory recall", "What is my favorite programming language?", _has("python")),
    ("counting", "How many times does the letter s appear in 'mississippi'?",
     _has("4", "four")),
    ("compound", "My name is Muaaz. Multiply 6 by 7, and write a one-line "
                 "haiku about the moon.", _has("42")),
    ("files", "Save the text 'orchestra rocks' into a file named demo.txt, "
              "then read demo.txt back and tell me what it contains.",
     _has("orchestra rocks")),
    ("plain question (no invented tools)",
     "Explain in one sentence what an API is", _plain_question),
    ("arabic in, arabic out", "اشرح لي بجملة واحدة ما هو الذكاء الاصطناعي",
     _arabic_reply),
]


def main() -> int:
    from orchestra import __version__
    from orchestra.core.config import settings
    from orchestra.engine.pipeline import Orchestra
    from orchestra.llm.health import ollama_status
    from orchestra.observability.telemetry import stats_report

    print(f"orchestra {__version__}  backend={settings.llm_backend} "
          f"main={settings.main_model} fast={settings.fast_model} "
          f"concurrency={settings.max_concurrency} num_ctx={settings.num_ctx}")
    if settings.llm_backend == "mock":
        print("The mock backend cannot answer these questions - this script is "
              "for a real Ollama.\nUnset ORCHESTRA_LLM_BACKEND and try again.")
        return 0
    status = ollama_status(timeout=5)
    if not status["reachable"]:
        print("✗ " + status["error"])
        return 1
    if status["missing"]:
        print("✗ models not installed: " + ", ".join(status["missing"]))
        print("  fix: " + "; ".join(f"ollama pull {m}" for m in status["missing"]))
        return 1

    orch = Orchestra()
    passed = 0
    for label, msg, check in CASES:
        t0 = time.perf_counter()
        report = orch.handle(msg)       # never raises: failures come back in the report
        dt = time.perf_counter() - t0
        ok = report.ok and check(report)
        passed += ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] {label}  ({dt:.1f}s, "
              f"{len(report.tasks)} task(s))")
        print(f"  reply : {report.reply[:160]}")
        if not ok:
            print("  audit :\n" + "\n".join(
                "    " + line for line in report.audit().splitlines()))
    print(f"\n=== {passed}/{len(CASES)} passed ===\n")
    print(stats_report())
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
