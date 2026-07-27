"""The streamable-HTTP mount path.

Several corpora share one hostname behind a path-routing Cloudflare Tunnel. A tunnel
matches on path but does NOT strip it, so each server must mount at the same prefix its
route matches or every request 404s — with nothing in any log to say why.

`--path` therefore has to be verified, not assumed: it sets a *settings* field on the MCP
SDK's FastMCP, and the SDK has been reshaping this area (FastMCP -> MCPServer; the mount
has appeared as both a setting and a run() kwarg). These tests fail loudly if an SDK
upgrade stops honouring it, which is the whole point.
"""
import pytest

from mcp.server.fastmcp import FastMCP


def _mounted(path: str) -> list[str]:
    mcp = FastMCP("test")
    mcp.settings.streamable_http_path = path
    return [getattr(r, "path", None) for r in mcp.streamable_http_app().routes]


def test_the_sdk_still_exposes_the_mount_setting():
    """server.py exits rather than starting if this disappears; catch it here first."""
    assert hasattr(FastMCP("test").settings, "streamable_http_path")


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
