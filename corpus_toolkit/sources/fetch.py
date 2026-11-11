"""Fetching a source honestly, once, over HTTP/2 -- for every corpus (ADR-0016).

Lifted from oregon-counties' `src/fetch.py`, the one fetcher on the platform that survived
contact with hostile hosts: 36 counties on a dozen unrelated vendor platforms. Everything
below that reads like a war story is one, and each rule is there because its absence was
measured, not imagined.

THE IDENTITY THIS SENDS IS A DECISION, NOT A DEFAULT. The default User-Agent identifies
the project and the corpus truthfully and links to the repository. It is not a browser
string. A civic mirror that pretends to be a browser undercuts the reproduction policy the
platform rests on, so:

  - A host that answers 401/403/429 to an honest agent has refused us at the HTTP layer.
    That source is UNAVAILABLE and is recorded as such; `Refused` is raised so the caller
    cannot mistake it for a 404. We do not then retry wearing a Chrome fingerprint.
  - One request per source, cached on disk forever after (`snapshot`). Re-fetching is
    opt-in, never a side effect of running an ingester again.
  - `min_interval` between requests to the same host, always.
  - robots.txt is REPORTED, not enforced, by default -- the toolkit-wide rule in AGENTS.md
    ("Fetching: the toolkit does not enforce robots.txt"): arriving as a surprise
    behaviour change in a version bump is how a corpus silently stops ingesting. A corpus
    that decides to enforce says so with `enforce_robots=True`, and the decision is then
    its own, in its own code, reviewable.

HTTP/2, AND WHY THAT MATTERED MORE THAN ANY OF THE ABOVE. Marion County's code was recorded
unavailable for four tranches on the belief that Cloudflare was refusing our identity. With
the SAME User-Agent: `curl --http2` -> 200, `curl --http1.1` -> 403. Python's urllib speaks
only HTTP/1.1, so every request was scored on protocol version and refused. Speaking HTTP/2
is not impersonation -- it is being a current client, with the honest User-Agent unchanged.
This is also why `httpx[http2]` is a toolkit dependency rather than an extra.

TLS. Some hosts serve their leaf certificate without the intermediate, so every verifying
client fails while browsers paper over it. That is the host's misconfiguration; the remedy
is a reviewed chain supplement under `_meta/tls-chain/<host>.pem` (ADR-0012), which this
fetcher loads through `corpus_toolkit.sources.tls` exactly as the drift detector does. A
verification failure with no supplement is reported as `Refused` naming that remedy.

The drift detector (`corpus_toolkit.sources.changes`) keeps its own transport on purpose:
its 8,105-source monthly sweep runs in an hour with no per-host interval, and its
User-Agent token is what robots.txt directives are matched against. This module is for
INGEST.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Callable, NamedTuple

import httpx

from corpus_toolkit.sources import robots, tls

TIMEOUT = 120.0
MIN_INTERVAL = 2.0           # seconds between requests to the SAME host
# Escalating waits after a 429. Ends rather than looping forever, so a host that genuinely
# will not serve us produces a recorded refusal instead of an ingest that never finishes.
BACKOFF = (5.0, 15.0, 45.0)

# Formats the platform has no extractor for. Reported by name rather than attempted, so the
# log says "we cannot read this" instead of raising a parser error that reads like a corrupt
# download. These are overwhelmingly application forms rather than law.
UNSUPPORTED = frozenset({"doc", "docx", "xls", "xlsx"})


class FetchError(RuntimeError):
    """A fetch that did not produce a body. Base of the refusals below; also raised for
    a 429 that outlasted every backoff step."""


class Refused(FetchError):
    """The host refused an honestly-identified request (401/403/429 exhausted, TLS chain it
    does not complete, connection reset before a response).

    Distinct from a 404 on purpose. A missing document is a fact about the publisher; a
    refusal is a fact about OUR ACCESS, and every corpus keeps `none-found` and
    `could-not-verify` apart for exactly this reason. Collapsing them is how a coverage
    number becomes a false claim.
    """


class Challenge(Refused):
    """A bot-challenge interstitial, which does not look like a refusal by status code.

    Sucuri CloudProxy answers HTTP 307 with NO Location header and a ~1.3 KB JavaScript
    cookie page. Cloudflare managed challenges answer 403 with `cf-mitigated: challenge`.
    Both are indistinguishable from a dead link unless you look, and both mean the opposite
    of one.
    """


class CurlUnavailable(FetchError):
    """`curl_on_challenge=True` was asked for and no `curl` binary is on PATH.

    Raised at CONSTRUCTION. An opt-in that silently degraded to "no curl" would turn a
    challenged host into the same `Challenge` the corpus believed it had configured around,
    discovered mid-ingest, hours in.
    """


class Fetched(NamedTuple):
    body: bytes
    content_type: str        # media type only, e.g. "application/pdf"; "" when absent
    status: int
    url: str                 # the final URL after redirects


def toolkit_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("corpus-toolkit")
    except PackageNotFoundError:
        return "0"


def default_user_agent(config=None) -> str:
    """Honest, specific, linked: the project, this toolkit's version, and the corpus repo.

    The corpus id doubles as the repository name across OregonAI, which is what makes the
    link resolvable. With no config the link points at the toolkit itself.
    """
    repo = getattr(config, "id", None) or "corpus-toolkit"
    return (f"OregonAI-CivicCorpus/{toolkit_version()} "
            f"(+https://github.com/OregonAI/{repo}; public-records archival)")


def _retry_after(headers) -> float | None:
    """Honour `Retry-After` when the host states one -- it knows better than our guess."""
    raw = (headers or {}).get("Retry-After")
    try:
        return max(1.0, min(120.0, float(raw)))
    except (TypeError, ValueError):
        return None


def sniff(body: bytes, declared: str | None = None) -> str:
    """Format from MAGIC BYTES, never from the URL suffix or the server's say-so.

    oregon-audits learned this over 242 reports: 239 of its sources are an HTML viewer with
    a base64 PDF inside, 2 are plain PDFs at a .pdf URL, and one URL shape serves neither.
    County portals are worse -- extensionless URLs that serve PDFs, .aspx that serves HTML,
    vendor endpoints that serve JSON. Trusting the suffix converts HTML-to-text over PDF
    bytes and reports the source as CHANGED on every single run, forever.
    """
    if body.startswith(b"%PDF"):
        return "pdf"
    head = body[:512].lstrip()
    if head.startswith(b"{") or head.startswith(b"["):
        return "json"
    if head[:5].lower() == b"<?xml":
        return "xml"
    # Office documents. `\xd0\xcf\x11\xe0` is the OLE compound-file header (.doc/.xls);
    # a ZIP magic with `word/` or `[Content_Types]` inside is OOXML (.docx/.xlsx). Detected
    # rather than left to the declared format because a .doc handed to the PDF extractor
    # raises `PdfStreamError: Stream has ended unexpectedly`, which reads like a corrupt
    # download rather than a file we simply cannot parse.
    if body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    if body.startswith(b"PK\x03\x04") and (b"word/" in body[:4000]
                                            or b"[Content_Types]" in body[:4000]):
        return "docx"
    if b"<html" in head.lower() or b"<!doctype html" in head.lower():
        return "html"
    # The declared format is the LAST resort, not the first.
    return declared or "html"


def _fetched(resp: httpx.Response) -> Fetched:
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    return Fetched(resp.content, ctype, resp.status_code, str(resp.url))


def _run_curl(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    # curl enforces --max-time itself; the subprocess limit is a backstop for a hung binary.
    return subprocess.run(argv, capture_output=True, timeout=timeout + 15)


def _parse_curl_headers(raw: bytes) -> tuple[int, list[tuple[str, str]]]:
    """Status and headers of the FINAL response in curl's `-D` dump, which holds one block
    per redirect hop (and any 1xx). Header names/values are latin-1, as HTTP defines them."""
    blocks = [b for b in raw.replace(b"\r\n", b"\n").split(b"\n\n") if b.strip()]
    lines = blocks[-1].decode("latin-1").split("\n")
    status = int(lines[0].split()[1])
    headers = [tuple(x.strip() for x in ln.split(":", 1)) for ln in lines[1:] if ":" in ln]
    return status, headers


def _curl_error(url: str, host: str, code: int, stderr: str) -> Exception:
    """curl's exit code in the vocabulary the Python path already raises."""
    if code == 28:
        return httpx.ReadTimeout(f"{url}: curl timed out")
    if code in (6, 7):
        return httpx.ConnectError(f"{url}: curl could not connect (exit {code}: {stderr})")
    if code == 60:
        return Refused(f"{url}: TLS chain incomplete (server omits its intermediate). "
                       f"Remedy: a reviewed chain supplement at _meta/tls-chain/{host}.pem "
                       f"(ADR-0012).")
    if code in (52, 56, 92):
        return Refused(f"{url}: connection closed without a response "
                       f"(curl exit {code}: {stderr})")
    return FetchError(f"{url}: curl failed (exit {code}: {stderr})")


class Fetcher:
    """One HTTP/2 client per corpus, with the platform's fetch discipline built in.

        f = Fetcher(config)                       # honest UA, this corpus's TLS supplements
        got = f.get(url)                          # Fetched(body, content_type, status, url)
        body, fresh = f.snapshot(url, dest)       # cached on disk; fresh=False when cached
        fmt = sniff(body, declared=src["format"])

    Everything a caller needs to know:
      * `get` raises `Refused` / `Challenge` (subclasses of `FetchError`) rather than
        returning an error body. A 4xx/5xx that is neither is raised as httpx's
        `HTTPStatusError`. It never returns a body for a non-2xx response.
      * A 429 is waited out (`Retry-After` if given, else `backoff`) and retried; after the
        last step it is `Refused`.
      * Requests to one host are at least `min_interval` seconds apart, across the
        instance's lifetime.
      * `enforce_robots=True` consults robots.txt for the configured agent before each
        request and raises `Refused` on an explicit disallow. An unreachable robots.txt is
        `None`, not permission, and is let through -- the corpus decides what unknown means.
      * `transport`, `sleep` and `clock` exist so tests can run it with no network and no
        waiting; production callers leave them alone.
    """

    def __init__(self, config=None, *, user_agent: str | None = None,
                 min_interval: float = MIN_INTERVAL, timeout: float = TIMEOUT,
                 backoff: tuple[float, ...] = BACKOFF, max_connections: int = 4,
                 enforce_robots: bool = False, curl_on_challenge: bool = False,
                 runner=None, transport: httpx.BaseTransport | None = None,
                 log: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.user_agent = user_agent or default_user_agent(config)
        self.headers = {"User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9"}
        self.min_interval = float(min_interval)
        self.timeout = float(timeout)
        self.backoff = tuple(float(b) for b in backoff)
        self.max_connections = max_connections
        self.enforce_robots = enforce_robots
        self._transport = transport
        self._log = log or (lambda msg: print(msg, file=sys.stderr))
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._client: httpx.Client | None = None
        self.curl_on_challenge = bool(curl_on_challenge)
        self._curl: str | None = None
        self._curl_hosts: set[str] = set()       # hosts that have challenged the Python client
        self._runner = runner or _run_curl
        self._scratch: tempfile.TemporaryDirectory | None = None
        if self.curl_on_challenge:
            self._curl = shutil.which("curl")
            if self._curl is None:
                raise CurlUnavailable(
                    "curl_on_challenge=True but no `curl` binary is on PATH. Install curl, "
                    "or construct the Fetcher without the opt-in and accept that a "
                    "challenged host is a Challenge (ADR-0018).")

    # -- transport ---------------------------------------------------------------------

    def _mounts(self) -> dict | None:
        """This corpus's reviewed chain supplements (ADR-0012), scoped per host, or none."""
        config_path = getattr(self.config, "config_path", None)
        if config_path is None:
            return None
        contexts = tls.load(pathlib.Path(config_path).parent)
        return tls.mounts(contexts) if contexts else None

    def client(self) -> httpx.Client:
        """One HTTP/2 client, reused. Redirects followed; TLS still verified."""
        if self._client is None:
            kwargs: dict = dict(http2=True, follow_redirects=True, timeout=self.timeout,
                                headers=self.headers,
                                limits=httpx.Limits(max_connections=self.max_connections))
            mounts = self._mounts()
            if mounts:
                kwargs["mounts"] = mounts
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- politeness --------------------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        wait = self.min_interval - (self._clock() - self._last.get(host, float("-inf")))
        if wait > 0:
            self._sleep(wait)
        self._last[host] = self._clock()

    def robots(self, url: str) -> robots.RobotsRecord:
        """What this url's host says in robots.txt, as seen by our agent. Reporting only."""
        return robots.ai_position(url, self.user_agent)

    def allowed(self, url: str) -> bool | None:
        """robots.txt verdict for our agent: True / False / None (no reachable robots.txt).
        None is NOT permission and NOT refusal; it is missing information."""
        return robots.allowed(url, self.user_agent)

    # -- the fetch ---------------------------------------------------------------------

    def get(self, url: str) -> Fetched:
        """Fetch once, honestly, over HTTP/2. See the class docstring for what it raises."""
        return _fetched(self.get_response(url))

    def get_range(self, url: str, start: int, end: int | None = None) -> Fetched:
        """A byte range of `url`, with every guarantee `get` gives (interval, backoff,
        refusals, robots, TLS supplements, the curl opt-in).

        `get_range(u, 0, 1023)` is the first KiB; `get_range(u, 4096)` is everything from
        byte 4096; `get_range(u, -512)` is the LAST 512 bytes (a PDF's trailer). A host that
        honours it answers 206; one that ignores it answers 200 with the whole body, and
        `status` says so -- check it before trusting the length. `Content-Range` (the total
        size) is in `get_response(url, (start, end)).headers`.
        """
        return _fetched(self.get_response(url, (start, end)))

    def get_response(self, url: str, byte_range: tuple[int, int | None] | None = None
                     ) -> httpx.Response:
        """`get`'s whole response (status, headers, body, final `.url`), for a caller that
        needs a header such as `Content-Range`. Same errors, same guarantees; the transport
        (Python or, on a challenged host with the opt-in, curl) is invisible to the caller.
        The only request header a caller may add is the Range, so the identity cannot be
        changed through here."""
        extra: dict[str, str] = {}
        if byte_range is not None:
            start, end = byte_range
            if start < 0 and end is None:
                extra["Range"] = f"bytes={start}"          # suffix range: "bytes=-N"
            elif start >= 0 and (end is None or end >= start):
                extra["Range"] = f"bytes={start}-{'' if end is None else end}"
            else:
                raise ValueError(f"bad byte range ({start}, {end})")
        if self.enforce_robots and self.allowed(url) is False:
            raise Refused(f"{url}: robots.txt disallows {self.user_agent!r} "
                          f"(enforce_robots=True is this corpus's decision)")
        host = urllib.parse.urlsplit(url).netloc
        attempt = 0
        while True:
            via_curl = host in self._curl_hosts
            self._throttle(url)
            resp = self._send_curl(url, extra) if via_curl else self._send_python(url, extra)

            code = resp.status_code
            if code == 429 and attempt < len(self.backoff):
                # 429 IS NOT A REFUSAL. It means "you are going too fast" -- a request to
                # slow down, not a decision to exclude us.
                wait = _retry_after(resp.headers) or self.backoff[attempt]
                self._log(f"    429 from {host} -- backing off {wait:g}s "
                          f"(attempt {attempt + 1}/{len(self.backoff)})")
                self._sleep(wait)
                attempt += 1
                continue
            challenge = self._classify_challenge(resp)
            if challenge and self.curl_on_challenge and not via_curl:
                # The Python client was challenged and this corpus opted in (ADR-0018): same
                # request, same honest identity, the system curl's connection. Remembered
                # per host so later requests do not pay for a doomed first attempt. A host
                # that challenges curl too falls through to the raise below.
                self._log(f"    {host} challenged the Python client -- using curl (ADR-0018)")
                self._curl_hosts.add(host)
                continue
            if challenge:
                raise Challenge(f"{url}: {challenge}")
            if code in (401, 403, 429):
                mitigated = resp.headers.get("cf-mitigated", "")
                raise Refused(f"{url}: HTTP {code}"
                              f"{' (Cloudflare managed challenge)' if mitigated else ''}")
            resp.raise_for_status()
            return resp

    @staticmethod
    def _classify_challenge(resp: httpx.Response) -> str | None:
        """Why `resp` is a bot challenge, or None. Shared by both transports so a curl
        response is judged by exactly the rule the Python one is."""
        if (resp.status_code in (401, 403, 429)
                and resp.headers.get("cf-mitigated", "") == "challenge"):
            return f"HTTP {resp.status_code} (Cloudflare managed challenge)"
        if resp.status_code == 307 and not resp.headers.get("Location"):
            return "307 with no Location -- bot challenge"
        return None

    def _send_python(self, url: str, extra: dict) -> httpx.Response:
        host = urllib.parse.urlsplit(url).netloc
        try:
            return self.client().get(url, headers=extra or None)
        except httpx.ConnectError as e:
            if tls._is_verification_failure(e) or "CERTIFICATE_VERIFY" in str(e).upper():
                # The host serves its leaf without the intermediate. Named so it reads as
                # the host's misconfiguration, with the reviewed remedy, not a dead host.
                raise Refused(f"{url}: TLS chain incomplete (server omits its "
                              f"intermediate). Remedy: a reviewed chain supplement at "
                              f"_meta/tls-chain/{host}.pem (ADR-0012).") from e
            raise
        except httpx.RemoteProtocolError as e:
            # TLS completes and the connection is dropped rather than answered -- a
            # refusal delivered as a reset instead of a status code.
            raise Refused(f"{url}: connection closed without a response ({e})") from e

    def _send_curl(self, url: str, extra: dict) -> httpx.Response:
        """The same request through the system curl, returned as an `httpx.Response` so the
        classification above is ONE code path. The identity is `self.user_agent` and nothing
        else: `-q` stops ~/.curlrc adding headers, there is no cookie jar, no browser header
        set, no impersonation build (ADR-0018)."""
        host = urllib.parse.urlsplit(url).netloc
        if self._scratch is None:
            self._scratch = tempfile.TemporaryDirectory(prefix="fetch-curl-")
        scratch = pathlib.Path(self._scratch.name)
        body_path, head_path = scratch / "body", scratch / "headers"
        argv = [self._curl, "-q", "--silent", "--show-error", "--location",
                "--max-redirs", "20", "--proto", "=http,https", "--globoff",
                "--max-time", f"{self.timeout:g}", "-A", self.user_agent]
        for name, value in {**self.headers, **extra}.items():
            if name.lower() != "user-agent":
                argv += ["-H", f"{name}: {value}"]
        config_path = getattr(self.config, "config_path", None)
        bundle = tls.ca_bundle(pathlib.Path(config_path).parent, host, scratch) \
            if config_path is not None else None
        if bundle is not None:
            argv += ["--cacert", str(bundle)]
        argv += ["-o", str(body_path), "-D", str(head_path), "-w", "%{url_effective}",
                 "--url", url]
        try:
            proc = self._runner(argv, self.timeout)
        except subprocess.TimeoutExpired as e:
            raise httpx.ReadTimeout(f"{url}: curl exceeded {self.timeout:g}s") from e
        if proc.returncode != 0:
            raise _curl_error(url, host, proc.returncode,
                              (proc.stderr or b"").decode("utf-8", "replace").strip())
        status, headers = _parse_curl_headers(head_path.read_bytes())
        final = (proc.stdout or b"").decode("utf-8", "replace").strip() or url
        resp = httpx.Response(status, headers=headers, content=body_path.read_bytes(),
                              request=httpx.Request("GET", final))
        body_path.unlink(missing_ok=True)
        head_path.unlink(missing_ok=True)
        return resp

    def snapshot(self, url: str, dest: pathlib.Path, refetch: bool = False) -> tuple[bytes, bool]:
        """Fetch to `dest` unless it already exists. Returns (bytes, fresh).

        `fresh` is what `snapshots.retrieved_date` needs to decide whether `retrieved` may
        advance. Getting this wrong is not cosmetic: stamping the wall clock on a cached run
        moved `retrieved` forward every time an ingester ran, so the older a snapshot got,
        the fresher it claimed to be -- precisely backwards for the one field a reviewer
        uses to judge staleness.
        """
        dest = pathlib.Path(dest)
        if dest.is_file() and not refetch:
            return dest.read_bytes(), False
        got = self.get(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(got.body)
        return got.body, True
