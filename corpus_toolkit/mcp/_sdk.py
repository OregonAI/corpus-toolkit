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

That table is the SERVER side. The CLIENT side differs in six further ways and is spanned at
the bottom of this file — see "THE CLIENT SIDE". It was missing until corpus-gateway (a server
that is also a client to nine corpora) hit every one of them across four failed deploys, two
of them silently. Anything in this org that opens an MCP connection goes through
`open_client_streams` for the same reason `server.py` goes through `Server`.

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
           "tool_names", "call_tool",
           # The client half of the same seam — see "THE CLIENT SIDE" below.
           "CLIENT_SYMBOL", "open_client_streams", "tool_input_schema",
           "result_is_error"]


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


# --------------------------------------------------------------------------- THE CLIENT SIDE
#
# Everything above spans the majors for code that IS an MCP server. Everything below spans
# them for code that TALKS TO one. The second half was missing, and the omission was not
# theoretical: `corpus-gateway` — a server that is also a client to all nine corpora —
# crash-looped on its first deploy and then failed its healthcheck three more times, one
# break at a time, each fix revealing the next.
#
# WHY THE EXTRA'S BOUND DOES NOT HELP HERE. `mcp[cli]>=1.28,<3` is bounded deliberately and
# deliberately admits BOTH majors, because this module spans them. A consumer that only
# serves inherits that spanning. A consumer that CONNECTS inherited the admission without
# the spanning: same bound, same install, no seam. Narrowing the bound would be the wrong
# fix — it would strand the server side, which genuinely works on both.
#
# SIX BREAKS, and only the first announces itself:
#
#   | # | thing                     | 1.28.1                        | 2.0.0                  |
#   |---|---------------------------|-------------------------------|------------------------|
#   | 1 | client name               | streamablehttp_client         | streamable_http_client |
#   | 2 | client signature          | (url, headers, timeout, ...)  | (url, *, http_client)  |
#   | 3 | http library              | httpx                         | httpx2                 |
#   | 4 | yielded arity             | (read, write, get_session_id) | (read, write)          |
#   | 5 | Tool.inputSchema          | camelCase                     | input_schema           |
#   | 6 | CallToolResult.isError    | camelCase                     | is_error               |
#
#   1 is an ImportError at startup. 2 is a TypeError on the first call — a fix for 1 alone
#   produces a clean import and a failure further from the cause. 3 means this module must
#   never `import httpx`: on 2.x that raises ModuleNotFoundError, because the SDK depends on
#   httpx2. 4 raises "not enough values to unpack" from inside a task group, surfacing as a
#   nested ExceptionGroup that reads like a transport fault.
#
#   5 AND 6 ARE SILENT, and 5 is the dangerous one. A client that catches broadly so an
#   unreachable peer degrades rather than breaks will swallow the AttributeError and report
#   an EMPTY TOOL INVENTORY for every server it can reach perfectly well.
#
# 1.28.1 SHIPS BOTH CLIENT NAMES AS DIFFERENT FUNCTIONS. `streamable_http_client` on 1.x is
# not an alias — it already carries the 2.x signature. The legacy name is preferred where
# present so a 1.x deployment keeps the code path the matrix actually exercised there.

import importlib
from contextlib import asynccontextmanager

_client_mod = importlib.import_module("mcp.client.streamable_http")
_legacy_client = getattr(_client_mod, "streamablehttp_client", None)
_modern_client = getattr(_client_mod, "streamable_http_client", None)

# PREFER THE MODERN ENTRY POINT. 1.28.1 -- the floor this extra declares -- already ships
# `streamable_http_client` with the 2.x shape, and deprecates the legacy name with a warning
# pointing here. Preferring it means ONE code path on both majors rather than two, no
# DeprecationWarning on every connection, and the branch that has a future is the branch both
# matrix legs exercise. The legacy name stays as a fallback only for an install that predates
# the rename; if the floor ever rises past it, that branch can simply go.
USES_LEGACY_CLIENT = _modern_client is None
CLIENT_SYMBOL = "streamablehttp_client" if USES_LEGACY_CLIENT else "streamable_http_client"

if _legacy_client is None and _modern_client is None:  # pragma: no cover - defensive
    raise ImportError(
        "mcp.client.streamable_http exposes neither streamablehttp_client nor "
        "streamable_http_client. The client entry point has moved again; extend _sdk "
        "rather than importing it directly at a call site."
    )


def _sdk_httpx():
    """The httpx the SDK itself uses — httpx on 1.x, httpx2 on 2.x.

    Read off the SDK's own module rather than imported by name, because a bare
    `import httpx` is a ModuleNotFoundError inside a 2.x install.
    """
    utils = importlib.import_module("mcp.shared._httpx_utils")
    for attr in ("httpx2", "httpx"):
        lib = getattr(utils, attr, None)
        if lib is not None:
            return lib
    raise ImportError("could not locate the httpx module the MCP SDK is using")


def _as_triple(streams):
    """Normalise yielded streams to `(read, write, get_session_id)`.

    On 2.x the session-id getter is gone, so the third element becomes a callable returning
    None. That keeps `get_session_id()` safe to call rather than turning an API change into
    an AttributeError at every call site.
    """
    if len(streams) >= 3:
        return streams[0], streams[1], streams[2]
    return streams[0], streams[1], (lambda: None)


@asynccontextmanager
async def open_client_streams(url: str, *, timeout: float = 30.0,
                              sse_read_timeout: float = 300.0,
                              headers: dict | None = None):
    """Open a streamable-HTTP client connection, yielding `(read, write, get_session_id)`.

    One signature regardless of which major is installed.

    `sse_read_timeout` is separate from `timeout` and generous by default: the response
    stream is legitimately long-lived, and a read timeout tighter than the slowest tool call
    severs healthy connections mid-answer.

    `headers` matters more than it looks. A server started with `--public-hostname` runs the
    SDK's DNS-rebinding guard, whose allow-list is localhost plus that one name — so reaching
    it by container name sends a Host the server rejects, and the client sees JSON-RPC -32603
    with "Invalid Host header" only in the SERVER's log. Pass the public hostname as `Host`
    when connecting to a container directly.
    """
    if USES_LEGACY_CLIENT:
        async with _legacy_client(url, headers=headers, timeout=timeout,
                                  sse_read_timeout=sse_read_timeout) as streams:
            yield _as_triple(streams)
        return

    httpx = _sdk_httpx()
    # First positional is the default for every phase; `read` is widened for the SSE stream.
    client = _client_mod.create_mcp_http_client(
        headers=headers, timeout=httpx.Timeout(timeout, read=sse_read_timeout))
    async with client:
        async with _modern_client(url, http_client=client) as streams:
            yield _as_triple(streams)


def tool_input_schema(tool) -> dict:
    """A tool's JSON Schema from a `list_tools` result, whatever this major calls the field.

    Break 5. Silent on 2.x: clients that catch broadly turn the AttributeError into an empty
    inventory and report the server as having no tools.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema or {}


def result_is_error(result) -> bool:
    """The MCP-level error flag on a `call_tool` result. Break 6, also silent.

    Note this is the PROTOCOL's flag. A server may additionally report failure inside its
    own response body — the corpus servers do — and reading this alone is not a complete
    error check for such a server.
    """
    for attr in ("is_error", "isError"):
        value = getattr(result, attr, None)
        if value is not None:
            return bool(value)
    return False


def report(stream=sys.stderr) -> None:
    print(f"[corpus-mcp] mcp SDK {sdk_version()} (major {SDK_MAJOR}, "
          f"client entry point {CLIENT_SYMBOL})", file=stream)
