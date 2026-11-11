# ADR-0018 — A challenged host may be fetched with the system curl, under the same User-Agent

**Status:** proposed, **AWAITING OWNER CONFIRMATION** · **Date:** 2026-11-11 · amends [ADR-0016](0016-ingest-primitives-are-shared-orchestration-is-local.md) (the fetching bullet and the `fetch.py` docstring's "we do not then retry wearing a Chrome fingerprint"). Decided provisionally during an unattended build; the owner was not available. It is opt-in and off by default, so it is cheap to reverse: delete `curl_on_challenge` and nothing that does not use it changes.

## Context

`fetch.py` records that Marion County was "refused" for four tranches because Python's `urllib` spoke HTTP/1.1, while `curl --http2` with the SAME User-Agent got 200. The docstring drew the conclusion that speaking HTTP/2 "is not impersonation -- it is being a current client, with the honest User-Agent unchanged", and made `httpx[http2]` a dependency.

The same shape has reappeared with the HTTP/2 client already in place. Measured 2026-11-11 from the build machine, one request each way to `https://www.cdt.ca.gov/policy/simm/`, two seconds apart, both sending
`OregonAI-CivicCorpus/1.36.4 (+https://github.com/OregonAI/corpus-toolkit; public-records archival)`:

```
toolkit Fetcher client (httpx, HTTP/2)   HTTP 403, cf-mitigated: challenge
system curl 8.5.0 (OpenSSL 3.0.13)       HTTP 200, text/html, server: cloudflare
```

An earlier measurement the same day (recorded in the task that started this work) found `www.gov.ca.gov` doing the same for pages and PDFs, and the Colorado Secretary of State rules site answering curl 200. Still refused to curl: `leginfo.legislature.ca.gov` (re-confirmed 2026-11-11: still Cloudflare managed challenge through the new transport), `oit.colorado.gov` (403), and `governor.colorado.gov` (connection fails). So the refusal at the first group of hosts is scored on **the connection the Python client makes (TLS handshake and HTTP/2 settings as OpenSSL-via-Python presents them), not on our declared identity**. The host is not refusing OregonAI; it is refusing a client library. Recording it as `Refused` ("the host excluded us") would be a false statement about the publisher, which is the confusion the `Refused` / `Challenge` split exists to prevent.

## Decision

`Fetcher(..., curl_on_challenge=True)` is an **opt-in** transport. Default `False`; with it off, every code path is byte-for-byte the previous behaviour.

When on: a request is made with httpx as always. If the response is a **`Challenge`** (a 401/403/429 carrying `cf-mitigated: challenge`, or a 307 with no `Location`) the same request is repeated through the system `curl` binary, with the same User-Agent, and the host is remembered so later requests to it go straight to curl. Nothing else triggers it: a plain 403, a 401, a TLS failure or a 404 is not retried.

It is the same Fetcher, not a second one. The curl response is turned into an `httpx.Response` and runs through the one classification and the one loop, so everything the Fetcher guarantees holds on both transports: per-host `min_interval` (the Python attempt and the curl attempt are separate requests and are spaced), 429 backoff with `Retry-After`, `Refused` / `Challenge` classification (**a host that challenges curl too is still a `Challenge`**; there is no second fallback and no loop), robots reporting and `enforce_robots`, the corpus's TLS chain supplements, binary bodies, the final URL after redirects, and status/headers. Failures use the vocabulary the Python path already raises (curl exit 60 is the ADR-0012 `Refused` naming its remedy; 28 is a timeout; 6/7 a `ConnectError`; 52/56/92 a closed connection).

**Timeouts.** `timeout` means what it means to httpx: a per-step limit, not a cap on the transfer. curl gets `--connect-timeout <timeout>` and `--speed-limit 1 --speed-time <timeout>` (abort when no data arrives for `timeout` seconds), so a large body from a slow host that keeps arriving succeeds on both transports. `--max-time` is a fixed one-hour total (`CURL_TOTAL_CAP`) that only stops a host trickling forever; the subprocess backstop is that plus 15 s.

**Concurrency.** Each curl request writes its body and headers to its own temporary files, removed in a `finally`, so a Fetcher shared across threads cannot return one URL's body under another's.

**Missing curl fails at construction** (`CurlUnavailable`, a `FetchError`), never silently falls back: a corpus that opted in believes challenged hosts are handled.

**TLS.** `tls.ca_bundle()` writes, for a host that has a supplement, a bundle of the system roots plus that supplement, under all of ADR-0012's refusals, and curl gets `--cacert <bundle>` for that host only. Not the supplement alone: `curl --cacert <intermediate-only>` accepts a partial chain (documented in `tls.py`), a wider trust than the Python client has. If the system CA file cannot be found the bundle is refused. One residual difference is stated rather than hidden: a supplemented intermediate is, under curl, a certificate in the store for that host, and curl's chain building permits a partial chain ending at it. It is still a public, signed, non-self-signed, unexpired CA certificate named for exactly one host.

**Ranged get.** `Fetcher.get_range(url, start, end=None)` (negative `start` is a suffix, "the last N bytes") returns a `Fetched`; `Fetcher.get_response(url, byte_range=None)` returns the whole `httpx.Response` for a caller that needs `Content-Range` or any header. Both go through the same loop, so a corpus no longer reaches into `client()` and loses the interval, backoff, refusal handling and the curl opt-in. The Range header is the only request header a caller can add. `Fetched` is deliberately **not** widened: it is a four-tuple that corpora unpack, and a fifth field would break them (hence `get_response` for headers).

### Why this is "being a current client" and not impersonation

The line the docstring already draws for HTTP/2 is: **a different way of speaking, with the declared identity unchanged, is not a disguise; a different claimed identity is.** Python's TLS/HTTP stack and curl's are two implementations of the same protocols. Which one a host's bot scoring prefers is a fact about the host's scoring, and using the one it accepts claims nothing false about who we are. The sent identity is identical, header for header, to what we send everywhere, and the request is polite by the same rules.

### Where the line is (the identity rule), enforced in code and tests

- The User-Agent sent is always `Fetcher.user_agent`, the honest one (or the corpus's own recorded override). Never a browser string.
- No browser header set (`Sec-Ch-*`, `Sec-Fetch-*`, a `Referer`); the only headers are the Fetcher's own plus an optional Range.
- No impersonation tooling: no curl-impersonate, no curl_cffi, no JA3/JA4 or HTTP/2 settings mimicry. The system `curl`, as shipped, with its own TLS stack.
- No challenge solving, no JavaScript, no cookie jar, and no cookies copied from anywhere. `-q` is passed so a user's `~/.curlrc` cannot add a header or an agent.
- A host that challenges curl as well is recorded as `Challenge`: `leginfo.legislature.ca.gov`, `oit.colorado.gov` and `governor.colorado.gov` stay unavailable and the corpus records them as stubs (ADR-0017). The opt-in widens nothing for a host that refuses us for real.

If a future host is reachable only by looking like a browser, the answer under this ADR is the one it already was: unavailable.

## Considered options

**Use curl always.** Rejected: eight live corpora pin this toolkit and their behaviour must not change on a version bump, and curl is a binary dependency the Docker images may not carry.

**Retry with curl on any 403.** Rejected: a plain 403 is a decision by the host. Only the explicit challenge signal, the thing that was shown to be scored on the connection, triggers it.

**A browser-fingerprint impersonation library.** Refused outright (see the line above); it would also be the thing ADR-0016 removed from the platform.

**Make `Fetched` carry headers.** Rejected for compatibility (above).

**Feature-flag by attribute assignment (`f.curl_on_challenge = True`).** Rejected: on an old toolkit it would set an attribute nothing reads and the corpus would believe it was protected.

## Consequences

- A corpus opts in with one keyword; every other corpus is unchanged.
- Whether `curl` is a platform dependency stays the corpus image's decision; the opt-in fails at construction if it is absent.
- **Feature detection for a corpus that must also run on an older toolkit** (states-it-governance's CI is on v1.36.4): `"curl_on_challenge" in inspect.signature(Fetcher.__init__).parameters`, and pass the keyword only then. At the old version the ingester behaves as it does today, a challenged host is a `Challenge`, and it must not treat the missing feature as an error.
- The challenged-then-curl pair is two requests to the host for the first URL on a challenged host, spaced by `min_interval`.
- **Open for the owner:** whether the use of curl against these hosts, a transport choice made to defeat scoring on our client's connection and not a declared change of who we are, sits inside the platform's reproduction policy as the `fetch.py` docstring reads it. This ADR argues it does; the owner decides, and until then it is only ever switched on by a corpus's own explicit choice.
