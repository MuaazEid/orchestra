"""Smoke test for the TARGET machine (run with real Ollama).

    python smoke_test.py

Runs 5 representative requests through the full pipeline, prints per-run
status + latency, then the telemetry table. Send Claude the full output —
the numbers decide what we tune next.
"""
from __future__ import annotations

import time

CASES = [
    # (label, message, must_contain_any)
    ("simple math", "What is 25 multiplied by 17?", ["425"]),
    ("memory save", "Please remember that my favorite language is Python",
     ["python", "saved", "remember"]),
    ("memory recall", "What is my favorite programming language?", ["python"]),
    ("counting", "How many times does the letter s appear in 'mississippi'?",
     ["4", "four"]),
    ("compound", "My name is Muaaz. Multiply 6 by 7, and write a one-line "
                 "haiku about the moon.", ["42"]),
    ("files", "Save the text 'orchestra rocks' into a file named demo.txt, "
              "then read demo.txt back and tell me what it contains.",
     ["orchestra rocks"]),
]


def main() -> int:
    from orchestra.core.config import settings
    from orchestra.engine.pipeline import Orchestra
    from orchestra.observability.telemetry import stats_report

    print(f"backend={settings.llm_backend} main={settings.main_model} "
          f"fast={settings.fast_model} concurrency={settings.max_concurrency}")
    orch = Orchestra()
    passed = 0
    for label, msg, expect in CASES:
        t0 = time.perf_counter()
        try:
            report = orch.handle(msg)
            dt = time.perf_counter() - t0
            hit = any(e.lower() in report.reply.lower() for e in expect)
            ok = report.ok and hit
            passed += ok
            print(f"\n[{'PASS' if ok else 'FAIL'}] {label}  ({dt:.1f}s, "
                  f"{len(report.tasks)} task(s))")
            print(f"  reply : {report.reply[:160]}")
            if not ok:
                print("  audit :\n" + "\n".join(
                    "    " + line for line in report.audit().splitlines()))
        except Exception as exc:
            print(f"\n[CRASH] {label}: {exc!r}")
    print(f"\n=== {passed}/{len(CASES)} passed ===\n")
    print(stats_report())
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
