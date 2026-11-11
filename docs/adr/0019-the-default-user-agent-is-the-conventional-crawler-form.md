# ADR-0019 — The default User-Agent is the conventional crawler form; robots matches on the product token

**Status:** accepted · **Date:** 2026-11-11 · decided by the owner · amends [ADR-0016](0016-ingest-primitives-are-shared-orchestration-is-local.md) (the fetching bullet: "one honest User-Agent naming the platform, the toolkit version and the corpus repository")

## Context

Measured 2026-11-11 by the owner, from the build machine, against `https://oit.colorado.gov/robots.txt` (Colorado's shared CloudFront firewall, which fronts every colorado.gov site):

| User-Agent sent | Result |
|---|---|
| `OregonAI-CivicCorpus/1.36.4 (+https://github.com/OregonAI/corpus-toolkit; public-records archival)` (the old default) | 403 "Request blocked" |
| curl's default | 403 |
| `Mozilla/5.0 (compatible; OregonAI-CivicCorpus/1.36.4; +https://github.com/OregonAI/corpus-toolkit)` | 200 |

HTTP version, IPv4 and `Accept` headers made no difference. The firewall requires the `Mozilla/5.0` prefix; the compatible form passes and still names us.

## Decision

1. The ingest `Fetcher` default (`default_user_agent`) is
   `Mozilla/5.0 (compatible; OregonAI-CivicCorpus/<toolkit version>; +https://github.com/OregonAI/<repo>)`.
   A caller-supplied `user_agent=` still wins, unchanged.
2. **The purpose is not in the header.** "public-records archival" was dropped: the measured passing string did not carry it, an unmeasured addition could be what a strict firewall refuses, and the linked repository states the purpose. It stays in this ADR and the docs.
3. **Robots matching uses the product token, never the header.** `urllib.robotparser` matches on the text before the first `/` of the string it is given, so the new header would be matched as `Mozilla`, i.e. rules written for a browser family. `robots.product_token()` extracts the product from the crawler form only when it is one of our own (`OregonAI-CivicCorpus` for the Fetcher, `corpus-toolkit-change-detector` for the detector) and returns any other string unchanged, including a caller's own crawler-form agent, so a caller's agent matches exactly as before. `allowed()` and the crawl-delay lookup apply it; the full header is still what is sent when fetching robots.txt. A rule for `OregonAI-CivicCorpus` applies to us; a rule for `Mozilla` gets us nothing beyond what `*` gives.
4. **The drift detector** (`sources/changes.py`) sends the same form with the token `corpus-toolkit-change-detector`, because the same firewall refuses its old bare string. Its robots token is byte-for-byte what `USER_AGENT` was, so directive matching, `--check-robots` verdicts and everything keyed on them are unchanged. The one intended difference: a host that refused the old header while serving robots.txt (colorado.gov) now answers the robots request, so a previously `no robots.txt` verdict can become a real one.
5. `corpus_toolkit/remote.py`'s identity is unchanged: it fetches our own sibling indexes.

## Why this is honest identification, and where the line stays

`Mozilla/5.0 (compatible; Name/ver; +url)` is the convention Googlebot and Bingbot use: the prefix is a protocol-compatibility marker, and the parenthesis says who is calling, which version, and where to complain. It does not claim to be a browser; every identifying fact in it is true. The line stays where ADR-0016 and ADR-0018 put it: never a real browser's product tokens (`Chrome/`, `Safari/`, `AppleWebKit`, `Gecko`, `Firefox`, platform strings such as `X11; Linux`), no browser header sets, no impersonation, no challenge solving. Tests assert the absence of those tokens on the Fetcher, the drift detector and the curl argv.

## Consequences

- Every corpus's outgoing User-Agent changes on the next release. A host that allow-listed or block-listed the old exact string behaves differently; a publisher's server logs show a new agent string.
- Hosts that refused the old default at the firewall (colorado.gov) may now answer. Corpora that recorded such a source as `Refused` should re-test on the new release rather than assume.
- Robots outcomes do not move for any host that did not already treat the old header specially.
