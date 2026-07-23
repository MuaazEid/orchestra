"""Career tools — deterministic job-fit scoring + application tracking.

Design (same philosophy as the rest of Orchestra): don't trust the LLM to
"just know" the fit score — compute it deterministically from a background
file the user controls, then let the LLM reason ON TOP of that number. This
mirrors the routing backstop and terminal-tools pattern used elsewhere.

The user maintains their own `background.txt` in the workspace (their real
skills/experience, in their own words) — this tool never embeds anyone's
personal data in source code.
"""
from __future__ import annotations

import re
from datetime import datetime

from .file_tools import _safe
from .toolbox import tool

_BACKGROUND_FILE = "background.txt"
_LOG_FILE = "applications_log.md"


def _keywords(text: str) -> set[str]:
    # Technical/skill-bearing tokens: alphanumerics of length >= 3, plus
    # common multi-word tech terms normalized to single tokens.
    text = text.lower()
    for phrase, token in [
        ("machine learning", "ml"), ("large language model", "llm"),
        ("computer vision", "cv"), ("natural language processing", "nlp"),
        ("access control", "access_control"), ("video management", "vms"),
    ]:
        text = text.replace(phrase, token)
    return {w for w in re.findall(r"[a-z0-9_]{3,}", text)}


@tool
def score_job_fit(posting_text: str) -> str:
    """Score how well a job posting matches the user's background.
    posting_text: the full text of the job posting (paste it in).
    Reads background.txt from the workspace for comparison; reports which
    of the user's own skills/keywords appear in the posting and a rough
    match percentage. Deterministic — same inputs always give the same score."""
    bg_path = _safe(_BACKGROUND_FILE)
    if not bg_path.exists():
        return (f"Error: {_BACKGROUND_FILE} not found in the workspace. "
                f"Save your skills/experience there first (e.g. ask the "
                f"File Clerk to write it), then try again.")
    background = bg_path.read_text(encoding="utf-8", errors="replace")
    bg_words = _keywords(background)
    post_words = _keywords(posting_text)
    if not bg_words:
        return "Error: background.txt is empty."
    matched = sorted(bg_words & post_words)
    pct = round(100 * len(matched) / max(len(bg_words), 1))
    tier = "STRONG" if pct >= 30 else "MODERATE" if pct >= 15 else "WEAK"
    return (f"Fit: {tier} ({pct}% of your background keywords appear in "
            f"this posting)\nMatched: {', '.join(matched) if matched else '(none)'}")


@tool
def log_application(company: str, role: str, fit_summary: str) -> str:
    """Append one row to the application tracking log (applications_log.md).
    company: employer name. role: job title. fit_summary: one-line note
    (fit tier, key match, or anything worth remembering)."""
    date = datetime.now().strftime("%Y-%m-%d")
    line = f"- {date} | {company} | {role} | {fit_summary}\n"
    log_path = _safe(_LOG_FILE)
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("# Application Log\n\n", encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)
    return f"Logged: {company} — {role}"
