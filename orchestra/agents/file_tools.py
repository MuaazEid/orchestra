"""File tools — sandboxed to ~/.orchestra/workspace.

Safety by construction:
- Every path is resolved and MUST stay inside the workspace. "../" tricks,
  absolute paths, drive hops — all rejected before touching the disk.
  The agent gets a workshop, not the keys to the machine.
- Reads are size-capped so a huge file can't flood the model's context.
"""
from __future__ import annotations

from pathlib import Path

from ..core.config import settings
from .toolbox import tool

_MAX_READ_CHARS = 8000
_MAX_WRITE_CHARS = 200_000


def _workspace() -> Path:
    ws = settings.data_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _safe(rel_path: str) -> Path:
    # Normalize Windows separators FIRST, then reject any ".." segment
    # outright — belt and suspenders on top of the resolve() check, and
    # identical behavior on Linux (dev) and Windows (target machine).
    norm = rel_path.replace("\\", "/")
    if any(part == ".." for part in norm.split("/")):
        raise PermissionError(f"path '{rel_path}' contains '..' — not allowed")
    ws = _workspace().resolve()
    target = (ws / norm).resolve()
    if ws != target and ws not in target.parents:
        raise PermissionError(
            f"path '{rel_path}' escapes the workspace — only relative paths "
            "inside the workspace are allowed")
    return target


@tool
def list_files(subfolder: str = "") -> str:
    """List files in the agent workspace. subfolder: optional relative folder
    to list; empty lists the workspace root."""
    base = _safe(subfolder or ".")
    if not base.exists():
        return f"Folder '{subfolder}' does not exist."
    entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return "(empty)"
    lines = []
    for p in entries:
        if p.is_dir():
            lines.append(f"[dir]  {p.name}/")
        else:
            lines.append(f"[file] {p.name} ({p.stat().st_size} bytes)")
    return "\n".join(lines)


@tool
def read_file(path: str) -> str:
    """Read a text file from the workspace. path: relative path like
    'notes.txt' or 'reports/summary.md'."""
    p = _safe(path)
    if not p.exists() or not p.is_file():
        return f"File '{path}' does not exist."
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > _MAX_READ_CHARS:
        return (text[:_MAX_READ_CHARS]
                + f"\n...[truncated — file has {len(text)} chars total]")
    return text


@tool
def write_file(path: str, content: str) -> str:
    """Write text to a file in the workspace (creates or overwrites).
    path: relative path like 'notes.txt'. content: the full text to write."""
    if len(content) > _MAX_WRITE_CHARS:
        return f"Error: content too large ({len(content)} chars)."
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


@tool
def append_file(path: str, content: str) -> str:
    """Append text to the end of a workspace file (creates it if missing).
    path: relative path. content: text to add."""
    if len(content) > _MAX_WRITE_CHARS:
        return f"Error: content too large ({len(content)} chars)."
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(content)
    return f"Appended {len(content)} chars to {path}"
