"""The opt-in curl transport of `Fetcher` (ADR-0018) and the public ranged get.

No network and no real curl run: the curl subprocess is a fake `runner` that records its argv
and writes the two files (`-o` body, `-D` headers) and the `-w` stdout a real curl would, so
the tests assert what the Fetcher SENDS and how it classifies what comes back.
"""
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from corpus_toolkit.sources import fetch, tls
from corpus_toolkit.sources.fetch import (Challenge, CurlUnavailable, Fetched, FetchError,
                                          Fetcher, Refused)

CHALLENGE = httpx.Response(403, headers={"cf-mitigated": "challenge"})


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.events: list = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.events.append(("sleep", s))
        self.t += s


class FakeCurl:
    """Stands in for the curl binary. `script` is a list of replies, consumed in order; the
    last one repeats. A reply is (status, headers, body, final_url) or an int exit code."""

    def __init__(self, *replies, clock=None):
        self.replies = list(replies)
        self.calls: list[list[str]] = []
        self.clock = clock

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.clock:
            self.clock.events.append(("curl",))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, int):
            return subprocess.CompletedProcess(argv, reply, b"", b"curl: (%d) boom" % reply)
        status, headers, body, final = reply
        head = f"HTTP/2 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        open(argv[argv.index("-o") + 1], "wb").write(body)
        open(argv[argv.index("-D") + 1], "wb").write(
            b"HTTP/1.1 301 Moved\r\nLocation: /x\r\n\r\n" + head.encode() + b"\r\n")
        return subprocess.CompletedProcess(argv, 0, final.encode(), b"")

    def header_values(self, call, name):
        return [call[i + 1] for i, a in enumerate(call) if a in ("-H", "--header")
                and call[i + 1].lower().startswith(name.lower() + ":")]


@pytest.fixture(autouse=True)
def curl_present(monkeypatch):
    monkeypatch.setattr(fetch.shutil, "which", lambda name: "/usr/bin/curl")


def make(handler, curl, **kw):
    clock = Clock()
    curl.clock = clock
    kw.setdefault("curl_on_challenge", True)
    f = Fetcher(transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock.now,
                log=lambda m: None, runner=curl, **kw)
    return f, clock


def challenged(req):
    return httpx.Response(403, headers={"cf-mitigated": "challenge"})


def ok(body=b"<html>ok</html>", url="https://h.example.gov/p", ctype="text/html; charset=UTF-8",
       status=200, **extra):
    return (status, {"Content-Type": ctype, **extra}, body, url)


def test_off_by_default_a_challenge_is_still_a_challenge_and_curl_never_runs():
    curl = FakeCurl(ok())
    clock = Clock()
    f = Fetcher(transport=httpx.MockTransport(challenged), sleep=clock.sleep,
                clock=clock.now, log=lambda m: None, runner=curl)
    with pytest.raises(Challenge):
        f.get("https://h.example.gov/p")
    assert curl.calls == []


def test_a_challenged_host_is_retried_with_curl_and_the_same_honest_user_agent():
    curl = FakeCurl(ok(b"%PDF-1.7 \x00\xff bin", "https://h.example.gov/final.pdf",
                       "application/pdf"))
    f, _ = make(challenged, curl)
    got = f.get("https://h.example.gov/a")
    assert got == Fetched(b"%PDF-1.7 \x00\xff bin", "application/pdf", 200,
                          "https://h.example.gov/final.pdf")
    [call] = curl.calls
    assert call[0] == "/usr/bin/curl" and call[1] == "-q"          # ~/.curlrc cannot inject
    assert call[call.index("-A") + 1] == f.user_agent
    assert f.user_agent.startswith("OregonAI-CivicCorpus/")
    assert curl.header_values(call, "User-Agent") == []            # one identity, one place
    assert call[-1] == "https://h.example.gov/a"


def test_a_custom_user_agent_is_what_curl_sends_too():
    curl = FakeCurl(ok())
    f, _ = make(challenged, curl, user_agent="Corpus/9 (+https://example.org)")
    f.get("https://h.example.gov/a")
    assert curl.calls[0][curl.calls[0].index("-A") + 1] == "Corpus/9 (+https://example.org)"


def test_no_browser_headers_no_cookies_no_impersonation_flags():
    curl = FakeCurl(ok())
    f, _ = make(challenged, curl)
    f.get("https://h.example.gov/a")
    flat = " ".join(curl.calls[0]).lower()
    for forbidden in ("mozilla", "chrome", "sec-ch", "sec-fetch", "--cookie", " -b ",
                      "cookie:", "impersonate", "--insecure", " -k ", "referer"):
        assert forbidden not in flat, forbidden
    assert "--proto" in flat                                       # http(s) only


def test_the_host_is_remembered_so_later_requests_skip_the_doomed_python_attempt():
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return challenged(req)
    curl = FakeCurl(ok())
    f, _ = make(handler, curl)
    f.get("https://h.example.gov/a")
    f.get("https://h.example.gov/b")
    assert seen == ["h.example.gov"]                               # httpx tried once
    assert len(curl.calls) == 2
    f.get("https://other.example.gov/")                            # other hosts: python first
    assert seen == ["h.example.gov", "other.example.gov"]


def test_only_a_challenge_triggers_curl_a_plain_403_is_still_refused():
    curl = FakeCurl(ok())
    f, _ = make(lambda req: httpx.Response(403), curl)
    with pytest.raises(Refused) as e:
        f.get("https://h.example.gov/a")
    assert not isinstance(e.value, Challenge)
    assert curl.calls == []


def test_a_307_without_location_also_counts_as_a_challenge_and_goes_to_curl():
    curl = FakeCurl(ok())
    f, _ = make(lambda req: httpx.Response(307), curl)
    assert f.get("https://h.example.gov/a").status == 200
    assert len(curl.calls) == 1


def test_a_host_that_challenges_curl_too_is_still_a_challenge():
    curl = FakeCurl((403, {"cf-mitigated": "challenge"}, b"", "https://h.example.gov/a"))
    f, _ = make(challenged, curl)
    with pytest.raises(Challenge, match="Cloudflare managed challenge"):
        f.get("https://h.example.gov/a")
    assert len(curl.calls) == 1                                    # no loop


def test_curl_answering_403_or_307_without_location_is_classified_like_httpx():
    f, _ = make(challenged, FakeCurl((403, {}, b"", "u")))
    with pytest.raises(Refused) as e:
        f.get("https://h.example.gov/a")
    assert not isinstance(e.value, Challenge)
    f, _ = make(challenged, FakeCurl((307, {}, b"", "u")))
    with pytest.raises(Challenge, match="307"):
        f.get("https://h.example.gov/a")


def test_a_curl_404_raises_httpx_status_error_like_the_python_path():
    f, _ = make(challenged, FakeCurl((404, {}, b"nope", "https://h.example.gov/a")))
    with pytest.raises(httpx.HTTPStatusError) as e:
        f.get("https://h.example.gov/a")
    assert e.value.response.status_code == 404


def test_the_per_host_interval_separates_the_python_attempt_from_the_curl_one():
    curl = FakeCurl(ok())
    f, clock = make(challenged, curl, min_interval=2.0)
    f.get("https://h.example.gov/a")
    assert ("sleep", 2.0) in clock.events
    assert clock.events.index(("sleep", 2.0)) < clock.events.index(("curl",))
    f.get("https://h.example.gov/b")                               # and curl-to-curl
    assert clock.events.count(("sleep", 2.0)) == 2


def test_429_over_curl_honours_retry_after_then_backoff_then_refuses():
    curl = FakeCurl((429, {"Retry-After": "7"}, b"", "u"), (429, {}, b"", "u"))
    f, clock = make(challenged, curl, backoff=(3.0, 9.0), min_interval=0)
    with pytest.raises(Refused, match="429"):
        f.get("https://h.example.gov/a")
    sleeps = [e[1] for e in clock.events if e[0] == "sleep" and e[1] > 0]
    assert sleeps == [7.0, 9.0]
    assert len(curl.calls) == 3                                    # 1 + len(backoff)


def test_curl_missing_fails_loudly_at_construction_never_a_silent_fallback(monkeypatch):
    monkeypatch.setattr(fetch.shutil, "which", lambda name: None)
    with pytest.raises(CurlUnavailable, match="curl"):
        Fetcher(curl_on_challenge=True)
    assert issubclass(CurlUnavailable, FetchError)
    Fetcher()                                                      # opted out: no check


def test_curl_exit_codes_map_to_the_errors_the_python_path_raises():
    cases = [(28, httpx.TimeoutException), (6, httpx.ConnectError),
             (7, httpx.ConnectError), (52, Refused), (56, Refused), (92, Refused),
             (60, Refused)]
    for code, exc in cases:
        f, _ = make(challenged, FakeCurl(code))
        with pytest.raises(exc):
            f.get("https://h.example.gov/a")
    f, _ = make(challenged, FakeCurl(60))
    with pytest.raises(Refused, match=r"_meta/tls-chain/h\.example\.gov\.pem"):
        f.get("https://h.example.gov/a")
    f, _ = make(challenged, FakeCurl(2))
    with pytest.raises(FetchError, match="exit 2"):
        f.get("https://h.example.gov/a")


def test_timeout_is_passed_to_curl():
    curl = FakeCurl(ok())
    f, _ = make(challenged, curl, timeout=33.0)
    f.get("https://h.example.gov/a")
    assert curl.calls[0][curl.calls[0].index("--max-time") + 1] == "33"


def test_a_supplement_is_passed_to_curl_as_system_roots_plus_the_supplement_for_its_host(
        tmp_path, monkeypatch):
    fixtures = __import__("pathlib").Path(__file__).parent / "fixtures" / "tls"
    chain = tmp_path / "_meta" / "tls-chain"
    chain.mkdir(parents=True)
    supp = (fixtures / "example-intermediate.pem").read_bytes()
    (chain / "h.example.gov.pem").write_bytes(supp)
    system = tmp_path / "system.pem"
    system.write_bytes((fixtures / "example-root.pem").read_bytes())
    monkeypatch.setattr(tls.ssl, "get_default_verify_paths",
                        lambda: SimpleNamespace(cafile=str(system), openssl_cafile=""))
    cfg = SimpleNamespace(id="x", config_path=tmp_path / "_meta" / "corpus.yml")
    curl = FakeCurl(ok())
    seen_bundle = []
    inner = curl.__call__

    def spy(argv, timeout):
        if "--cacert" in argv:
            seen_bundle.append(open(argv[argv.index("--cacert") + 1], "rb").read())
        return inner(argv, timeout)
    f, _ = make(challenged, spy, config=cfg)
    # A mounted host's Python attempt would use the real supplemented transport (no network
    # in unit tests), so start it as already-challenged; the host is then fetched by curl.
    f._curl_hosts.add("h.example.gov")
    f.get("https://h.example.gov/a")
    f.get("https://other.example.gov/a")
    assert len(seen_bundle) == 1                                   # only the covered host
    assert supp.strip() in seen_bundle[0]
    assert system.read_bytes().strip() in seen_bundle[0]
    f.close()


def test_get_response_exposes_headers_on_both_transports():
    f, _ = make(lambda req: httpx.Response(200, content=b"p", headers={"X-A": "1"}),
                FakeCurl(ok(X_B="2")))
    assert f.get_response("https://h.example.gov/a").headers["x-a"] == "1"
    g, _ = make(challenged, FakeCurl(ok(**{"Content-Range": "bytes 0-0/9"})))
    r = g.get_response("https://h.example.gov/a")
    assert r.headers["content-range"] == "bytes 0-0/9" and r.status_code == 200


def test_get_range_sends_a_range_header_over_python():
    seen = []

    def handler(req):
        seen.append(req.headers["Range"])
        assert req.headers["User-Agent"].startswith("OregonAI-CivicCorpus/")
        return httpx.Response(206, content=b"abc", headers={"Content-Type": "application/pdf"})
    f, _ = make(handler, FakeCurl(ok()), curl_on_challenge=False)
    got = f.get_range("https://h.example.gov/big.pdf", 10, 12)
    assert got == Fetched(b"abc", "application/pdf", 206, "https://h.example.gov/big.pdf")
    f.get_range("https://h.example.gov/big.pdf", 100)
    f.get_range("https://h.example.gov/big.pdf", -50)
    assert seen == ["bytes=10-12", "bytes=100-", "bytes=-50"]


def test_get_range_over_curl_keeps_every_guarantee():
    curl = FakeCurl(ok(b"abc", "https://h.example.gov/big.pdf", "application/pdf", status=206))
    f, clock = make(challenged, curl, min_interval=2.0)
    got = f.get_range("https://h.example.gov/big.pdf", 0, 2)
    assert got.status == 206 and got.body == b"abc"
    assert curl.header_values(curl.calls[0], "Range") == ["Range: bytes=0-2"]
    assert ("sleep", 2.0) in clock.events


def test_get_range_416_is_a_status_error_and_a_bad_range_is_refused_up_front():
    f, _ = make(lambda req: httpx.Response(416), FakeCurl(ok()))
    with pytest.raises(httpx.HTTPStatusError):
        f.get_range("https://h.example.gov/a", 5, 6)
    for args in ((5, 2), (0, -1)):
        with pytest.raises(ValueError):
            f.get_range("https://h.example.gov/a", *args)


def test_a_bundle_is_never_the_supplement_alone(tmp_path, monkeypatch):
    fixtures = __import__("pathlib").Path(__file__).parent / "fixtures" / "tls"
    (tmp_path / "tls-chain").mkdir()
    (tmp_path / "tls-chain" / "h.example.gov.pem").write_bytes(
        (fixtures / "example-intermediate.pem").read_bytes())
    monkeypatch.setattr(tls.ssl, "get_default_verify_paths",
                        lambda: SimpleNamespace(cafile=None, openssl_cafile="/nonexistent"))
    with pytest.raises(tls.SupplementRefused, match="supplement alone"):
        tls.ca_bundle(tmp_path, "h.example.gov", tmp_path)
    assert tls.ca_bundle(tmp_path, "other.example.gov", tmp_path) is None


# -- the real curl binary against a loopback server (no external network) -------------------

import http.server
import shutil as _shutil
import threading


class _Origin(http.server.BaseHTTPRequestHandler):
    """Challenges any client that sends Accept-Encoding (httpx does, bare curl does not), so
    the Python attempt is refused and the real curl is let through."""
    seen: list = []

    def do_GET(self):
        _Origin.seen.append((self.path, dict(self.headers)))
        if self.headers.get("Accept-Encoding"):
            self.send_response(403)
            self.send_header("cf-mitigated", "challenge")
            self.end_headers()
        elif self.path == "/old":
            self.send_response(301)
            self.send_header("Location", "/new.pdf")
            self.end_headers()
        else:
            body = b"%PDF-1.4\x00\xff\xfe" + bytes(range(256))
            rng = self.headers.get("Range")
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "application/pdf")
            if rng:
                body = body[2:6]
                self.send_header("Content-Range", "bytes 2-5/270")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.mark.skipif(_shutil.which("curl") is None, reason="no curl binary")
def test_the_real_curl_binary_follows_redirects_keeps_binary_and_sends_only_our_agent():
    _Origin.seen.clear()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with Fetcher(curl_on_challenge=True, min_interval=0, log=lambda m: None) as f:
            got = f.get(base + "/old")
            assert got.body == b"%PDF-1.4\x00\xff\xfe" + bytes(range(256))
            assert got.url == base + "/new.pdf" and got.content_type == "application/pdf"
            part = f.get_range(base + "/new.pdf", 2, 5)
            assert part.status == 206 and part.body == b"DF-1"
            assert f.get_response(base + "/new.pdf", (2, 5)).headers["content-range"] \
                == "bytes 2-5/270"
    finally:
        srv.shutdown()
    curl_reqs = [h for _, h in _Origin.seen if not h.get("Accept-Encoding")]
    assert curl_reqs and all(h["User-Agent"] == f.user_agent for h in curl_reqs)
    assert not any("Cookie" in h or "Referer" in h for h in curl_reqs)
