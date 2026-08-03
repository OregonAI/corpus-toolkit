"""The CLIENT half of the SDK compat seam, proved by an actual round trip.

Everything here starts a real streamable-HTTP server built by `_sdk`, connects to it with
`_sdk.open_client_streams`, and asserts on what comes back. That is deliberate and it is the
whole point of the file: the six client-side breaks between mcp 1.28.1 and 2.0.0 were found
one at a time across four failed deploys of corpus-gateway, and **only the first of them is
visible without making a real call**.

    1 name         ImportError at import
    2 signature    TypeError on the first call
    3 http library ModuleNotFoundError (2.x uses httpx2, not httpx)
    4 arity        ValueError inside a task group -> nested ExceptionGroup
    5 inputSchema  SILENT — swallowed by broad catches, yields an empty tool inventory
    6 isError      SILENT — error results score as successes

A test that asserted on `inspect.signature` or on which symbol was selected would pass while
5 and 6 were broken, which is exactly the trap `test_mount_path.py` records for the server
side: the old tests asserted a SETTING existed, so on 2.x they could not even be collected.
Assert behaviour, run unchanged on both majors.

Verified against mcp 1.28.1 and 2.0.0.
"""
import asyncio
import socket
import threading
import time

import pytest

from corpus_toolkit.mcp import _sdk

pytest.importorskip("uvicorn")
import uvicorn  # noqa: E402

from mcp import ClientSession  # noqa: E402

HOST = "127.0.0.1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _build_server():
    server = _sdk.Server("client-seam-test")

    @server.tool()
    def echo(text: str, times: int = 1) -> dict:
        """Echo `text` back `times` times."""
        return {"echoed": text * times}

    @server.tool()
    def explode() -> dict:
        """Always fails, so the protocol error flag has something to report."""
        raise RuntimeError("deliberate failure")

    return server


class _Serving:
    """Run the streamable-HTTP app on a real socket for the duration of a test."""

    def __init__(self, path="/mcp"):
        self.path = path
        self.port = _free_port()
        self.server = _build_server()
        kwargs = _sdk.http_kwargs(host=HOST, port=self.port, path=path)
        app = _sdk.build_http_app(self.server, kwargs)
        config = uvicorn.Config(app, host=HOST, port=self.port, log_level="error")
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}{self.path}"

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if getattr(self._uvicorn, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("test server did not start")

    def __exit__(self, *exc):
        self._uvicorn.should_exit = True
        self._thread.join(timeout=15)


def _run(coro):
    return asyncio.run(coro)


def test_the_client_entry_point_is_one_we_have_actually_tested():
    """A third arrangement would otherwise land silently — `open_client_streams` would still
    import if a future SDK kept either name, and every behavioural test below could pass
    while something untested was in play."""
    assert _sdk.CLIENT_SYMBOL in (
        "streamablehttp_client",
        "streamable_http_client",
    ), _sdk.CLIENT_SYMBOL


def test_modern_entry_point_is_preferred_where_available():
    """mcp 1.28.1 ships both names as DIFFERENT functions — the modern one already carries
    the 2.x shape, and the legacy one emits a DeprecationWarning pointing at it. Preferring
    modern keeps ONE code path across both majors instead of two, and means both matrix legs
    exercise the branch that has a future."""
    import mcp.client.streamable_http as mod

    if hasattr(mod, "streamable_http_client"):
        assert _sdk.CLIENT_SYMBOL == "streamable_http_client"
    else:
        assert _sdk.CLIENT_SYMBOL == "streamablehttp_client"


def test_no_deprecation_warning_on_connect():
    """A shim that produces a DeprecationWarning on every connection has picked the wrong
    branch. This is the check that keeps the preference from silently regressing."""
    import warnings

    async def go():
        with _Serving() as serving:
            async with _sdk.open_client_streams(serving.url, timeout=15) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(go())
    deprecations = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "streamable" in str(w.message).lower()
    ]
    assert not deprecations, [str(w.message) for w in deprecations]


def test_round_trip_call_returns_the_tools_answer():
    """Breaks 1-4 at once: import, signature, http library, and yielded arity. Any of them
    fails here, and none of them can fail without a real connection being attempted."""

    async def go():
        with _Serving() as serving:
            async with _sdk.open_client_streams(serving.url, timeout=15) as (r, w, get_id):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool("echo", {"text": "ab", "times": 3})
                    text = "".join(
                        getattr(b, "text", "") for b in (result.content or [])
                    )
                    assert "ababab" in text, text
                    # Break 4: the third element must always be callable, even on 2.x
                    # where the SDK stopped yielding it.
                    assert callable(get_id)
                    get_id()

    _run(go())


def test_tool_input_schema_survives_the_field_rename():
    """Break 5, the silent one. Asserted through a real `list_tools`, because the failure is
    an AttributeError that broad exception handling converts into an empty inventory."""

    async def go():
        with _Serving() as serving:
            async with _sdk.open_client_streams(serving.url, timeout=15) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    by_name = {t.name: t for t in listing.tools}
                    assert {"echo", "explode"} <= set(by_name)

                    schema = _sdk.tool_input_schema(by_name["echo"])
                    assert schema, "empty schema — the field rename was not bridged"
                    assert "text" in schema.get("properties", {}), schema
                    assert schema.get("required") == ["text"], schema

                    # A no-argument tool still yields a usable object rather than None.
                    assert isinstance(_sdk.tool_input_schema(by_name["explode"]), dict)

    _run(go())


def test_result_is_error_survives_the_field_rename():
    """Break 6, also silent: a failing tool must read as failed on both majors."""

    async def go():
        with _Serving() as serving:
            async with _sdk.open_client_streams(serving.url, timeout=15) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()

                    good = await session.call_tool("echo", {"text": "x"})
                    assert _sdk.result_is_error(good) is False

                    bad = await session.call_tool("explode", {})
                    assert _sdk.result_is_error(bad) is True, (
                        "a raising tool did not report the protocol error flag — the "
                        "isError/is_error rename was not bridged"
                    )

    _run(go())


def test_custom_headers_reach_the_server():
    """`headers` is not decoration: it is how a client satisfies a server's DNS-rebinding
    guard when connecting by container name instead of through the proxy.

    Asserted via the guard itself — a server whose allow-list names only `example.test`
    accepts a request carrying that Host and rejects one carrying the socket's own.
    """

    async def go():
        security = _sdk.TransportSecuritySettings(
            allowed_hosts=["example.test"],
            allowed_origins=["https://example.test"],
        )
        serving = _Serving()
        serving.server = _build_server()
        kwargs = _sdk.http_kwargs(
            host=HOST, port=serving.port, path="/mcp", transport_security=security
        )
        app = _sdk.build_http_app(serving.server, kwargs)
        config = uvicorn.Config(app, host=HOST, port=serving.port, log_level="error")
        serving._uvicorn = uvicorn.Server(config)
        serving._thread = threading.Thread(target=serving._uvicorn.run, daemon=True)

        with serving:
            # Without the Host header the guard rejects the connection.
            with pytest.raises(BaseException):
                async with _sdk.open_client_streams(serving.url, timeout=10) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()

            # With it, the same server answers.
            async with _sdk.open_client_streams(
                serving.url, timeout=15, headers={"Host": "example.test"}
            ) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool("echo", {"text": "ok"})
                    assert not _sdk.result_is_error(result)

    _run(go())
