"""The change detector speaks HTTP/2, and cannot quietly stop (corpus-toolkit#162).

A growing number of government hosts refuse HTTP/1.1 outright. With the SAME User-Agent:

    curl --http2    -> 200
    curl --http1.1  -> 403
    python urllib   -> 403

54 oregon-counties sources could never be seeded because of it, while that repo's own
ingest reached the same hosts fine -- the mirror held the documents and could not watch
them for change.

The dependency is what makes this work: `httpx[http2]`, whose extra pulls in `h2`.
`httpx` refuses to build an http2 client without it, and the test below asserts that
refusal -- because if httpx ever started DOWNGRADING silently instead, the detector would
believe it was on HTTP/2, collect 403s from the hosts above, and report them as fetch
failures forever. That is the same wrong diagnosis one layer down, and it would be
invisible. The property is depended on, so it is checked rather than assumed.
"""
import sys
from unittest import mock

import pytest

from corpus_toolkit.sources import changes


@pytest.fixture(autouse=True)
def _reset_client():
    """The client is a module-level singleton; each test builds its own."""
    changes._client = None
    yield
    changes._client = None


def test_the_client_asks_for_http2():
    with mock.patch.object(changes, "_client", None), \
         mock.patch("httpx.Client") as Client:
        changes._http_client()
    assert Client.call_args.kwargs["http2"] is True, (
        "the client must ask for HTTP/2 -- without it the hosts in #162 answer 403"
    )


def test_the_user_agent_stays_honest():
    """Speaking HTTP/2 is not impersonation. If this ever grows a browser User-Agent,
    the remedy has drifted from 'be a current client' to 'pretend to be someone else'."""
    with mock.patch.object(changes, "_client", None), \
         mock.patch("httpx.Client") as Client:
        changes._http_client()
    ua = Client.call_args.kwargs["headers"]["User-Agent"]
    assert ua == "corpus-toolkit-change-detector"
    assert "Mozilla" not in ua and "Chrome" not in ua


def test_a_missing_h2_is_refused_rather_than_silently_downgraded():
    """THE PROPERTY THIS FIX RESTS ON, checked rather than assumed.

    A silent downgrade would be indistinguishable from the bug being fixed: the detector
    would report HTTP/2 and collect 403s. This asserts httpx refuses instead, so the day
    that stops being true is the day a test fails rather than the day a corpus quietly
    stops being watched.
    """
    real_import = __import__

    def no_h2(name, *a, **kw):
        if name == "h2" or name.startswith("h2."):
            raise ImportError("no h2")
        return real_import(name, *a, **kw)

    with mock.patch.object(changes, "_client", None), \
         mock.patch("builtins.__import__", side_effect=no_h2):
        with pytest.raises(ImportError, match="h2"):
            changes._http_client()


@pytest.mark.skipif(not __import__("os").environ.get("LIVE_HTTP2_CHECK"),
                    reason="network; set LIVE_HTTP2_CHECK=1 to run")
def test_a_host_that_refuses_http1_is_reachable():
    """The end-to-end fact, against the host from #162. Opt-in because it needs network."""
    import httpx
    c = httpx.Client(http2=True, follow_redirects=True, timeout=30,
                     headers={"User-Agent": changes.USER_AGENT})
    r = c.get("https://lake.county.codes/LCC/1")
    assert r.status_code == 200
    assert r.http_version == "HTTP/2"
