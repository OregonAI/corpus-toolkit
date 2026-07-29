"""The streamable-HTTP mount path, and the transport-config seam it sits on.

Several corpora share one hostname behind a path-routing Cloudflare Tunnel. A tunnel
matches on path but does NOT strip it, so each server must mount at the same prefix its
route matches or every request 404s — with nothing in any log to say why.

`--path` therefore has to be verified, not assumed. These tests are written against
`corpus_toolkit.mcp._sdk` rather than against a specific SDK class, because the SDK did
exactly what the old version of this file warned it might: mcp 2.0.0 deleted
`mcp.server.fastmcp` and moved the mount from a mutable `settings` field to a keyword
argument. The old tests asserted the SETTING existed, so on 2.x they did not fail — they
could not even be collected, which is a worse outcome than failing.

Everything here therefore asserts BEHAVIOUR (does the app answer at this path, did the
allow-list reach the session manager) and runs unchanged on both majors. Verified against
mcp 1.28.1 and 2.0.0.
"""
import pytest

from corpus_toolkit.mcp import _sdk


def _app(path, security=None):
    server = _sdk.Server("test")
    return _sdk.build_http_app(
        server, _sdk.http_kwargs(host="127.0.0.1", port=8000, path=path,
                                 transport_security=security)), server


def _mounted(path: str) -> list[str]:
    app, _ = _app(path)
    return [getattr(r, "path", None) for r in app.routes]


def test_the_sdk_major_is_one_we_have_actually_tested():
    """The compat seam supports 1.x and 2.x. A 3.0 would land here silently otherwise —
    `Server` would still import if a future SDK kept either name, and every behavioural
    test below could pass while some untested third arrangement was in play."""
    assert _sdk.SDK_MAJOR in (1, 2), _sdk.sdk_version()


def test_default_mount_is_unchanged():
    """--path defaults to /mcp, so existing single-corpus deployments must not move."""
    assert "/mcp" in _mounted("/mcp")


@pytest.mark.parametrize("path", [
    "/executive-regulatory-frameworks/mcp",
    "/oregon-records-retention/mcp",
    "/oregon-legislature/mcp",
])
def test_corpus_prefixed_mounts(path):
    assert path in _mounted(path)


def test_a_prefixed_mount_does_not_also_answer_at_the_bare_path():
    """If both answered, path routing would appear to work while actually being ignored —
    the failure mode this flag exists to prevent would pass every smoke test."""
    mounted = _mounted("/oregon-legislature/mcp")
    assert "/mcp" not in mounted, mounted


def test_transport_security_reaches_the_session_manager():
    """The v1.5.0 outage, asserted on the property that actually matters.

    --public-hostname was applied AFTER the app was built; on 1.x the session manager had
    already captured settings.transport_security and cached it for the process, so the
    allow-list stayed localhost-only and every tunnelled request 421'd while the correct
    hostname sat unused in config. server.py refuses to start when this is empty, so this
    test is what stands behind that refusal being meaningful."""
    sec = _sdk.TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["example.com"])
    _, server = _app("/c/mcp", sec)
    assert "example.com" in _sdk.session_allowed_hosts(server)


def test_no_security_settings_yields_an_empty_list_not_an_exception():
    """On 2.x `session_manager` RAISES before the first app build. server.py calls this
    while assembling a startup error message, so a raise there would replace a precise
    diagnostic with a traceback — the exact substitution this whole area keeps making."""
    assert _sdk.session_allowed_hosts(_sdk.Server("test")) == []


def test_tool_names_sees_registrations():
    server = _sdk.Server("test")
    assert _sdk.tool_names(server) == set()

    @server.tool()
    def probe_tool(x: str) -> dict:
        """probe"""
        return {"x": x}

    assert _sdk.tool_names(server) == {"probe_tool"}


def test_tool_names_works_from_inside_a_running_event_loop():
    """server.py diffs tool names around a corpus's tools_module to catch a hook that
    registers nothing, and must not need an event loop of its own to do it.

    Written this way because the obvious version does not test its own name. An earlier
    draft asserted only that `tool_names()` returned the right set; reimplementing it as
    `asyncio.run(server.list_tools())` kept that assertion green, so the test would have
    passed a version that explodes the moment build_server is called from async context
    ("asyncio.run() cannot be called from a running event loop"). Calling it from inside
    a live loop is what actually discriminates the two."""
    import asyncio
    import inspect

    assert not inspect.iscoroutinefunction(_sdk.tool_names)

    server = _sdk.Server("test")

    @server.tool()
    def probe_tool(x: str) -> dict:
        """probe"""
        return {"x": x}

    async def from_async():
        return _sdk.tool_names(server)

    assert asyncio.run(from_async()) == {"probe_tool"}


def test_call_tool_returns_the_raw_python_value_on_either_major():
    """The release gate asserts on what a tool ANSWERED — that an external neighbour comes
    back {citation, external: true}. 2.x's public call_tool wraps results in a
    CallToolResult; asserting through that would test the SDK's marshalling instead."""
    import asyncio

    server = _sdk.Server("test")

    @server.tool()
    def graph_neighbors(doc_id: str) -> dict:
        """neighbours"""
        return {"id": doc_id, "related": [{"citation": "OAR 166-300-0015",
                                           "external": True}]}

    got = asyncio.run(_sdk.call_tool(server, "graph_neighbors", {"doc_id": "d1"}))
    assert got == {"id": "d1",
                   "related": [{"citation": "OAR 166-300-0015", "external": True}]}
