"""`corpus_toolkit.sources.fetch.Fetcher` -- the platform's one ingest fetcher (ADR-0016).

Every case here is a host behaviour oregon-counties met for real. No network: httpx's
MockTransport stands in for the hosts, and `sleep`/`clock` are injected so backoff and the
per-host interval are asserted rather than waited for.
"""
import re

import httpx
import pytest

from corpus_toolkit.sources import fetch
from corpus_toolkit.sources.fetch import Challenge, Fetched, Fetcher, Refused, sniff


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def fetcher(handler, **kw):
    clock = Clock()
    f = Fetcher(transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock.now,
                log=lambda m: None, **kw)
    return f, clock


def test_the_default_user_agent_is_honest_versioned_and_linked():
    ua = fetch.default_user_agent()
    assert re.fullmatch(r"OregonAI-CivicCorpus/\S+ \(\+https://github\.com/OregonAI/corpus-toolkit; "
                        r"public-records archival\)", ua), ua
    assert "Mozilla" not in ua

    class Cfg:
        id = "oregon-counties"
    assert "OregonAI/oregon-counties;" in fetch.default_user_agent(Cfg())


def test_get_returns_body_media_type_status_and_final_url():
    def handler(req):
        assert req.headers["User-Agent"].startswith("OregonAI-CivicCorpus/")
        return httpx.Response(200, content=b"%PDF-1.4 x",
                              headers={"Content-Type": "application/pdf; charset=binary"})
    f, _ = fetcher(handler)
    got = f.get("https://example.gov/a.pdf")
    assert got == Fetched(b"%PDF-1.4 x", "application/pdf", 200, "https://example.gov/a.pdf")


def test_429_waits_retry_after_then_backoff_and_finally_refuses():
    seen = []

    def handler(req):
        seen.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"} if len(seen) == 1 else {})
    f, clock = fetcher(handler, min_interval=0, backoff=(5, 15))
    with pytest.raises(Refused, match="HTTP 429"):
        f.get("https://slow.example.gov/x")
    assert len(seen) == 3                       # first try + one per backoff step
    assert clock.slept == [7.0, 15.0]           # Retry-After honoured, then the schedule


def test_429_that_clears_returns_the_body():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429) if n["i"] == 1 else httpx.Response(200, content=b"ok")
    f, clock = fetcher(handler, min_interval=0, backoff=(5,))
    assert f.get("https://example.gov/y").body == b"ok"
    assert clock.slept == [5.0]


@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_are_refusals_not_404s(status):
    f, _ = fetcher(lambda req: httpx.Response(status), min_interval=0)
    with pytest.raises(Refused) as ei:
        f.get("https://example.gov/z")
    assert not isinstance(ei.value, Challenge)


def test_cloudflare_managed_challenge_is_a_challenge():
    f, _ = fetcher(lambda req: httpx.Response(403, headers={"cf-mitigated": "challenge"}),
                   min_interval=0)
    with pytest.raises(Challenge, match="Cloudflare managed challenge"):
        f.get("https://cf.example.gov/")


def test_sucuri_307_without_location_is_a_challenge():
    f, _ = fetcher(lambda req: httpx.Response(307, content=b"<script>cookie</script>"),
                   min_interval=0)
    with pytest.raises(Challenge, match="307 with no Location"):
        f.get("https://sucuri.example.gov/")


def test_an_incomplete_tls_chain_is_named_with_its_remedy():
    def handler(req):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
    f, _ = fetcher(handler, min_interval=0)
    with pytest.raises(Refused, match=r"_meta/tls-chain/baker\.example\.gov\.pem"):
        f.get("https://baker.example.gov/code")


def test_a_reset_before_any_response_is_a_refusal():
    def handler(req):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
    f, _ = fetcher(handler, min_interval=0)
    with pytest.raises(Refused, match="closed without a response"):
        f.get("https://lb2.example.gov/")


def test_other_http_errors_surface_as_httpx_status_errors():
    f, _ = fetcher(lambda req: httpx.Response(404), min_interval=0)
    with pytest.raises(httpx.HTTPStatusError):
        f.get("https://example.gov/missing")


def test_requests_to_one_host_are_spaced_by_min_interval_and_other_hosts_are_not():
    f, clock = fetcher(lambda req: httpx.Response(200, content=b"x"), min_interval=2.0)
    f.get("https://a.example.gov/1")
    f.get("https://b.example.gov/1")          # different host: no wait
    f.get("https://a.example.gov/2")          # same host, 0s later: wait the full interval
    assert clock.slept == [2.0]
    clock.t += 5
    f.get("https://a.example.gov/3")          # long enough ago: no wait
    assert clock.slept == [2.0]


def test_snapshot_caches_on_disk_and_reports_fresh_only_once(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, content=b"body")
    f, _ = fetcher(handler, min_interval=0)
    dest = tmp_path / "snap" / "s.html"
    assert f.snapshot("https://example.gov/s", dest) == (b"body", True)
    assert f.snapshot("https://example.gov/s", dest) == (b"body", False)
    assert len(calls) == 1
    assert f.snapshot("https://example.gov/s", dest, refetch=True) == (b"body", True)
    assert len(calls) == 2


def test_enforce_robots_refuses_only_an_explicit_disallow(monkeypatch):
    verdict = {"v": False}
    monkeypatch.setattr(fetch.robots, "allowed", lambda url, ua: verdict["v"])
    f, _ = fetcher(lambda req: httpx.Response(200, content=b"ok"), min_interval=0,
                   enforce_robots=True)
    with pytest.raises(Refused, match="robots.txt disallows"):
        f.get("https://example.gov/")
    verdict["v"] = None                       # unreachable robots.txt: not permission, not refusal
    assert f.get("https://example.gov/").body == b"ok"
    verdict["v"] = True
    assert f.get("https://example.gov/").body == b"ok"


def test_robots_is_reported_not_enforced_by_default(monkeypatch):
    monkeypatch.setattr(fetch.robots, "allowed", lambda url, ua: False)
    f, _ = fetcher(lambda req: httpx.Response(200, content=b"ok"), min_interval=0)
    assert f.get("https://example.gov/").body == b"ok"
    assert f.allowed("https://example.gov/") is False


@pytest.mark.parametrize("body, declared, want", [
    (b"%PDF-1.7\n...", "html", "pdf"),
    (b"\n  <!DOCTYPE html><html>", "pdf", "html"),
    (b'{"a": 1}', None, "json"),
    (b"<?xml version='1.0'?><r/>", None, "xml"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "pdf", "doc"),
    (b"PK\x03\x04" + b"\0" * 30 + b"word/document.xml", None, "docx"),
    (b"plain text nothing else", "pdf", "pdf"),      # declared is the LAST resort
    (b"plain text nothing else", None, "html"),
])
def test_sniff_trusts_magic_bytes_before_anyone_s_say_so(body, declared, want):
    assert sniff(body, declared) == want
    assert fetch.UNSUPPORTED >= {"doc", "docx"}
