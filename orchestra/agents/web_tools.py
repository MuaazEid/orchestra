"""Web fetch tool — stdlib only, zero new dependencies.

Honest scope: this FETCHES a given URL and extracts readable text. It is
not a search engine. Good for "summarize this page", "what does this
article say". http/https only, size- and time-capped.

Safety: the URL comes from a language model, and a fetched page can itself
contain instructions aimed at that model (prompt injection). So the tool
refuses to reach loopback, private, link-local or otherwise non-public
addresses - including via redirects - unless ORCHESTRA_ALLOW_PRIVATE_FETCH
is set. Known residual risk: the name is resolved once for the check and
again by the HTTP stack, so a hostile DNS server could still rebind between
the two; closing that fully needs connecting to the validated IP.
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.parse
import urllib.request

from ..core.config import settings
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


def _validate_url(url: str) -> None:
    """Raise ValueError unless `url` is a public http(s) address."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("url must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("url has no host")
    if parts.username or parts.password:
        raise ValueError("urls with embedded credentials are not allowed")
    if settings.allow_private_fetch:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443,
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host '{parts.hostname}' ({exc})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise ValueError(
                f"blocked: '{parts.hostname}' resolves to a non-public address "
                f"({ip}); fetch_webpage only reaches public sites")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop - a public URL can 302 to localhost."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirect)


def _decode(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:  # a charset name Python does not know
        return raw.decode("utf-8", errors="replace")


@tool
def fetch_webpage(url: str) -> str:
    """Fetch a web page and return its readable text. url: full address
    starting with http:// or https://."""
    try:
        _validate_url(url)
    except ValueError as exc:
        return f"Error: {exc}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _opener.open(req, timeout=_TIMEOUT_S) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return f"Error: unsupported content type '{ctype}'"
            raw = _decode(resp.read(1_500_000), resp.headers.get_content_charset())
    except ValueError as exc:  # a redirect to a blocked address
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: could not fetch {url} ({exc})"
    text = _extract_text(raw)
    if not text:
        return "Error: page contained no readable text."
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + " ...[truncated]"
    return text
