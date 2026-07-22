"""Web fetch tool — stdlib only, zero new dependencies.

Honest scope: this FETCHES a given URL and extracts readable text. It is
not a search engine. Good for "summarize this page", "what does this
article say". http/https only, size- and time-capped.
"""
from __future__ import annotations

import html
import re
import urllib.request

from .toolbox import tool

_MAX_CHARS = 2500   # tight on purpose: a 7B model on a laptop chokes on
                    # long context — every ReAct step re-reads the whole
                    # transcript. A focused excerpt keeps steps fast.
_TIMEOUT_S = 15
_UA = "Mozilla/5.0 (OrchestraLocalAgent/2.0)"


def _extract_text(raw_html: str) -> str:
    # strip script/style, then tags, then collapse whitespace
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


@tool
def fetch_webpage(url: str) -> str:
    """Fetch a web page and return its readable text. url: full address
    starting with http:// or https://."""
    if not re.match(r"^https?://", url):
        return "Error: url must start with http:// or https://"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return f"Error: unsupported content type '{ctype}'"
            raw = resp.read(1_500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Error: could not fetch {url} ({exc})"
    text = _extract_text(raw)
    if not text:
        return "Error: page contained no readable text."
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + " ...[truncated]"
    return text
