"""Orchestra CLI.

    python -m orchestra chat      interactive session
    python -m orchestra doctor    check Ollama + models before first run
    python -m orchestra stats     real numbers from telemetry
    python -m orchestra ask "..." one-shot question (good for scripting)
"""
from __future__ import annotations

import argparse
import logging
import sys

from .core.config import settings


def _setup_logging() -> None:
    fmt = ('{"t":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}'
           if settings.log_json else
           "%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logging.basicConfig(level=settings.log_level, format=fmt,
                        datefmt="%H:%M:%S")


def cmd_doctor() -> int:
    """Pre-flight check on the TARGET machine. Fails loudly with fixes."""
    import urllib.request, json as _json
    print(f"backend        : {settings.llm_backend}")
    print(f"data dir       : {settings.data_dir}")
    if settings.llm_backend == "mock":
        print("mock backend — nothing else to check. OK")
        return 0
    url = settings.ollama_base_url
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
            models = {m["name"] for m in _json.load(r).get("models", [])}
    except Exception as exc:
        print(f"✗ Ollama unreachable at {url} ({exc})")
        print("  fix: run `ollama serve` (or check ORCHESTRA_OLLAMA_BASE_URL)")
        return 1
    print(f"✓ Ollama up at {url} — {len(models)} model(s) installed")
    rc = 0
    for label, name in (("main", settings.main_model),
                        ("fast", settings.fast_model)):
        if any(m == name or m.startswith(name + ":") for m in models) or name in models:
            print(f"✓ {label} model  : {name}")
        else:
            print(f"✗ {label} model  : {name} NOT installed")
            print(f"  fix: ollama pull {name}")
            rc = 1
    try:
        import langchain_ollama  # noqa: F401
        print("✓ langchain-ollama installed")
    except ImportError:
        print("✗ langchain-ollama missing — fix: pip install langchain-ollama")
        rc = 1
    print("doctor:", "ALL GOOD — run `python -m orchestra chat`" if rc == 0
          else "problems found, apply fixes above")
    return rc


def cmd_stats() -> int:
    from .observability.telemetry import stats_report
    print(stats_report())
    return 0


def cmd_ask(message: str, audit: bool) -> int:
    from .engine.pipeline import Orchestra
    report = Orchestra().handle(message)
    print(report.reply)
    if audit:
        print("\n--- audit ---\n" + report.audit(), file=sys.stderr)
    return 0 if report.ok else 2


def cmd_chat() -> int:
    from .engine.pipeline import Orchestra
    orch = Orchestra()
    print("Orchestra v2 — type /audit to toggle the audit trail, /quit to exit")
    show_audit = False
    while True:
        try:
            msg = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not msg:
            continue
        if msg in {"/quit", "/exit"}:
            return 0
        if msg == "/audit":
            show_audit = not show_audit
            print(f"[audit {'on' if show_audit else 'off'}]")
            continue
        if msg == "/stats":
            cmd_stats()
            continue
        report = orch.handle(msg)
        print(f"\norchestra> {report.reply}")
        if show_audit:
            print("\n" + report.audit())


def cmd_serve(port: int) -> int:
    import uvicorn
    print(f"Orchestra web UI: http://localhost:{port}  (Ctrl+C to stop)")
    uvicorn.run("orchestra.web.server:app", host="127.0.0.1", port=port,
                log_level=settings.log_level.lower())
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    p = argparse.ArgumentParser(prog="orchestra")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat")
    sub.add_parser("doctor")
    sub.add_parser("stats")
    ask = sub.add_parser("ask")
    ask.add_argument("message")
    ask.add_argument("--audit", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)
    return {"chat": cmd_chat, "doctor": cmd_doctor, "stats": cmd_stats,
            "ask": lambda: cmd_ask(args.message, args.audit),
            "serve": lambda: cmd_serve(args.port)}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
