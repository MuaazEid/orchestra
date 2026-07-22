"""Tool registry — the company's equipment room.

v1 lesson #10 (the `remember` vs `remember_about_user` KeyError) is now
IMPOSSIBLE by design: registration reads the function's own __name__,
so the registry key and the name the LLM sees are always identical.

Adding a tool = write a function + one decorator. Nothing else to touch.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Callable] = {}


def tool(fn: Callable) -> Callable:
    """Register `fn` under its own __name__. Docstring becomes the LLM's manual."""
    name = fn.__name__
    if not fn.__doc__:
        raise ValueError(f"tool '{name}' must have a docstring — the LLM reads it")
    if name in _REGISTRY:
        raise ValueError(f"tool '{name}' registered twice")
    _REGISTRY[name] = fn
    return fn


def get_tools(names: list[str]) -> list[Callable]:
    """Resolve names -> callables. Unknown names fail LOUDLY at assignment
    time (supervisor), not silently at execution time (specialist)."""
    missing = [n for n in names if n not in _REGISTRY]
    if missing:
        raise KeyError(f"unknown tools: {missing}. Known: {sorted(_REGISTRY)}")
    return [_REGISTRY[n] for n in names]


def run_tool(name: str, args: dict[str, Any]) -> str:
    """Execute one tool call with argument validation and safe errors."""
    args_view = str(args)[:200]
    if name not in _REGISTRY:
        logger.warning("TOOL %-20s UNKNOWN | args=%s", name, args_view)
        return f"Error: unknown tool '{name}'. Available: {sorted(_REGISTRY)}"
    fn = _REGISTRY[name]
    try:
        # Validate args against the signature BEFORE calling — catches the
        # small-model habit of inventing parameter names.
        inspect.signature(fn).bind(**args)
    except TypeError as exc:
        logger.warning("TOOL %-20s BAD-ARGS | args=%s | %s",
                       name, args_view, exc)
        return (f"Error: bad arguments for '{name}': {exc}. "
                f"Required signature: {name}{inspect.signature(fn)}")
    try:
        out = str(fn(**args))
        logger.info("TOOL %-20s ok | args=%s | out=%s",
                    name, args_view, out[:120].replace("\n", " "))
        return out
    except Exception as exc:
        logger.warning("TOOL %-20s RAISED | args=%s | %r",
                       name, args_view, exc)
        return f"Error: {name} raised {exc!r}"


def registry_names() -> list[str]:
    return sorted(_REGISTRY)
