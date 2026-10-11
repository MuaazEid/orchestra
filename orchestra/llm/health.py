"""Ollama readiness: is it reachable, are the configured models installed,
and (optionally) load them ahead of the first request.

Shared by the CLI `doctor`, the web `/api/health` endpoint and the server's
start-up warm-up, so all three agree on what "ready" means.
"""
from __future__ import annotations

import logging
import threading
import time

from ..core.config import settings

logger = logging.getLogger(__name__)


def required_models() -> list[str]:
    """Distinct models the pipeline will call (main and fast may be equal)."""
    out: list[str] = []
    for name in (settings.main_model, settings.fast_model):
        if name not in out:
            out.append(name)
    return out


def model_installed(name: str, installed: set[str]) -> bool:
    # `ollama list` shows "llama3:latest" for a model pulled as "llama3"
    return name in installed or (":" not in name and f"{name}:latest" in installed)


def ollama_status(timeout: float = 2.0) -> dict:
    """{'reachable': bool, 'installed': [...], 'missing': [...], 'error': str|None}"""
    if settings.llm_backend == "mock":
        return {"reachable": True, "installed": [], "missing": [], "error": None}
    import httpx
    url = settings.ollama_base_url
    try:
        r = httpx.get(f"{url}/api/tags", timeout=timeout)
        r.raise_for_status()
        installed = {m["name"] for m in r.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return {"reachable": False, "installed": [], "error":
                f"Ollama is not reachable at {url} ({type(exc).__name__}). "
                "Start it with `ollama serve`, or fix ORCHESTRA_OLLAMA_BASE_URL.",
                "missing": required_models()}
    missing = [m for m in required_models() if not model_installed(m, installed)]
    return {"reachable": True, "installed": sorted(installed),
            "missing": missing, "error": None}


# The browser polls /api/health; a 5-second cache keeps that from hammering
# Ollama while still noticing a restart quickly.
_cache: dict = {"at": 0.0, "value": None}
_CACHE_TTL_S = 5.0


def cached_ollama_status() -> dict:
    now = time.monotonic()
    if _cache["value"] is None or now - _cache["at"] > _CACHE_TTL_S:
        _cache["value"] = ollama_status()
        _cache["at"] = now
    return _cache["value"]


# ── Warm-up ────────────────────────────────────────────────────────
_warm = {"state": "idle", "detail": ""}
_warm_lock = threading.Lock()


def warm_state() -> dict:
    with _warm_lock:
        return dict(_warm)


def _set_warm(state: str, detail: str = "") -> None:
    with _warm_lock:
        _warm.update(state=state, detail=detail)


def warm_models(timeout: float = 300.0) -> dict:
    """Load each required model into Ollama's memory with the SAME context
    window the pipeline will request (a different num_ctx would make Ollama
    reload the model on the first real call, defeating the point)."""
    status = ollama_status()
    if not status["reachable"]:
        _set_warm("failed", status["error"])
        return warm_state()
    todo = [m for m in required_models() if m not in status["missing"]]
    if not todo:
        _set_warm("failed", "models not installed: " + ", ".join(status["missing"]))
        return warm_state()
    import httpx
    _set_warm("warming", ", ".join(todo))
    t0 = time.monotonic()
    try:
        for model in todo:
            r = httpx.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": model, "prompt": "", "stream": False,
                      "keep_alive": settings.keep_alive,
                      "options": {"num_ctx": settings.num_ctx}},
                timeout=timeout)
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("WARMUP | failed: %s", exc)
        _set_warm("failed", f"{type(exc).__name__}: {exc}")
        return warm_state()
    _set_warm("ready", f"{', '.join(todo)} loaded in {time.monotonic() - t0:.1f}s")
    logger.info("WARMUP | %s", _warm["detail"])
    return warm_state()


def start_warmup() -> bool:
    """Warm in a background thread; returns False if one is already running."""
    with _warm_lock:
        if _warm["state"] == "warming":
            return False
        _warm.update(state="warming", detail="starting")
    threading.Thread(target=warm_models, name="orchestra-warmup",
                     daemon=True).start()
    return True
