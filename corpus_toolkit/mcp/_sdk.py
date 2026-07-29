"""The one place that knows which major of the `mcp` SDK is installed.

WHY THIS FILE EXISTS. `mcp` 2.0.0 deleted `mcp.server.fastmcp` outright — there is no
alias, no deprecation shim, no `FastMCP` name anywhere in the wheel. `server.py` imported
it at module scope, and the toolkit's `mcp` extra was an unbounded `mcp[cli]`, so the day
2.0.0 shipped every fresh install resolved onto it and `corpus-mcp-serve` became
unimportable. CI on main went red on a docs-only commit.

The worst of it was where the failure landed. A corpus image does:

    RUN pip install --no-cache-dir -r requirements.txt      # -> mcp 2.0.0
    RUN python3 -c "...corpus_toolkit.mcp.framework import CorpusFramework...ensure_index()"

That second line — the build's own smoke check — imports `framework`, which is stdlib-only
and imports fine on both majors. So **the image builds green and the container cannot
start**, and the only signal is a crash loop at deploy time. Measured, not inferred: see
the transcript in corpus-toolkit#13.

WHY BOTH MAJORS RATHER THAN PICKING ONE. A corpus pins a toolkit TAG; it does not pin the
SDK. The SDK therefore floats at image-build time regardless of how carefully the corpus
pins the toolkit, which is exactly how this happened. Supporting one major and bounding the
other just moves the cliff to `mcp` 3.0. Code that spans the boundary, plus a CI matrix
that runs the suite under both, is the durable answer — and the surface turned out to be
two things: the import, and how transport options are supplied.

WHAT ACTUALLY DIFFERS (verified against 1.28.1 and 2.0.0, not read off a changelog):

  |                        | 1.28.1                          | 2.0.0                        |
  |------------------------|---------------------------------|------------------------------|
  | class                  | mcp.server.fastmcp.FastMCP      | mcp.server.mcpserver.MCPServer|
  | @x.tool(), @x.resource | identical                       | identical                    |
  | _tool_manager.list_tools | sync                          | sync                         |
  | _tool_manager.call_tool  | context optional              | context REQUIRED             |
  | host/port/path/security  | MUTABLE settings.*            | kwargs on run()/app()        |
  | session manager          | _session_manager (private)    | session_manager (public)     |
  | TransportSecuritySettings| mcp.server.transport_security | same import path             |

The settings->kwargs move is the substantive one, and it is an improvement: on 1.x the
session manager captured `settings.transport_security` at the FIRST
`streamable_http_app()` call and cached it for the process, so anything set afterwards was
silently ignored — that is the bug that made every tunnelled request 421 in v1.5.0. On
2.0 each build honours its own kwargs (verified: two builds with different settings each
take effect). The guard in `server.py` stays regardless, because the point of it was never
to describe one SDK's internals — it was to refuse to start rather than serve a
configuration that had been silently dropped.
"""
from __future__ import annotations

import sys

# Import path is the whole compatibility question; everything else follows from it.
try:
    from mcp.server.mcpserver import MCPServer as Server       # mcp >= 2
    SDK_MAJOR = 2
except ModuleNotFoundError:                                    # pragma: no cover
    from mcp.server.fastmcp import FastMCP as Server           # mcp 1.x
    SDK_MAJOR = 1

# Same module path on both majors. Imported here so callers never have to care.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402,F401

__all__ = ["SDK_MAJOR", "Server", "TransportSecuritySettings", "sdk_version",
           "http_kwargs", "build_http_app", "run_http", "session_allowed_hosts",
           "tool_names", "call_tool"]


def sdk_version() -> str:
    """Installed SDK version, for startup logging. Worth printing: the failure this
    module exists for is invisible in the toolkit's own version number."""
    try:
        import importlib.metadata as md
        return md.version("mcp")
    except Exception:                                          # noqa: BLE001
        return "unknown"


def http_kwargs(*, host: str, port: int, path: str,
                transport_security=None) -> dict:
    """The streamable-HTTP options as ONE dict.

    Built once and passed to both `build_http_app` and `run_http` by the caller, and that
    discipline is load-bearing on 2.x: `run()` there constructs its own app from its own
    kwargs, so a mount verified from a separately-argued build would be a check on a
    different object than the one served — a check that passes for the wrong reason. One
    dict, two uses, no way for them to drift."""
    kw = {"host": host, "port": port, "streamable_http_path": path}
    if transport_security is not None:
        kw["transport_security"] = transport_security
    return kw


def build_http_app(server, kwargs: dict):
    """Build the streamable-HTTP app, applying `kwargs` the way this major requires.

    On 1.x that means mutating `settings` FIRST and then building, because the build
    freezes them. On 2.x the values are arguments to the build itself."""
    if SDK_MAJOR >= 2:
        return server.streamable_http_app(**{k: v for k, v in kwargs.items()
                                             if k != "port"})
    server.settings.host = kwargs["host"]
    server.settings.port = kwargs["port"]
    server.settings.streamable_http_path = kwargs["streamable_http_path"]
    if "transport_security" in kwargs:
        server.settings.transport_security = kwargs["transport_security"]
    return server.streamable_http_app()


def run_http(server, kwargs: dict) -> None:
    """Serve. On 1.x the settings were already applied by build_http_app and `run` reuses
    the cached session manager; on 2.x the options travel as arguments."""
    if SDK_MAJOR >= 2:
        server.run(transport="streamable-http", **kwargs)
    else:
        server.run(transport="streamable-http")


def session_allowed_hosts(server) -> list[str]:
    """Host allow-list the session manager ACTUALLY captured — not what was requested.

    Reported rather than assumed because the two diverged once already and the symptom
    (every proxied request 421s, correct value sitting unused in config) points nowhere
    near the cause. Returns [] when there is no session manager yet; on 2.x the property
    raises in that state, which must not become a startup crash."""
    try:
        sm = server.session_manager if SDK_MAJOR >= 2 else server._session_manager
    except Exception:                                          # noqa: BLE001
        return []
    sec = getattr(sm, "security_settings", None)
    return list(getattr(sec, "allowed_hosts", []) or []) if sec else []


def tool_names(server) -> set[str]:
    """Registered tool names, synchronously. The tool manager's `list_tools` is sync on
    both majors while the server's is a coroutine — using the sync one keeps a
    before/after name diff out of an event loop."""
    return {t.name for t in server._tool_manager.list_tools()}


async def call_tool(server, name: str, arguments: dict):
    """Invoke a tool and return its RAW Python return value on either major.

    Deliberately not `MCPServer.call_tool`, whose 2.x form wraps the result in a
    `CallToolResult`: the release gate asserts on the tool's actual answer (that an
    external graph neighbour comes back `{citation, external: true}`, that a document body
    contains its text), and asserting through a serialization wrapper would test the SDK's
    marshalling instead of the toolkit's behaviour.

    `context` is optional on 1.x and required on 2.x."""
    tm = server._tool_manager
    if SDK_MAJOR >= 2:
        from mcp.server.mcpserver import Context
        ctx = Context(mcp_server=server, subscriptions=server._subscriptions)
        return await tm.call_tool(name, arguments, ctx, convert_result=False)
    return await tm.call_tool(name, arguments)


def report(stream=sys.stderr) -> None:
    print(f"[corpus-mcp] mcp SDK {sdk_version()} (major {SDK_MAJOR})", file=stream)
