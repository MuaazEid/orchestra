"""Job discovery — real web search via Tavily's API (built for AI agents,
not scraping job sites directly — avoids ToS/bot-detection risk entirely).

Requires ORCHESTRA_TAVILY_API_KEY in the user's own .env — never hardcoded,
never passed through chat. Costs Tavily credits (free tier: 1000/month), so
this tool makes exactly one search per call — no silent fan-out.
"""
from __future__ import annotations

import json
import urllib.request

from ..core.config import settings
from .toolbox import tool

_ENDPOINT = "https://api.tavily.com/search"
_TIMEOUT_S = 20


@tool
def search_jobs(query: str) -> str:
    """Search the live web for job postings. query: a specific search like
    'AI engineer jobs Riyadh Bayt' or 'physical security engineer Riyadh
    hiring'. Uses one Tavily API credit per call \u2014 make the query specific
    rather than calling this repeatedly for the same need."""
    if not settings.tavily_api_key:
        return ("Error: no Tavily API key configured. Set "
                "ORCHESTRA_TAVILY_API_KEY in your local .env file first.")
    body = json.dumps({
        "api_key": settings.tavily_api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
    }).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return f"Error: Tavily search failed ({exc})"
    results = data.get("results", [])
    if not results:
        return "No results found for that query."
    lines = []
    for r in results:
        title = r.get("title", "(no title)")
        url = r.get("url", "")
        snippet = (r.get("content", "") or "")[:200]
        lines.append(f"- {title}\n  {url}\n  {snippet}")
    return "\n".join(lines)
