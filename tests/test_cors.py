"""CORS on the streamable-HTTP endpoint (corpus-toolkit#37).

A browser MCP client could not talk to any corpus at all. Two independent gaps, and a fix
for only the first produces a WORSE failure than no fix:

  1. the SDK's transport security 403s an unlisted `Origin` before any handler runs;
  2. nothing emits `Access-Control-*` headers, and `OPTIONS` is not a route (405).

And the half-fix: streamable HTTP hands back the session id in a RESPONSE HEADER, which a
browser cannot read unless the server exposes it. Wrap the app without `expose_headers`
and the preflight passes, `initialize` returns 200, and the client dies on `Bad Request:
Missing session ID` — an error pointing nowhere near CORS.

These run a REAL server on a REAL socket and send REAL preflights, because every one of
those failures is a header-level behaviour that a mocked app would answer wrongly.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from corpus_toolkit.mcp import _sdk


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    import uvicorn
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if getattr(server, "started", False):
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def _request(url, method, headers):
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


@pytest.fixture
def cors_server():
    origin = "https://claude.ai"
    security = _sdk.TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", origin],
    )
    server = _sdk.Server("test")
    kw = _sdk.http_kwargs(host="127.0.0.1", port=_free_port(), path="/mcp",
                          transport_security=security)
    app = _sdk.with_cors(_sdk.build_http_app(server, kw), [origin])
    uv = _serve(app, kw["port"])
    yield f"http://127.0.0.1:{kw['port']}/mcp", origin
    uv.should_exit = True


def test_preflight_is_answered_and_not_405(cors_server):
    """OPTIONS is not a route on the bare app — the middleware has to answer it."""
    url, origin = cors_server
    status, headers = _request(url, "OPTIONS", {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,mcp-session-id",
    })
    assert status == 200, f"preflight got {status}; bare app 405s because OPTIONS is not a route"
    assert headers.get("access-control-allow-origin") == origin


def test_session_id_header_is_exposed_on_the_real_response(cors_server):
    """The half-fix guard, asserted where the header actually appears.

    `Access-Control-Expose-Headers` is sent on the ACTUAL response, never on the
    preflight — an earlier version of this test checked the OPTIONS reply and failed
    against a correct implementation. So this performs a real `initialize`, which is the
    exact request whose response header a browser must be able to read.
    """
    url, origin = cors_server
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Origin": origin, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=5) as r:
        headers = {k.lower(): v for k, v in r.headers.items()}
        assert r.headers.get("mcp-session-id"), "handshake did not return a session id"
    exposed = (headers.get("access-control-expose-headers") or "").lower()
    assert "mcp-session-id" in exposed, (
        "a browser cannot read mcp-session-id unless it is exposed; omitting it makes "
        "initialize succeed and the NEXT request fail with 'Missing session ID'")
    assert headers.get("access-control-allow-origin") == origin


def test_required_request_headers_are_allowed(cors_server):
    url, origin = cors_server
    _, headers = _request(url, "OPTIONS", {
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,mcp-session-id"})
    allowed = (headers.get("access-control-allow-headers") or "").lower()
    for h in ("content-type", "mcp-session-id"):
        assert h in allowed, f"{h} must be permitted or the client cannot send it"


def test_disallowed_origin_gets_no_allow_origin_header(cors_server):
    """Opt-in means an unlisted origin is not silently accepted."""
    url, _ = cors_server
    _, headers = _request(url, "OPTIONS", {
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert headers.get("access-control-allow-origin") not in ("https://evil.example", "*")


def test_without_cors_the_endpoint_still_serves_non_browser_clients():
    """CORS is additive. Every current consumer connects server-side, where no Origin is
    sent at all, and must be unaffected by this change."""
    server = _sdk.Server("test")
    kw = _sdk.http_kwargs(host="127.0.0.1", port=_free_port(), path="/mcp")
    app = _sdk.build_http_app(server, kw)
    uv = _serve(app, kw["port"])
    try:
        # No Origin header — the server-side case. A POST without a session reaches the
        # handler and is rejected on MCP grounds (4xx), not blocked by transport security.
        status, _ = _request(f"http://127.0.0.1:{kw['port']}/mcp", "POST",
                             {"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream"})
        assert status != 403, "a server-side client sends no Origin and must not be blocked"
    finally:
        uv.should_exit = True


def test_with_cors_does_not_change_the_mounted_path():
    """Wrapping must not move the endpoint — a path-routing tunnel would 404 everything."""
    server = _sdk.Server("test")
    kw = _sdk.http_kwargs(host="127.0.0.1", port=8000, path="/oregon-budget/mcp")
    app = _sdk.build_http_app(server, kw)
    assert "/oregon-budget/mcp" in [getattr(r, "path", None) for r in app.routes]
    wrapped = _sdk.with_cors(app, ["https://claude.ai"])
    assert getattr(wrapped, "app", None) is app
