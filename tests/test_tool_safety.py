"""fetch_webpage network safety (SSRF), charset handling, and Unicode-aware
memory recall."""
import http.server
import os
import threading

os.environ["ORCHESTRA_LLM_BACKEND"] = "mock"
os.environ["ORCHESTRA_DATA_DIR"] = "/tmp/orchestra-test"

import pytest

from orchestra.agents import web_tools
from orchestra.agents.toolbox import run_tool
from orchestra.core.config import settings

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def dns(monkeypatch):
    """Fake DNS: a name -> IP table (anything else resolves to PUBLIC_IP)."""
    table = {"localhost": "127.0.0.1", "internal.corp": "10.0.0.5",
             "metadata.google.internal": "169.254.169.254"}

    def fake(host, port, type=0, **kw):
        ip = table.get(host, host if host.replace(".", "").isdigit() or ":" in host else PUBLIC_IP)
        return [(2, 1, 6, "", (ip, port))]
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", fake)
    return table


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost:8080/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://internal.corp/",
    "http://10.1.2.3/",
    "http://192.168.1.1/",
    "http://172.16.0.9/",
    "http://100.64.0.1/",               # carrier-grade NAT
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",       # IPv4-mapped loopback
])
def test_non_public_addresses_are_blocked(url, dns):
    with pytest.raises(ValueError, match="blocked"):
        web_tools._validate_url(url)
    assert run_tool("fetch_webpage", {"url": url}).startswith("Error: blocked")


@pytest.mark.parametrize("url,why", [
    ("ftp://example.com/x", "http"),
    ("file:///etc/passwd", "http"),
    ("http://user:pw@example.com/", "credentials"),
    ("http:///nohost", "no host"),
])
def test_bad_schemes_and_credentials_are_rejected(url, why, dns):
    with pytest.raises(ValueError, match=why):
        web_tools._validate_url(url)


def test_a_public_address_passes_validation(dns):
    web_tools._validate_url("https://example.com/article")
    web_tools._validate_url(f"http://{PUBLIC_IP}/")


def test_a_redirect_to_localhost_is_blocked(dns):
    handler = web_tools._GuardedRedirect()
    req = web_tools.urllib.request.Request("http://example.com/")
    with pytest.raises(ValueError, match="blocked"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")


def test_unresolvable_host_is_a_clean_error(monkeypatch):
    def nope(*a, **k):
        raise web_tools.socket.gaierror("no such host")
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", nope)
    assert "could not resolve" in run_tool("fetch_webpage", {"url": "http://nope.invalid/"})


# ── Real fetch against a local server (private access explicitly allowed) ──
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = "<html><body><p>café crème</p><script>x()</script></body></html>".encode("latin-1")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=iso-8859-1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_fetch_decodes_using_the_declared_charset(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_fetch", True)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = run_tool("fetch_webpage", {"url": f"http://127.0.0.1:{srv.server_port}/"})
    finally:
        srv.shutdown()
    assert out == "café crème"          # latin-1 decoded, script stripped


def test_private_fetch_is_blocked_by_default_even_for_a_live_local_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = run_tool("fetch_webpage", {"url": f"http://127.0.0.1:{srv.server_port}/"})
    finally:
        srv.shutdown()
    assert out.startswith("Error: blocked")


def test_unknown_charset_falls_back_to_utf8():
    assert web_tools._decode("hé".encode("utf-8"), "not-a-charset") == "hé"


# ── Unicode-aware recall ───────────────────────────────────────────
def test_recall_matches_arabic_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    run_tool("remember_about_user", {"key": "اللون_المفضل", "fact": "الأزرق"})
    run_tool("remember_about_user", {"key": "city", "fact": "Riyadh"})
    out = run_tool("recall_about_user", {"query": "ما هو لوني المفضل"})
    assert "الأزرق" in out and "Riyadh" not in out    # filtered, not "return everything"
