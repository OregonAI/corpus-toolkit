#!/usr/bin/env python3
"""corpus-detect-changes — re-fetch every source declared in the corpus's source
manifest, diff content hashes, and write the drift report: `DRIFT.md`, rendered from
`drift-state.json`, plus the two per-run artifacts below. It files no issues (ADR 0015).

Until v1.34.0 a run opened one GitHub issue per changed source, one `Group drifted:`
finding per group whose every compared source changed (ADR 0010), and one `Access
failure:` issue per source whose fetch had failed past a threshold (ADR 0013), capped at
25 a run. Measured 2026-09-02: 90 of the org's 172 open issues were those tickets and none
was read. The claims stand; the medium moved. Each is now a section of `DRIFT.md`, written
every run, in the same words. Ported originally from oregon-policy-repo/src/detect_changes.py;
the Oregon-specific SharePoint-listing diff is NOT ported (a corpus keeps that as its own
script, importing `corpus_toolkit.repo.content_hash`).

Manifest shape (`_meta/corpus.yml`'s `source_manifest_path`, a file or a directory of
group files — see `corpus_toolkit.config.load_source_manifest_groups`):
  sources:
    - id: some-doc-id
      url: https://...
      sha256: <content_hash of the source as of the last review; "" until seeded>
      format: html   # optional; inferred from the URL's extension otherwise
      watch:         # optional, json sources only (corpus-toolkit#72): hash ONLY these
        - rowsUpdatedAt          # paths, so vendor counters that move on their own are
        - columns[].name         # inert by construction. Absent = hash the whole document.

THE BASELINE IS SEEDED BY THE RUN THAT FIRST FETCHES A SOURCE (ADR 0015). It used to be
written only under a human-typed `--record-baseline`, which appeared in no workflow:
oregon-counties (3,447 sources), oregon-audits (244) and oregon-budget ran their whole
lifetimes with `sha256: ''` everywhere — every source CHANGED every run, exit 0
(corpus-toolkit#68, #145). A source with no recorded baseline now has one recorded on the
run that fetches it, is listed under "Seeded this run" in `DRIFT.md`, and is compared from
the next run on. The manifest edit rides the same PR as the rest of the run's state.
`--record-baseline=refresh` is the one deliberate act left: accepting an observed change as
the new baseline without re-ingesting.

Do NOT seed from a document's frontmatter `source_sha256`. That is a different hash over
different input (`hash_snapshot`, over committed `.txt`), and the two agree only for
image-only scans where both fall back to raw bytes.

AN UNSEEDED SOURCE IS NOT A CHANGED SOURCE (ADR 0010, corpus-toolkit#145). `sha256: ''`
compares unequal to everything, so it is reported `no_baseline`, seeded, and never counted
as drift. It is still in `changed-sources.tsv` with an empty `old` column, because that
file's meaning predates this rule and corpus repos read it positionally.

A FETCH FAILURE THAT NEVER STOPS IS DIFFERENT FROM ONE THAT NEVER STARTED
(corpus-toolkit#166). `--strict` treats any fetch failure as fatal; default mode treats an
isolated one as noise, on purpose. `access-failures.json` accumulates the streak across
runs, and a source past `ACCESS_FAILURE_ESCALATE_RUNS` consecutive failed runs or
`ACCESS_FAILURE_ESCALATE_DAYS` elapsed days is marked **escalated** in `DRIFT.md` — worded
as a fact about our access, never as a claim about upstream, because this tool cannot tell
a block from a page that moved and does not guess.

FOUR ARTIFACTS, ALL PUBLIC SURFACE the moment this runs inside a corpus repo (AGENTS.md:
"anything reachable from a corpus repo is public, whether or not this repo calls it"). All
are written on every run that reaches the fetch loop — including a run that fails every
fetch, which is the run each of them exists for — and none when `--check-robots` returns
before fetching anything.

* `changed-sources.tsv`, at the corpus root. Four tab-separated columns —
  `id, url, old_sha256, new_sha256` — one row per source whose hash MOVED, in manifest
  order. STABLE SINCE ITS INTRODUCTION and pinned by test: a corpus repo may keep reading
  it positionally (executive-regulatory-frameworks' bulletin report does). It only ever
  answers "what changed", never "what happened to everything else" (corpus-toolkit#160).
* `source-outcomes.json`, at the corpus root (corpus-toolkit#160) — THIS RUN's observation
  of every in-scope source: one of six outcomes (`changed`, `unchanged`, `no_baseline`,
  `fetch_failed`, `unreadable_json`, `watch_path_missing`), plus the per-group breakdown
  and run totals. A source outside `--group` scope is absent from it. STABILITY: the
  top-level shape, the six strings and the keys of a `groups` entry are the contract;
  `schema_version` bumps on a breaking change; additive fields are not breaking.
* `access-failures.json`, at the corpus root (corpus-toolkit#166) — one record per source
  CURRENTLY on a fetch-failure streak: consecutive failed runs and the date the streak
  began. Present only while the last observation was `fetch_failed`; a source outside this
  run's `--group` scope is HELD; one retired from an enumerated group is pruned.
* `drift-state.json` and `DRIFT.md`, at the corpus root (ADR 0015) — the ROLLING view.
  One record per source the corpus knows about, carrying its LAST observation, the date a
  change was first observed against the current baseline, and when it was seeded or its
  baseline accepted. Held/pruned by the same rules as `access-failures.json`. `DRIFT.md`
  is rendered from it and from `access-failures.json` by `corpus_toolkit.sources.drift_report`
  and can be re-rendered without a fetch (`corpus-drift-report`). It is not `--check`-gated
  in CI: it records observations, which no local command can reproduce.

Exit codes: 0 on a run that could do its job — drift is a signal, not an error. 1 when a
fetch fails under `--strict`; when failures are systemic (>20% of fetches); when the scope
came out empty so nothing was checked; when a manifest rewrite was refused; and,
regardless of `--strict`, when a `watch`-declared source returned a document missing a
declared path or one that will not parse as json — the bytes ARRIVED, so this is not
upstream being briefly unreachable, and the source stays uncompared on every run until
somebody looks. `RunVerdict` is the one place those reasons live; `DRIFT.md`'s "Last run"
section prints the same list. The common thread: a run that could not do the thing it
reports on must not report success.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit.config import (iter_manifest_sources,
                                   validate_watch_declarations)
from corpus_toolkit.repo import (WatchedDocumentUnreadable, WatchedPathMissing,
                                 content_hash)
from corpus_toolkit.sources import drift_report, tls

USER_AGENT = "corpus-toolkit-change-detector"

# HTTP/2, AND WHY IT IS NOT A PERFORMANCE CHOICE (corpus-toolkit#162).
#
# This fetched with `urllib`, which speaks only HTTP/1.1, and a growing number of
# government hosts refuse HTTP/1.1 outright. With the SAME User-Agent:
#
#     curl --http2    -> 200
#     curl --http1.1  -> 403
#     python urllib   -> 403
#
# 54 sources in oregon-counties could never be seeded because of it -- lake.county.codes
# (all 14) and www.codepublishing.com among them -- while THAT REPO'S OWN INGEST reached
# the same hosts fine, because it already speaks HTTP/2. The mirror held the documents and
# could not watch them for change: two halves of one repository disagreeing about whether
# a site is reachable.
#
# oregon-counties/src/fetch.py records what the wrong diagnosis cost -- Marion County was
# marked `unavailable` for four tranches on the belief that our identity was being refused,
# and a headless browser was reached for before anyone checked the protocol version. Its
# conclusion is the one this follows: speaking HTTP/2 is NOT impersonation. The
# User-Agent stays honest and identifying; only the protocol moves to one from 2015.
#
# Nothing here widens the header set or pretends to be a browser. That was never necessary.
HTTP2_TIMEOUT = 60

_client = None
# Host -> verifying SSL context, from `_meta/tls-chain/<host>.pem`. Empty for every corpus
# that declares none, which is all of them until one measures a server that omits its chain.
# See `corpus_toolkit/sources/tls.py` for why supplying an intermediate is not a trust
# widening, and ADR 0012 for the decision.
_chain_supplements: dict = {}


def configure_chain_supplements(meta_dir) -> dict:
    """Load `<meta_dir>/tls-chain/*.pem` and rebuild the client around them.

    Called once, from `main`, BEFORE the first fetch. Returns what it loaded so the run can
    say so out loud: an exception nobody can see in the log is how a workaround outlives the
    condition it was written for.
    """
    global _chain_supplements, _client
    _chain_supplements = tls.load(meta_dir)
    _client = None
    return _chain_supplements


def _http_client():
    """One HTTP/2 client, reused. Redirects followed; TLS still verified.

    Where a chain supplement is declared for a host, that host is mounted on its own
    transport carrying its own verifying context. Every OTHER host keeps the default one,
    which is what makes this an exception rather than a setting.
    """
    global _client
    if _client is None:
        import httpx

        # NO LOCAL h2 CHECK. httpx refuses to build an http2 client without `h2`
        # (ImportError, "Using http2=True, but the 'h2' package is not installed"), so a
        # guard here would restate a rule httpx already enforces -- one fact declared
        # twice, with the copies free to drift. `tests/test_http2_fetch.py` asserts that
        # refusal instead, so if httpx ever starts downgrading silently we find out from a
        # failing test rather than from 403s nobody can explain.
        #
        # The real fix is the DEPENDENCY: pyproject asks for `httpx[http2]`, and the extra
        # is what pulls `h2` in.
        _client = httpx.Client(http2=True, follow_redirects=True,
                               timeout=HTTP2_TIMEOUT,
                               mounts=tls.mounts(_chain_supplements),
                               headers={"User-Agent": USER_AGENT})
    return _client


def fetch(url: str) -> bytes:
    """Fetch over HTTP/2 where the host offers it, HTTP/1.1 where it does not.

    httpx negotiates via ALPN, so a host that only speaks HTTP/1.1 is unaffected -- this
    ADDS a protocol rather than requiring one.
    """
    resp = _http_client().get(url)
    resp.raise_for_status()
    return resp.content


def _format_for(url: str, declared: str | None) -> str:
    if declared:
        return declared
    path = url.lower().split("?")[0]
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "html"
    return ext if ext in ("pdf", "xls", "xlsx", "docx", "xml") else "html"


# AN ALLOWLIST, for the same reason the feature itself is one: a `watch` list needs a json
# body, and enumerating the formats that are NOT json makes every format nobody thought of
# a silent acceptance. That is not hypothetical -- the first version of this check was a
# blocklist exempting `_format_for`'s `html` result, and since `_format_for` returns `html`
# for EVERY extension it does not recognise, the exemption written to protect Socrata
# `.json` urls also swallowed `.html`, `.csv` and `.aspx`. `.xml` and `.pdf` were refused
# and a real HTML page was not, with nothing declared in either case.
_WATCH_COMPATIBLE_FORMATS = frozenset({"json", "geojson"})


def _url_extension(url: str) -> str:
    """The url's filename extension, lowercased, or "" if it has none.

    Same derivation as `_format_for` (query string dropped, dot required in the last path
    segment), kept beside it so the two cannot drift.
    """
    path = url.lower().split("?")[0]
    tail = path.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1] if "." in tail else ""

# THE ESCALATION THRESHOLD (corpus-toolkit#166), decided by the operator on 2026-09-02
# against measured data, not derived here:
#
#     ESCALATE ON 2 CONSECUTIVE FAILED RUNS OR 14 ELAPSED DAYS, WHICHEVER COMES FIRST.
#
# `FETCH FAILED` prints on every run and nothing accumulated it across runs, so a source
# failing its thirtieth run in a row read exactly like one failing its first. The live
# instance: executive-regulatory-frameworks#140, 45 sources failing since 2026-08-05 at
# 3.3% of 1,347 — under the 20% systemic guard, so 22 days and 22 `success` results with
# no drift detection at all on those 45.
#
# ELAPSED TIME IS THE HARM, which is why there are two arms and not one. "This source has
# been unwatched for two weeks" is true regardless of how often the cron that watches it
# runs. Run-counting alone gives a slow-cadence corpus a longer blind window than a fast
# one — backwards, since the slower the cadence, the more each missed run costs. Measured
# cadences: oregon-counties, oregon-collective-bargaining and oregon-kpm run WEEKLY
# (cron '0 14 * * 1'); executive-regulatory-frameworks runs MONTHLY. Under a run-count-only
# rule, reaching even 2 consecutive runs is ~14 days for the weekly corpora and ~30-60 days
# for the monthly one — and the second run is the FIRST opportunity a pure run-count rule
# has to notice anything, so a monthly corpus would sit unwatched for a month no matter
# what the threshold is set to. The elapsed-days arm does not wait for that second fetch:
# it is evaluated from the clock alone, every time this tool runs for ANY reason, over
# EVERY currently-tracked access failure regardless of this run's `--group` scope (see
# `_access_failure_escalations`) — so a source in a monthly group can escalate off a
# WEEKLY group's own run, without waiting for its own next scheduled fetch.
#
# REJECTED: escalating on the FIRST failure. A 429 is transient by definition —
# oregon-counties currently carries 9 of them from ecode360.com rate-limiting alone — and a
# ticket per throttle trains everyone to ignore the channel. That is how 90 open
# `Source changed:` issues across three corpora came to sit unread: noise habituates.
#
# REJECTED: pure run-counting with no elapsed-days arm (the withdrawn corpus-toolkit#166
# attempt, `wip/166-access-failure-escalation`, used 3 consecutive runs). Under that rule
# alone a monthly corpus takes 3 months to say a fetch has been failing, for a defect that
# went unnoticed for 22 days and was worth knowing about in about two weeks.
ACCESS_FAILURE_ESCALATE_RUNS = 2
ACCESS_FAILURE_ESCALATE_DAYS = 14

# Fraction of the bytes fetched on the HTML/XML path that one `volatile_patterns:` entry
# may strip before the run says so loudly. A genuine volatile token is small: a session
# id, a CDN hash, an application footer version — tens of bytes in a page of tens of
# thousands, under 0.1%. A pattern removing a tenth of every page is not stripping a token,
# it is deleting content, and content deleted before hashing can never produce drift: two
# documents differing only inside the removed region hash identically and the run reports
# `0 changed`, exit 0, forever. That is a check that cannot fail, reachable from one line
# of corpus config.
#
# REPORTED, NOT REFUSED. A breadth limit would be a policy this toolkit has no standing to
# set — a corpus whose upstream really does wrap 40% of each page in a rotating banner is
# not doing anything wrong, and refusing would push it back to patching the toolkit or
# running a second hasher, which is the #66 failure mode. Same reasoning as --check-robots
# and corpus-detect-unsourced: measure, say the number, leave the decision with the
# operator and the reviewer of the PR that adds the pattern.
VOLATILE_BREADTH_WARN = 0.10


def _utcnow_date() -> date:
    """Today, UTC. A SEAM: tests fix the clock via `mock.patch.object(changes,
    "_utcnow_date", ...)` rather than sleeping real days to drive the elapsed-days arm of
    the access-failure escalation threshold (corpus-toolkit#166)."""
    return datetime.now(timezone.utc).date()


class AccessFailureRecord(NamedTuple):
    """One source's persisted fetch-failure streak, as recorded in `access-failures.json`
    (corpus-toolkit#166) — see that artifact's docstring in this module's header for why
    this state lives beside `source-outcomes.json` rather than in the manifest."""
    group: str
    id: str
    consecutive_failures: int
    first_failed_at: str  # ISO date the CURRENT streak began, e.g. "2026-08-05"


def _load_access_failures(config) -> dict[tuple[str, str], AccessFailureRecord]:
    """Read `access-failures.json` from the last run, keyed `(group, id)`.

    DEGRADES TO EMPTY on anything unreadable — a missing file (first run ever), a file that
    is not json, an entry missing a field — rather than raising. The alternative is a run
    that cannot start because ITS OWN bookkeeping file from a prior run is damaged, which
    is a worse failure than forgetting a streak: a source mid-escalation loses its count and
    resumes counting from zero, which is recoverable over the next couple of runs, while a
    run that refuses to execute reports nothing at all.

    SAID OUT LOUD, either way — degrading is the right recovery, but a degrade that prints
    nothing is indistinguishable from every streak having genuinely cleared, which is the
    corpus-toolkit#53 shape one artifact over: state was lost and nothing said so. A missing
    file (path does not exist) is not a warning -- it is the ordinary first-run and
    every-run-since-the-last-clear case and would fire on every green run.
    """
    path = config.root / "access-failures.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: access-failures.json exists but could not be read ({e}) — "
              f"every access-failure streak it carried is starting over from zero this "
              f"run, and the damaged file is about to be overwritten.", file=sys.stderr)
        return {}
    out: dict[tuple[str, str], AccessFailureRecord] = {}
    dropped = 0
    for entry in data.get("sources") or []:
        try:
            key = (str(entry["group"]), str(entry["id"]))
            out[key] = AccessFailureRecord(
                group=key[0], id=key[1],
                consecutive_failures=int(entry["consecutive_failures"]),
                first_failed_at=str(entry["first_failed_at"]))
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue  # one damaged entry does not cost every other source its streak
    if dropped:
        print(f"WARNING: {dropped} entr{'y' if dropped == 1 else 'ies'} in "
              f"access-failures.json could not be read and lost their streak this run.",
              file=sys.stderr)
    return out


def _update_access_failures(prior: dict[tuple[str, str], AccessFailureRecord],
                            outcomes: list["SourceOutcome"], in_scope: set,
                            checked_groups: set, today: date, declared_groups: set
                            ) -> dict[tuple[str, str], AccessFailureRecord]:
    """This run's `access-failures.json`, built from last run's state plus this run's
    fetch outcomes.

    FOUR RULES, one per source shape this run can observe:

    * `outcome == "fetch_failed"` — the streak continues (or begins): consecutive_failures
      increments, `first_failed_at` is carried over from the existing record (or set to
      TODAY if this is the first failure of a new streak).
    * Any other outcome for an IN-SCOPE source — the fetch succeeded, in the sense that
      bytes arrived (`changed`, `unchanged`, `no_baseline`, `unreadable_json`,
      `watch_path_missing` are all successful fetches per this module's own docstring: "the
      bytes ARRIVED; this is not upstream being briefly unreachable"). The streak clears.
    * A source belonging to a group this run CHECKED but that is no longer in the
      manifest — retired. Its record is pruned rather than kept forever asserting a dead
      source is currently failing (the withdrawn attempt's own item #6).
    * A source whose whole GROUP no longer appears in the manifest at all — the group file
      was deleted, not just emptied. `checked_groups` is derived FROM the manifest's own
      declared groups (`declared_groups & args.group`, or all of `declared_groups`), so a
      group that is not declared anywhere is never a member of `checked_groups` either and
      the rule above never reaches it: a deleted group file held its sources forever,
      re-escalating them every run behind the placeholder "url unknown — source since
      removed from the manifest" text, because closing the resulting ticket as "source
      retired" only refiled it on the next run. Pruned unconditionally, regardless of this
      run's `--group` filter — a group that does not exist cannot be excluded from scope,
      only be gone.

    A source in a group this run did NOT check (`--group` excluded it, but the group is
    still declared somewhere in the manifest) is HELD: untouched, neither incremented nor
    cleared, because this run observed nothing about it. That matters because ERF runs
    different `--group` sets from different crons — a source outside today's scope did not
    just start succeeding.
    """
    state = dict(prior)
    for o in outcomes:
        key = (o.group, o.id)
        if o.outcome == "fetch_failed":
            existing = prior.get(key)
            streak = (existing.consecutive_failures if existing else 0) + 1
            first_failed = existing.first_failed_at if existing else today.isoformat()
            state[key] = AccessFailureRecord(o.group, o.id, streak, first_failed)
        else:
            state.pop(key, None)
    for key in list(state):
        g, sid = key
        if g not in declared_groups:
            del state[key]  # the group itself no longer exists in the manifest
        elif g in checked_groups and key not in in_scope:
            del state[key]  # retired from a group this run actually enumerated
    return state


def _write_access_failures(config, state: dict[tuple[str, str], AccessFailureRecord]
                           ) -> None:
    path = config.root / "access-failures.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "sources": [rec._asdict() for rec in
                   sorted(state.values(), key=lambda r: (r.group, r.id))],
    }, indent=2, sort_keys=True) + "\n")


class AccessFailureEscalation(NamedTuple):
    group: str
    id: str
    consecutive_failures: int
    elapsed_days: int


def _access_failure_escalations(state: dict[tuple[str, str], AccessFailureRecord],
                                today: date) -> list[AccessFailureEscalation]:
    """Every currently-tracked source that has crossed the operator's threshold.

    OVER THE WHOLE STATE, not just this run's in-scope sources — including HELD entries
    from groups this run never touched. That is deliberate and is the entire point of the
    elapsed-days arm: a monthly-cadence group's own next fetch is ~30 days away, so a rule
    that only looked at sources this run fetched could never notice an elapsed-days
    escalation before that fetch happens — by which point 2 consecutive runs has usually
    already fired too, and the two arms would never be observed to disagree (see this
    module's `ACCESS_FAILURE_ESCALATE_RUNS`/`_DAYS` comment). Evaluating the clock against
    EVERY tracked record, on every invocation for any reason, is what lets a WEEKLY group's
    own run notice a MONTHLY group's overdue failure without waiting for that group's own
    cron.

    WORST FIRST: sorted by elapsed days descending, then streak descending, then
    (group, id) — deterministic, and spends the shared issue budget (see `main`) on the
    longest-unwatched sources first when a run has more escalations than room.
    """
    out = []
    for rec in state.values():
        first_failed = date.fromisoformat(rec.first_failed_at)
        elapsed = (today - first_failed).days
        if (rec.consecutive_failures >= ACCESS_FAILURE_ESCALATE_RUNS
                or elapsed >= ACCESS_FAILURE_ESCALATE_DAYS):
            out.append(AccessFailureEscalation(rec.group, rec.id,
                                               rec.consecutive_failures, elapsed))
    return sorted(out, key=lambda e: (-e.elapsed_days, -e.consecutive_failures,
                                      e.group, e.id))


def _report_robots(config, groups) -> int:
    """Report every source host's robots.txt position. Exits 0 — see --check-robots.

    Grouped BY HOST rather than by source, because the position is a property of the host
    and a corpus can have hundreds of sources behind a dozen of them; a per-source listing
    would bury the finding under repetition.
    """
    from corpus_toolkit.sources import robots as robots_mod

    hosts: dict[str, list[str]] = {}
    for s in iter_manifest_sources(config):
        if groups and s.get("_group") not in groups:
            continue
        hosts.setdefault(urllib.parse.urlsplit(s["url"]).netloc, []).append(s["id"])

    refused, states_position, unknown = [], [], []
    for host in sorted(hosts):
        url = f"https://{host}/"
        rec = robots_mod.ai_position(url, USER_AGENT)
        verdict = robots_mod.allowed(url, USER_AGENT)
        n = len(hosts[host])
        if verdict is False:
            refused.append(host)
            mark = "REFUSES US"
        elif verdict is None:
            unknown.append(host)
            mark = "no robots.txt"
        else:
            mark = "permitted"
        print(f"{mark:14} {host}  ({n} source{'s' if n != 1 else ''})")
        if rec.states_ai_position:
            states_position.append(host)
            if rec.blocked_ai_agents:
                print(f"               states a position on AI crawling: blocks "
                      f"{', '.join(rec.blocked_ai_agents)}")
            if rec.content_signal:
                print(f"               Content-Signal: {rec.content_signal}")
        if rec.crawl_delay:
            print(f"               Crawl-delay: {rec.crawl_delay:g}s")

    print(f"\n{len(hosts)} host(s): {len(refused)} refuse our agent, "
          f"{len(states_position)} state a position on AI crawling, "
          f"{len(unknown)} serve no reachable robots.txt.")
    if states_position:
        # The distinction that the raw allowed/denied count hides: a host can permit our
        # UA while plainly refusing this CATEGORY of use. Whether that refusal binds a
        # civic-corpus mirror is a judgement for the operator, not this tool.
        print("A host that permits our user agent while blocking named AI crawlers has "
              "still stated something about this kind of use. Record the decision on the "
              "source or group so it is reviewable and not re-derived every run.")
    if unknown:
        print("No reachable robots.txt is missing information, not permission.")
    return 0


def _warn_recheck_is_not_honoured(config) -> int:
    """Say out loud that `recheck:` configures nothing. Returns the number of declarations.

    Manifests declare a re-check cadence, top-level and per-source, and NOTHING reads it.
    The real cadence is whatever the calling workflow's cron says. Two ways that bites, and
    the second is the one that actually did: a curator writing `recheck: annual` believes
    they configured something, and a REVIEWER reading the human-approved manifest has no
    reason to go read the cron — so `recheck: annual` beside a weekly cron reads as
    deliberate restraint. oregon-audits declared annual, because an audit report is
    immutable once published, and re-fetched 242 PDFs every week.

    Deleting the key would put cadence in exactly one place, but it is already written into
    manifests across the platform and carries real curator intent about how an upstream
    should be treated. Warning keeps that intent legible and makes the dead key
    self-documenting, which is what it was missing.
    """
    seen: list[str] = []
    for group in config_mod.load_source_manifest_groups(config):
        if group.get("recheck"):
            seen.append(f"{group.get('group') or 'manifest'} (group-level)")
        for s in group.get("sources") or []:
            if isinstance(s, dict) and s.get("recheck"):
                seen.append(str(s.get("id", "<unnamed>")))
    if seen:
        shown = ", ".join(seen[:5]) + (f", +{len(seen) - 5} more" if len(seen) > 5 else "")
        print(f"NOTE: {len(seen)} `recheck:` declaration(s) in the source manifest are NOT "
              f"honoured — this tool checks every source on every run. The real cadence is "
              f"the workflow's cron schedule. ({shown})", file=sys.stderr)
    return len(seen)


def _annotate(title: str, message: str) -> None:
    """Emit a GitHub Actions warning annotation, where one will actually be read.

    Both observed capped runs put their warning on stderr and concluded `success`; the
    notice sat unread near line 3,870 of a 3,900-line log while drift detection was inert
    for a week (corpus-toolkit#67). An annotation surfaces in the run summary instead.
    Outside Actions this prints nothing rather than leaking `::warning::` into a terminal —
    the exit code carries the same fact there.
    """
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title={title}::{message}")


# `id:` and `sha256:` as they appear in a manifest group file. Line-level, because the
# alternative — yaml.safe_load + yaml.safe_dump — reformats a file a human curates and
# reviews: it drops every comment (`sha256: ""  # filled at first fetch` is real, in
# oregon-counties' manifest template), re-quotes every scalar, and turns a two-value seed
# into a whole-file diff no reviewer can read. Regexes over YAML are fragile, so nothing
# they produce is written until it has been re-parsed and compared — see _record_baselines.
# The line-level `sha256` editor and its two verifiers live in sources/manifest.py since
# v1.36.0, because ingest moves the baseline too (ADR-0016) and one field wants one writer.
# Imported under the names this module and its tests have always used.
from corpus_toolkit.sources.manifest import (  # noqa: E402
    _ID_RE, _SHA_RE, _scalar,
    plan_sha_edits as _plan_sha_edits,
    rewrite_sha256 as _rewrite_sha256,
    rewrite_problem as _rewrite_problem,
)


def _record_baselines(config, fetched: dict, in_scope: set, mode: str,
                      uncompared: dict | None = None) -> dict:
    """Write freshly computed hashes into the manifest group files. Returns a tally.

    THE MANIFEST IS CURATED DATA a human reviews in a PR, and every rule here follows from
    that:

    * Seed mode runs on EVERY drift run since ADR 0015 and fills EMPTY baselines only;
      the edit rides the same reviewed PR as the rest of the run's state.
    * Seed mode fills EMPTY baselines only. Replacing a recorded one is accepting an
      upstream change without reading it, so it takes `--record-baseline=refresh` to say
      so out loud — which is also the reconciliation path after adding a
      `volatile_patterns:` entry (corpus-toolkit#66).
    * A source we could not fetch is left byte-for-byte alone. A 403 must not overwrite a
      good baseline and must not write an empty one: "could not check" is not "unchanged".
    * The edit is line-level, on BYTES, and then verified twice — by re-parsing, so a
      value cannot land in the wrong entry, and line by line, so nothing else in the file
      (a comment, a quoting style, a CRLF ending) can move. A file that fails either check
      is left untouched and the operator is told which source and why. A curated file is
      not worth a regex's confidence.
    * Nothing is committed or pushed. The diff lands in the working tree and goes through
      review like any other change to reviewed data.
    """
    tally = {"written": 0, "already_current": 0, "left_alone": 0, "failed_fetch": 0,
             "files": [], "left_alone_ids": [], "refused": [], "written_ids": []}
    # The SAME SET the totals line and the group breakdown count, passed in rather than
    # re-derived. Deriving it from `fetched` was wrong for duplicate ids: `fetched` is keyed
    # (group, id), so a successful sibling entry populated the key and the failing entry
    # looked fetched -- and copying an entry, editing the url and forgetting the id is
    # exactly how a duplicate arises.
    # A COUNT PER KEY, not a membership test. `fetched` is keyed (group, id), so for a
    # duplicated id a successful sibling entry populated the key and the failing entry
    # looked fetched -- and copying an entry, editing the url and forgetting the id is
    # exactly how a duplicate arises. A membership test then over-counted the reverse case,
    # where both entries failed and the totals line counted two. The count is built where
    # the outcomes are known, per entry, and consumed once per key here.
    uncompared = uncompared or {}
    counted_uncompared: set = set()
    # ACROSS ALL FILES, KEYED `(group, id)` -- the space `fetched` and `in_scope` already
    # use. Built per FILE, two files declaring the same `group:` collided in the hash map
    # while looking unique to the duplicate guard below, so one entry's hash was written
    # into the other: silently, no refusal, exit 0. That is the wrong-entry write
    # `_plan_sha_edits` refuses by name, one level up where the guard did not look
    # (corpus-toolkit#120).
    #
    # Two files under DIFFERENT groups may legitimately share an id -- directory mode
    # defaults `group` to the file stem -- and they key differently, so they are untouched.
    group_files = list(config_mod.load_source_manifest_group_files(config))
    occurrences: dict[tuple[str, str], int] = {}
    where: dict[tuple[str, str], list] = {}
    for path, group in group_files:
        gname = group.get("group") or "manifest"
        for s in (group.get("sources") or []):
            if not isinstance(s, dict):
                continue
            key = (gname, str(s.get("id", "")))
            occurrences[key] = occurrences.get(key, 0) + 1
            if path not in where.setdefault(key, []):
                where[key].append(path)

    for path, group in group_files:
        gname = group.get("group") or "manifest"
        sources = [s for s in (group.get("sources") or []) if isinstance(s, dict)]
        updates: dict[str, str] = {}
        for s in sources:
            sid = str(s.get("id", ""))
            key = (gname, sid)
            if key not in in_scope:
                continue
            if uncompared.get(key) and key not in counted_uncompared:
                # BEFORE the duplicate-id branch. A source that was both duplicated and
                # never compared was counted by the totals line and the group breakdown but
                # not here -- one of the two combinations where "one definition of `not
                # compared`" was false. Counting it first costs nothing: the duplicate
                # branch below still refuses, and both facts are true of the same source.
                # Once per key, since a duplicated id reaches this line twice.
                counted_uncompared.add(key)
                tally["failed_fetch"] += uncompared[key]
            if occurrences[key] > 1:
                if sid not in [r[1] for r in tally["refused"]]:
                    files = where.get(key) or [path]
                    # NAMES EVERY FILE INVOLVED. The message was written for the within-file
                    # case and says "in this group file"; an operator told that about a
                    # CROSS-file collision searches the wrong one and finds a single entry
                    # that looks fine.
                    scope = (f"duplicate id in this group file"
                             if len(files) == 1 else
                             f"duplicate id across {len(files)} group files that share the "
                             f"group name {gname!r} ("
                             + ", ".join(str(f.name) for f in files) + ")")
                    tally["refused"].append(
                        (path, sid, f"{scope} — cannot tell which entry the fetched hash "
                                    "belongs to. No baseline was written for this id; the "
                                    "other sources in these files were written normally. "
                                    "Give the entries distinct ids and re-run."))
                continue
            new = fetched.get(key)
            if new is None:  # already counted above
                # No hash was computed for this source, so nothing may be written: "could
                # not check" is not "unchanged". Deliberately does NOT distinguish WHY --
                # a failed fetch, an unreadable body and a missing `watch` path are the same
                # decision here -- so the caller's wording names no reason at all. It named
                # two, and the third one then read as one of those two.
                continue
            old = str(s.get("sha256") or "").strip()
            if old == new:
                tally["already_current"] += 1
            elif old and mode != "refresh":
                tally["left_alone"] += 1
                tally["left_alone_ids"].append(sid)
            else:
                updates[sid] = new
        if not updates:
            continue
        # Bytes in, bytes out. `read_text`/`write_text` translate line endings, so a CRLF
        # manifest would come back LF THROUGHOUT — a whole-file rewrite, which is the one
        # thing the line-level editor exists to avoid, and invisible to a check that
        # compares parsed YAML.
        text = path.read_bytes().decode("utf-8")
        new_text, unlocated = _rewrite_sha256(text, updates)
        problem = _rewrite_problem(text, new_text, updates, unlocated)
        if problem:
            tally["refused"].append(
                (path, ", ".join(unlocated) or "<verification failed>",
                 f"{problem} NOTHING was written to this file — not even the sources that "
                 f"verified, because a file this tool cannot account for is a file it does "
                 f"not touch. Fix the manifest by hand and re-run."))
            continue
        path.write_bytes(new_text.encode("utf-8"))
        tally["written"] += len(updates)
        tally["written_ids"].extend(updates)
        tally["files"].append(path)
    return tally


def _print_group_breakdown(per_group: dict) -> None:
    """changed/checked per group, on EVERY run (corpus-toolkit#67 item 1).

    The line that makes a bulk false positive self-evident. ERF's capped run was `oar
    484/484` and a DEQ group `52/52` — a template change and a broken fetch — while the
    five genuine changes sat in three other groups; the totals line could not distinguish
    that from 544 real revisions and nobody could without opening every issue. Unseeded
    counts ride along because they separate the two shapes that both read as 100%: a
    stale baseline (#66) and no baseline at all (#68).
    """
    if not per_group:
        return
    parts = []
    for name in sorted(per_group, key=lambda g: (-per_group[g]["changed"], g)):
        st = per_group[name]
        unseeded = f" [{st['unseeded']} unseeded]" if st["unseeded"] else ""
        # `socrata 0/2` reads identically whether both sources were compared and found
        # stable or NEITHER was compared at all. This is the line that makes a bulk fault
        # self-evident (#67); it must not present the second case as the first.
        uncompared = (f" [{st.get('uncompared', 0)} not compared]"
                      if st.get("uncompared") else "")
        parts.append(f"{name} {st['changed']}/{st['total']}{unseeded}{uncompared}")
    print("drift by group (changed/checked): " + ", ".join(parts))


class ChangedSource(NamedTuple):
    """One source that drifted, plus the group it came from.

    A tuple with names because it is unpacked in four scopes and one of them writes
    `changed-sources.tsv`, whose four columns a corpus repo reads — positional unpacking
    there means a field added in the middle silently rewrites a public file.
    """
    group: str
    id: str
    url: str
    old: str
    new: str


class SourceOutcome(NamedTuple):
    """WHAT HAPPENED to one in-scope source this run — the record `source-outcomes.json`
    (corpus-toolkit#160) writes one of per source, so that "was this compared" never has to
    be inferred from a source's absence the way `changed-sources.tsv` alone requires.

    `outcome` is ONE OF SIX MUTUALLY EXCLUSIVE STRINGS, chosen the same way the fetch loop
    already branches — this does not re-decide anything, it names the branch that was
    already taken:

    * `"changed"`   — fetched, compared to a recorded baseline, and the hash moved.
    * `"unchanged"` — fetched, compared to a recorded baseline, and it did not move.
    * `"no_baseline"` — fetched successfully, but the manifest recorded no `sha256:` to
      compare it against (ADR 0010: an uncompared source is not a changed source). NOT the
      same set as `changed-sources.tsv`'s empty-`old` rows, which also include a
      no-baseline source whose fetch then failed — see `had_baseline` below for that fact.
    * `"fetch_failed"` — the fetch itself raised.
    * `"unreadable_json"` — a `watch`-declared source returned a body that will not parse.
    * `"watch_path_missing"` — a `watch`-declared source parsed, but a declared path is gone.

    `had_baseline` rides along SEPARATELY from `outcome`, because a source can be both
    unseeded AND fetch-failed — the fetch loop's own comment on `stats["uncompared"]` notes
    exactly this — and collapsing that into a single field would force a choice between two
    true facts. `outcome` says why nothing was compared THIS run; `had_baseline` says
    whether a comparison was even possible had the fetch succeeded.
    """
    group: str
    id: str
    url: str
    outcome: str
    had_baseline: bool


OUTCOMES = ("changed", "unchanged", "no_baseline", "fetch_failed",
            "unreadable_json", "watch_path_missing")
"""The outcome vocabulary, declared once (corpus-toolkit#160).

A tuple rather than six string literals scattered across the builder, the docstrings and the
glossary: `totals` is initialised FROM it, and an outcome outside it raises instead of
inventing a key that a consumer would then read as a real count of zero for the real one.
"""


def _source_outcomes_report(outcomes: list[SourceOutcome], n_total: int,
                            group_filter: list[str] | None, mode: str | None = None) -> dict:
    """The JSON payload `source-outcomes.json` writes (corpus-toolkit#160).

    Three things a consumer of `changed-sources.tsv` alone cannot get, together in one
    place:

    * `sources` — every in-scope source with its outcome, so "was this compared" is a
      lookup rather than an inference from absence. `changed-sources.tsv` only ever lists
      what changed; a source that was fetched and held still, one whose fetch failed, and
      one this run never reached are all equally absent from it.
    * `groups` and `totals` — BOTH counted off `outcomes`, in one pass, so the artifact
      holds ONE tally. The first version shipped `per_group` by reference instead, and that
      dict counts a no-baseline source as CHANGED (`stats["changed"]` fires on `new != old`
      with `old == ""`), while `totals` counted the same source as `no_baseline`. Two
      consumers reading one file got 2 and 4 for the same run — the corpus-toolkit#67
      double-tally shape this docstring claimed to guard against, reproduced inside the
      guard.

    WHAT THE LOG PRINTS IS RECOVERABLE, EXACTLY, AND IS NOT THE SAME NUMBER.
    `_print_group_breakdown`'s `drift by group (changed/checked)` counts an unseeded source
    in its changed figure, so for any group the printed pair is
    `(changed + no_baseline, checked)`. Stated here rather than left for a reader to
    discover from a mismatch, and asserted in `tests/test_drift_reporting.py` so the two
    cannot drift apart silently. The artifact keeps the outcome-based reading because
    `changed` naming two different sets inside one file is the defect, not the fix.

    `groups_in_scope` is the groups that actually yielded an in-scope source, which is not
    always `group_filter` verbatim: a `--group` naming a typo'd group yields none at all,
    and that emptiness is itself the finding (`nothing_checked` in `main`). `group_filter`
    is kept alongside it, unmodified, so a reader can see what was ASKED for as well as what
    was FOUND.

    `mode` names the run that wrote this. A `--record-baseline seed` run writes the artifact
    BEFORE it seeds, so every source in it reads `no_baseline` — true at the moment of
    writing and thoroughly misleading afterwards, with nothing in the file to say the run
    existed to fix exactly that.

    A source outside `group_filter` is simply never in `outcomes` — filtered before this
    function is called, by the same filter `manifest_sources` applies in `main`. It is
    absent from `sources` and uncounted, and that is documented in this module's docstring
    rather than invented as a seventh outcome string: an out-of-scope source was never
    observed this run, which is a different fact from any of the six outcomes, all of which
    describe an observation that was attempted.
    """
    if len(outcomes) != n_total:
        # LOUD, NOT SILENT. Every `continue` in the fetch loop appends exactly one outcome;
        # a future branch that forgets would leave a source uncounted in an artifact whose
        # whole purpose is that no source is silently missing from it.
        raise RuntimeError(
            f"source-outcomes: {len(outcomes)} outcomes for {n_total} in-scope sources — "
            f"a fetch-loop branch appended no outcome")
    totals = {"total": n_total, **{o: 0 for o in OUTCOMES}}
    groups: dict[str, dict] = {}
    for o in outcomes:
        if o.outcome not in totals:
            raise RuntimeError(f"source-outcomes: unknown outcome {o.outcome!r} for {o.id}")
        totals[o.outcome] += 1
        g = groups.setdefault(o.group, {"checked": 0, **{n: 0 for n in OUTCOMES}})
        g["checked"] += 1
        g[o.outcome] += 1
    return {
        "schema_version": 1,
        "mode": mode,
        "group_filter": list(group_filter) if group_filter else None,
        "groups_in_scope": sorted(groups),
        "groups": groups,
        "totals": totals,
        "sources": [{"group": o.group, "id": o.id, "url": o.url, "outcome": o.outcome,
                     "had_baseline": o.had_baseline} for o in outcomes],
    }


def _was_compared(old: str) -> bool:
    """Whether this source was compared to a recorded baseline at all.

    TAKES THE RECORDED BASELINE, not a `ChangedSource`, so the fetch loop can read the rule
    too. It could not before: the loop has `old` in hand and no ChangedSource yet, so
    `source-outcomes.json` re-implemented the test inline and this docstring's claim to be
    the one place stopped being true the moment it did (corpus-toolkit#160 review).

    ADR 0010'S PER-SOURCE RULE, in one place. "An uncompared source is not a changed
    source" is read twice — once to decide whether a ticket may be filed for it
    (corpus-toolkit#145) and once to decide whether it counts towards a group drift
    finding — and the two readings have to be the same reading. They were not: the finding
    filtered on a recorded baseline and the tickets did not, and a source with `sha256:
    ''` therefore filed `Source changed: <id>` with an empty previous hash while being
    excluded from the finding in the same run.

    `old` is the hash the manifest recorded, so an empty one means the fetched bytes were
    compared against nothing. It is a fact about the manifest, not about upstream.
    """
    return bool(old)


class GroupFinding(NamedTuple):
    """One group whose every compared source changed, and the counts behind that claim.

    The compared count RIDES ALONG rather than being looked up again where the issue is
    written: the rule that fired and the number in the body are then the same reading, and
    a finding cannot say `2 of 4` about a group it accepted because 2 of 2 changed.
    """
    group: str
    ids: list[str]
    compared: int
    in_scope: int


def _group_drift_findings(changed: list[ChangedSource],
                          per_group: dict[str, dict]) -> list[GroupFinding]:
    """Every group where EVERY COMPARED source changed, with its ids and compared count.

    The trigger of ADR 0010, and the ONE place it is expressed. `changed` supplies both the
    ids in the finding's body and the count the rule turns on, so there is no second tally
    that could disagree with the first about how many sources drifted.

    100% OF COMPARED SOURCES, because that is the only threshold that is itself an
    observation. ">80% changed" embeds a judgement about how much is a lot, and the sources
    that did NOT change are evidence against the very pattern the finding would assert. All
    three whole-group events on record were N of N.

    MORE THAN ONE compared source: one source cannot corroborate itself, and its own
    `Source changed:` ticket already says everything the finding would. Three ERF groups
    hold exactly one source, and at 1/1 the trigger is trivially true.

    AN UNCOMPARED SOURCE IS NOT A CHANGED SOURCE, which is what `compared` counts and why
    the denominator is not the group total. Unseeded baselines and failed fetches both read
    as 100% to anything counting mismatches alone — oregon-counties reported 3,447 of 3,447
    changed with every baseline empty (corpus-toolkit#68), an inert run a finding would have
    diagnosed with confidence.
    """
    by_group: dict[str, list[str]] = {}
    for c in changed:
        if _was_compared(c.old):
            by_group.setdefault(c.group, []).append(c.id)
    out = []
    for g, ids in sorted(by_group.items()):
        # KeyError, not `.get(g, {}).get("compared", 0)`: a per-group tally that does not
        # carry the count would silently file no finding at all, and "no finding" is what
        # this function says when the drift is genuinely not whole-group. A caller passing
        # a shape this cannot read has to hear about it.
        compared = per_group[g]["compared"]
        if compared > 1 and len(ids) == compared:
            out.append(GroupFinding(g, ids, compared, per_group[g]["total"]))
    # LARGEST FIRST, ties by group name: a finding says "these N changed together", so the
    # one with the most sources behind it leads the DRIFT.md section, and a re-run over the
    # same drift renders the same order (ADR 0010 settles nothing about this order).
    return sorted(out, key=lambda f: (-len(f.ids), f.group))


class RunVerdict(NamedTuple):
    """Why a run is red, in ONE place (ADR 0015).

    Every reason describes a run that did NOT do what a green check says it did: it could
    not compare anything (nothing in scope), its fetches failed at a rate that means an
    outage or a block rather than noise, it was asked to write a baseline and refused, or a
    `watch`-declared source arrived and still could not be compared. Drift itself is a
    signal, not an error, and never appears here. Until v1.34.0 six locals fed one
    `sys.exit` expression and the printed summary, the CI annotation and `--github-output`
    each re-derived the reasons; this is the object all three now read.
    """
    strict_failed: bool
    systemic: bool
    nothing_checked: bool
    refused_write: bool
    watch_failed: int
    unreadable: int

    @property
    def red(self) -> bool:
        return bool(self.strict_failed or self.systemic or self.nothing_checked
                    or self.refused_write or self.watch_failed or self.unreadable)

    def reasons(self) -> list[str]:
        out = []
        if self.strict_failed:
            out.append("a fetch failed under --strict")
        if self.systemic:
            out.append("systemic fetch failure (>20% of fetches failed: an outage or a block)")
        if self.nothing_checked:
            out.append("nothing was in scope, so nothing was checked")
        if self.refused_write:
            out.append("a manifest rewrite was refused")
        if self.watch_failed:
            out.append(f"{self.watch_failed} watched source(s) lack a declared path")
        if self.unreadable:
            out.append(f"{self.unreadable} watched source(s) returned a body that is not json")
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--github-output", help="path to $GITHUB_OUTPUT")
    ap.add_argument("--group", action="append",
                    help="directory-mode only: check just these source group(s) "
                         "(repeatable) — the per-cadence cron's knob")
    ap.add_argument("--check-robots", action="store_true",
                    help="report each source host's robots.txt position and exit without "
                         "fetching anything. REPORTS, never blocks: enforcement is a "
                         "per-corpus policy decision and must not arrive as a surprise "
                         "behaviour change in a toolkit bump (corpus-toolkit#29).")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on ANY fetch failure (the pre-M4 behavior). Default "
                         "tolerates isolated failures: over ~2,000 sources a weekly "
                         "run has a near-certain transient, and one dead fetch "
                         "failing the whole run is how ERF's drift detection ended "
                         "up retired with 813 sources frozen. Systemic failure "
                         "(>20%% of fetches) still exits 1 either way.")
    ap.add_argument("--record-baseline", nargs="?", const="seed", default=None,
                    choices=("seed", "refresh"), metavar="seed|refresh",
                    help="`seed` (the bare form) is what every run already does since "
                         "ADR 0015: a source with no recorded baseline gets one written "
                         "into the manifest, in the working tree, and is compared from "
                         "the next run on. `refresh` ALSO replaces recorded baselines "
                         "with what upstream now serves, which is accepting the observed "
                         "change without re-ingesting — the reconciliation path after "
                         "adding a `volatile_patterns:` entry. Sources whose fetch failed "
                         "are never written.")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    _warn_recheck_is_not_honoured(config)

    # BEFORE the first fetch, and SAID OUT LOUD. A supplement changes which sources this run
    # can reach at all, so a run that used one and did not mention it reads exactly like a
    # run against a host that was never broken -- and that is how an exception outlives its
    # cause. `SupplementRefused` is deliberately not caught: a supplement that cannot be
    # loaded means its sources cannot be fetched, and this file's whole position is that a
    # failed fetch must not pass as an absence of change.
    supplements = configure_chain_supplements(Path(args.config).resolve().parent)
    for host in sorted(supplements):
        print(f"TLS CHAIN SUPPLEMENT: {host} (this host serves an incomplete chain; the "
              f"missing intermediate is supplied, verification is NOT relaxed)")

    if args.check_robots:
        return _report_robots(config, args.group)

    patterns = config.volatile_patterns
    pattern_hits = [0] * len(patterns)
    pattern_bytes = [0] * len(patterns)
    n_normalizable = normalizable_bytes = n_normalizable_in_scope = 0
    changed, failed, watch_failed, unreadable = [], [], [], []
    # One SourceOutcome per in-scope source, appended in the same branch that already
    # decides what happened to it (corpus-toolkit#160) — see SourceOutcome's docstring for
    # the vocabulary. This is `source-outcomes.json`'s per-source list, in detection order.
    outcomes: list[SourceOutcome] = []
    # (group, id) -> how many ENTRIES with that key were not compared. Per entry, because
    # only the fetch loop knows which of two duplicate entries succeeded.
    uncompared_counts: dict[tuple[str, str], int] = {}
    per_group: dict[str, dict] = {}
    fetched: dict[tuple[str, str], str] = {}
    in_scope: set[tuple[str, str]] = set()
    n_total = n_unseeded = 0
    # NAMED, not only counted. Since corpus-toolkit#145 an unseeded source files no ticket,
    # so this list is the whole of what the run says about it and `1 of 3 have no recorded
    # baseline` does not tell an operator which manifest entry to go and seed. In manifest
    # order, so the ids line up with the file the remedy edits.
    unseeded_ids: list[str] = []
    # FILTER FIRST, THEN VALIDATE, THEN FETCH. A mistyped `watch` must stop the run before
    # the first request rather than however many minutes into the crawl the bad source
    # happens to sit -- but only for groups this run was told to check. Validating on yield
    # let one group's typo abort every other group's cron, and `--check-robots` with it.
    manifest_sources = [s for s in iter_manifest_sources(config)
                        if not args.group or s.get("_group") in args.group]
    # GROUPS THIS RUN WAS TOLD TO CHECK (corpus-toolkit#166's pruning rule), derived from
    # the manifest's OWN declared groups rather than from `per_group` below. `per_group`
    # only gains a key when a group has at least one SOURCE, so a group whose last source
    # was just retired — sources: [] — never appears in it, and access-failure pruning
    # read that absence as "this run never touched this group" and held the retired
    # source's entry forever instead of dropping it (caught by
    # `AccessFailureStatePersistenceTest`).
    declared_groups = {g.get("group") or "manifest"
                       for g in config_mod.load_source_manifest_groups(config)}
    checked_groups = declared_groups & set(args.group) if args.group else declared_groups
    validate_watch_declarations(manifest_sources)
    for s in manifest_sources:
        # A `watch:` block on a source whose body a watch list cannot read. `content_hash`
        # takes the watch branch BEFORE the format branch, so `json.loads` met the markup
        # and every run reported `WATCH BODY UNREADABLE ... a fact about the response` --
        # the opposite of what happened, from a manifest that says what the source is.
        #
        # A declared `format:` is believed; otherwise the url's extension decides. Both are
        # declarations. `format: JSON` and `geojson` are accepted, so the check does not
        # turn on the literal string `json`. A url with no extension (a REST endpoint) must
        # declare `format: json` -- the message says so, and guessing on its behalf is how
        # the blocklist version went wrong.
        if s.get("watch"):
            declared = str(s.get("format") or "").strip()
            seen = declared.lower() or _url_extension(s.get("url", ""))
            if seen not in _WATCH_COMPATIBLE_FORMATS:
                shown = (f"`format: {declared}`" if declared
                         else (f"a url ending {seen!r}" if seen
                               else "a url with no extension"))
                raise ValueError(
                    f"source {s.get('id')!r}: `watch` is declared alongside {shown}. A "
                    f"watch list selects paths out of a json document; on anything else "
                    f"the body will not parse, and the run would report that as an "
                    f"unreadable response rather than as this declaration. Drop the "
                    f"`watch` list, or add `format: json` if that is what upstream serves.")
    for s in manifest_sources:
        n_total += 1
        # `or ""`, not a plain get: `sha256:` with no value parses to None, and the
        # CHANGED line's `old[:12]` then dies mid-crawl — on a manifest shape that is
        # exactly what corpus-toolkit#68 is about, i.e. in front of the operator running
        # the documented remedy.
        sid, url = s["id"], s["url"]
        old = str(s.get("sha256") or "").strip()
        gname = s.get("_group", "manifest")
        stats = per_group.setdefault(gname, {"changed": 0, "total": 0, "unseeded": 0,
                                             "uncompared": 0, "compared": 0})
        stats["total"] += 1
        in_scope.add((gname, sid))
        if not old:
            n_unseeded += 1
            stats["unseeded"] += 1
            unseeded_ids.append(sid)
        fmt = _format_for(url, s.get("format"))
        # Counted BEFORE the fetch, so the pattern report can tell "no HTML/XML source was
        # ever in scope" (a fact about the manifest) from "every one of them failed" (a fact
        # about our access). Reporting the second as the first sent an operator to audit a
        # `volatile_patterns:` entry when the finding was that the crawler is blocked.
        if fmt in ("html", "xml") and not s.get("watch"):
            n_normalizable_in_scope += 1
        try:
            raw = fetch(url)
            # `and not s.get("watch")`: a watch source takes the digest branch in
            # `content_hash`, so no pattern was ever applied to its bytes -- but they were
            # added to `normalizable_bytes` anyway, and that is the DENOMINATOR of the >10%
            # breadth warning. Measured: a pattern stripping 39.7% of the one page it
            # processed reported `3.83% of 12544` once a 12 KB watched json body joined the
            # total, demoting the warning to a NOTE. The wider the json body, the safer a
            # content-deleting pattern looks. `format:` need not even say json -- an
            # unrecognised `.json` extension resolves to "html" in `_format_for`.
            if fmt in ("html", "xml") and not s.get("watch"):
                n_normalizable += 1
                normalizable_bytes += len(raw)
                for i, pat in enumerate(patterns):
                    # Each pattern measured against the RAW bytes independently, so an
                    # over-wide one is attributed the whole region it removes even when an
                    # earlier pattern would have taken part of it first. Overlapping
                    # patterns therefore over-count slightly; the alternative attributes a
                    # 90%-of-the-page pattern to whichever one happened to run first.
                    removed = len(raw) - len(pat.sub(b"", raw))
                    if removed:
                        pattern_hits[i] += 1
                        pattern_bytes[i] += removed
            new = content_hash(raw, fmt, patterns, watch=s.get("watch"))
        except WatchedDocumentUnreadable as e:
            # Caught BEFORE its parent. A 200 carrying an error page and a watched field
            # disappearing are different findings with different remedies, and calling the
            # first one the second sends the operator to audit a `watch` list that is fine.
            unreadable.append(sid)
            stats["uncompared"] += 1
            uncompared_counts[(gname, sid)] = uncompared_counts.get((gname, sid), 0) + 1
            outcomes.append(SourceOutcome(gname, sid, url, "unreadable_json", bool(old)))
            print(f"WATCH BODY UNREADABLE {sid}: {url} ({e})", file=sys.stderr)
            continue
        except WatchedPathMissing as e:
            # NOT A FETCH FAILURE, and kept out of `failed` for that reason. The bytes
            # arrived; the document does not have the shape this source declared it
            # watches. Counting it as a failed fetch put it under "a fact about our access,
            # not about upstream" -- the exact inverse of what it is -- and fed it into the
            # >20% SYSTEMIC threshold, so a schema change upstream could raise the one
            # alarm that means "our crawler is blocked" (corpus-toolkit#72).
            #
            # stderr, not stdout, and annotated: this is the most actionable thing the run
            # can find. A watched field disappearing is precisely what a `watch` list is
            # for, and the first version whispered it into a 3,900-line log and exited 0.
            watch_failed.append(sid)
            stats["uncompared"] += 1
            uncompared_counts[(gname, sid)] = uncompared_counts.get((gname, sid), 0) + 1
            outcomes.append(SourceOutcome(gname, sid, url, "watch_path_missing", bool(old)))
            print(f"WATCH PATH MISSING {sid}: {url} ({e})", file=sys.stderr)
            continue
        except Exception as e:
            failed.append(sid)
            # Counted here for the same reason as the two above: this source was NOT
            # compared, and without the marker the group line renders `blocked 0/2`,
            # indistinguishable from a group compared in full and found stable. That is the
            # case corpus-toolkit#67 added this line to expose -- ERF's DEQ group sat at
            # 52/52 off a broken fetch -- so it predates `watch` and is fixed alongside it.
            stats["uncompared"] += 1
            uncompared_counts[(gname, sid)] = uncompared_counts.get((gname, sid), 0) + 1
            outcomes.append(SourceOutcome(gname, sid, url, "fetch_failed", bool(old)))
            print(f"FETCH FAILED {sid}: {url} ({e})")
            continue
        fetched[(gname, sid)] = new
        # COUNTED HERE, not derived as `total - unseeded - uncompared`, because a source
        # can be BOTH — an unseeded entry whose fetch then failed increments both markers,
        # and the subtraction removes it twice. This is the one line where "was compared to
        # a recorded baseline" is a fact rather than an inference: bytes arrived, hashing
        # succeeded, and there was something to compare them against.
        if old:
            stats["compared"] += 1
        if new != old:
            # The group rides along because the issue budget is allocated by group
            # (corpus-toolkit#69) and the spend loop is far from the only scope that knows
            # `gname`. It is NOT written to the tsv: that file is read by corpus repos, so
            # its four columns are public surface.
            changed.append(ChangedSource(gname, sid, url, old, new))
            stats["changed"] += 1
            # An unseeded source is about to be SEEDED, not reported as drift (ADR 0010,
            # 0015); printing CHANGED for it is the misreading this module exists to refuse.
            if old:
                print(f"CHANGED  {sid}: {old[:12]}… -> {new[:12]}…")
            else:
                print(f"SEEDING  {sid}: (no baseline) -> {new[:12]}…")
        # `old` decides the outcome BEFORE `new != old` does. An unseeded source (`old ==
        # ""`) compares unequal to everything it fetches, by construction — that is
        # corpus-toolkit#145's bug, not a genuine comparison — so it is reported
        # `"no_baseline"` here even though it just fed `changed` above. ADR 0010's rule
        # ("an uncompared source is not a changed source") applies to this artifact's
        # outcome exactly as it already applies to tickets and group findings; it does not
        # touch `changed-sources.tsv`, which keeps its own long-documented meaning.
        outcomes.append(SourceOutcome(
            gname, sid, url,
            ("changed" if new != old else "unchanged") if _was_compared(old)
            else "no_baseline",
            _was_compared(old)))

    out = config.root / "changed-sources.tsv"
    # Detection order, i.e. manifest order — unchanged by #69's reordering, which applies
    # to the capped issue spend only. The tsv is not capped, so its order carries no
    # priority meaning and re-sorting it would churn a file consumers diff.
    out.write_text("".join(f"{c.id}\t{c.url}\t{c.old}\t{c.new}\n" for c in changed))

    # THE COMPANION ARTIFACT (corpus-toolkit#160), written unconditionally and BESIDE the
    # tsv above rather than instead of it — `changed-sources.tsv` keeps its name, its four
    # columns, its rows and their order, byte-for-byte, for the same inputs. Written here,
    # not gated behind `--open-issues` or `args.record_baseline`, because a run that failed
    # every fetch is exactly the run this artifact exists for (a wholly-failed run and a
    # clean run write the SAME empty tsv today — that collapse is the bug). Not written
    # only when `--check-robots` returns before any source is ever fetched, since there are
    # no observations yet to report.
    (config.root / "source-outcomes.json").write_text(
        json.dumps(_source_outcomes_report(
            outcomes, n_total, args.group,
            mode=f"record-baseline:{args.record_baseline}" if args.record_baseline else None),
                   indent=2, sort_keys=True) + "\n")

    # ACCESS-FAILURE STATE (corpus-toolkit#166), written unconditionally right beside
    # `source-outcomes.json` for the same reason that one is: a run that failed every
    # fetch is exactly the run whose streaks must not be lost. `checked_groups` is every
    # group this run was told to check (all of them, or `--group`'s own set) — a group
    # outside that was never touched this run and its existing entries must be HELD, not
    # cleared (see `_update_access_failures`).
    prior_access_state = _load_access_failures(config)
    access_state = _update_access_failures(prior_access_state, outcomes, in_scope,
                                           checked_groups, _utcnow_date(), declared_groups)
    _write_access_failures(config, access_state)

    # SEEDING IS THE DEFAULT (ADR 0015). A source with no recorded baseline cannot be
    # compared, and until v1.34.0 recording one was a human command that appeared in no
    # workflow — three corpora ran inert for weeks, every source "changed" against ``, exit
    # 0 (corpus-toolkit#68, #145). The run that first fetches a source now records what it
    # fetched, says so in DRIFT.md, and compares from the next run on; the manifest edit
    # rides the same PR as the rest of the run's state. `refresh` is the one deliberate act
    # left — accepting an observed change as the new baseline — and it is still a flag.
    mode = "refresh" if args.record_baseline == "refresh" else "seed"
    recorded = None
    # Also when seed mode was ASKED for on a fully seeded manifest: the recorder is what
    # names the drifted sources seed mode leaves alone and the refresh that accepts them.
    if n_unseeded or args.record_baseline:
        recorded = _record_baselines(config, fetched, in_scope, mode,
                                     uncompared=uncompared_counts)
    unseeded_set = set(unseeded_ids)
    seeded_keys: set[tuple[str, str]] = set()
    accepted_keys: set[tuple[str, str]] = set()
    if recorded:
        written = set(recorded["written_ids"])
        for key in in_scope:
            if key[1] in written:
                (seeded_keys if key[1] in unseeded_set else accepted_keys).add(key)

    # A COMPARED source that moved. `changed` also holds unseeded sources (for the tsv's
    # sake); this is the list every count of drift below reads (ADR 0010).
    drifted = [c for c in changed if _was_compared(c.old)]
    # A run that checked NOTHING — an empty group filter, a typo'd `--group`, a manifest
    # whose `sources:` is empty — is not a clean run. `--group nosuchgroup` used to print
    # "0 changed ... of 0 checked" and exit 0: could-not-check reported as not-there.
    nothing_checked = n_total == 0

    if args.github_output:
        # `changed` drives whatever the calling workflow does next; an unseeded source is
        # not a finding, so `drifted` and not `changed`. `seeded` rides along so a workflow
        # can react to the run having written baselines.
        with open(args.github_output, "a") as f:
            f.write(f"changed={'true' if drifted else 'false'}\n")
            f.write(f"unseeded={n_unseeded}\n")
            f.write(f"seeded={len(seeded_keys)}\n")

    # ONE DEFINITION of "not compared", used on every line that says it: in scope and never
    # compared to a baseline. Three adjacent lines once carried three different sets.
    not_compared = len(failed) + len(watch_failed) + len(unreadable)
    why = ", ".join(filter(None, [
        f"{len(failed)} fetch failed" if failed else "",
        f"{len(watch_failed)} watched path missing" if watch_failed else "",
        f"{len(unreadable)} body not parseable as json" if unreadable else ""]))
    uncompared = f"{not_compared} not compared ({why}), " if not_compared else ""
    print(f"\n{len(drifted)} changed, {len(failed)} fetch failure(s), {uncompared}"
          f"{n_unseeded} with no recorded baseline, of {n_total} checked.")
    _print_group_breakdown(per_group)
    if recorded is not None:
        print(f"{recorded['written']} baseline(s) written ({len(seeded_keys)} seeded, "
              f"{len(accepted_keys)} accepted), {recorded['already_current']} already "
              f"current, {recorded['left_alone']} left alone, {recorded['failed_fetch']} "
              f"skipped (not compared).")
        if recorded["files"]:
            print("manifest file(s) rewritten in the working tree: "
                  + ", ".join(str(p) for p in recorded["files"]))
        if recorded["left_alone"] and mode == "seed":
            shown = ", ".join(recorded["left_alone_ids"][:10])
            print(f"{recorded['left_alone']} source(s) already carry a recorded baseline "
                  f"that no longer matches upstream — that is drift, listed in DRIFT.md, and "
                  f"seed mode does not overwrite a curated value. Re-run with "
                  f"`--record-baseline=refresh` to accept it. ({shown}"
                  f"{'…' if recorded['left_alone'] > 10 else ''})")
        for path, sid, why_refused in recorded["refused"]:
            # `why_refused` states what was and was not written FOR THAT BRANCH.
            print(f"REFUSED to record {sid} in {path}: {why_refused}", file=sys.stderr)
        if recorded["refused"]:
            _annotate("Baseline recording refused",
                      f"{len(recorded['refused'])} manifest entr(ies) could not be "
                      f"recorded; this run exits non-zero.")
    if seeded_keys:
        shown_seeded = ", ".join(sorted(k[1] for k in seeded_keys)[:20])
        if len(seeded_keys) > 20:
            shown_seeded += f" (first 20 of {len(seeded_keys)}; all of them are in DRIFT.md)"
        # NOT an annotation: this is the remedy happening, not a defect to investigate.
        print(f"{len(seeded_keys)} source(s) had no recorded baseline and were SEEDED this "
              f"run: recorded what upstream served, compared nothing. They compare from the "
              f"next run on (ADR 0015): {shown_seeded}.", file=sys.stderr)
    findings = _group_drift_findings(changed, per_group)
    if findings:
        print(f"{len(findings)} group(s) where EVERY compared source changed: "
              + ", ".join(f"{f.group} ({len(f.ids)} of {f.compared})" for f in findings)
              + ". Reported in DRIFT.md: they changed together, and this says nothing about "
                "why (ADR 0010).")
    # Over the WHOLE access state, including HELD entries from groups this run never
    # touched — the elapsed-days arm's whole point (see `_access_failure_escalations`).
    escalations = _access_failure_escalations(access_state, _utcnow_date())
    if escalations:
        print(f"{len(escalations)} access failure(s) past {ACCESS_FAILURE_ESCALATE_RUNS} "
              f"consecutive failed runs or {ACCESS_FAILURE_ESCALATE_DAYS} elapsed days "
              f"(ADR 0013), marked escalated in DRIFT.md: "
              + ", ".join(e.id for e in escalations[:20])
              + ("…" if len(escalations) > 20 else "")
              + ". This is a fact about our access, never about upstream.", file=sys.stderr)
    for pat, hits, removed in zip(patterns, pattern_hits, pattern_bytes):
        shown = pat.pattern.decode("utf-8", "replace")
        share = removed / normalizable_bytes if normalizable_bytes else 0.0
        if not n_normalizable:
            # ZERO SOURCES MEASURED IS ITSELF THE FINDING, and this used to `break` and say
            # nothing. `content_hash` permits `volatile_patterns` alongside json sources on
            # the grounds that "a pattern that matches nothing anywhere is already named in
            # the drift report, per run" -- so the report going silent is that argument's
            # own premise failing. Reachable now that watch sources are (rightly) out of the
            # denominator: a corpus with no non-watch HTML source left hits it.
            if n_normalizable_in_scope:
                print(f"NOTE: volatile pattern {shown!r} was measured against no source "
                      f"this run — all {n_normalizable_in_scope} HTML/XML source(s) in "
                      f"scope could not be fetched. This says nothing about the pattern; "
                      f"the finding is the failed fetches.", file=sys.stderr)
            else:
                print(f"NOTE: volatile pattern {shown!r} was measured against NO source "
                      f"this run — nothing in scope reaches the HTML/XML path, so this "
                      f"says nothing about whether the pattern works. It is configured "
                      f"and untested.", file=sys.stderr)
        elif not hits:
            # A declared pattern that matches nothing is indistinguishable from the empty
            # built-in list it replaced — configured, and doing nothing (corpus-toolkit#66).
            print(f"NOTE: volatile pattern {shown!r} matched none of the "
                  f"{n_normalizable} HTML/XML source(s) fetched — it is configured and "
                  f"doing nothing.", file=sys.stderr)
        elif share >= VOLATILE_BREADTH_WARN:
            # The opposite failure, and the worse one: a pattern wide enough to swallow
            # the content. Everything it removes is invisible to hashing forever, so drift
            # inside it can never be detected and the run reports 0 changed regardless.
            print(f"WARNING: volatile pattern {shown!r} removed {removed} byte(s), "
                  f"{share:.1%} of the {normalizable_bytes} byte(s) fetched on the "
                  f"HTML/XML path, across {hits} of {n_normalizable} source(s). A pattern "
                  f"this wide deletes CONTENT before hashing: two versions differing only "
                  f"inside what it strips hash identically, so those documents can never "
                  f"report drift again. Reported, not refused — narrow it, or record in "
                  f"the PR why this much of the page is genuinely volatile.",
                  file=sys.stderr)
            _annotate("Volatile pattern removes a large share of each page",
                      f"{shown} removed {share:.1%} of fetched HTML/XML bytes; drift "
                      f"inside that region can no longer be detected.")
        else:
            print(f"NOTE: volatile pattern {shown!r} matched {hits} of {n_normalizable} "
                  f"HTML/XML source(s), removing {removed} byte(s) ({share:.2%} of "
                  f"{normalizable_bytes}).", file=sys.stderr)
    if unreadable:
        print(f"{len(unreadable)} source(s) declaring `watch` returned a body that is not "
              f"parseable json, so they were NOT compared: "
              + ", ".join(unreadable[:20]) + ("…" if len(unreadable) > 20 else "")
              + ". This is a fact about the response — an error page served with a 200, or "
                "a block — not about the `watch` list.", file=sys.stderr)
        _annotate("Watched sources returned unreadable bodies",
                  f"{len(unreadable)} source(s) were not compared because the response is "
                  f"not parseable json: " + ", ".join(unreadable[:20]))
    if watch_failed:
        # Its own line, above the fetch failures, because the remedy is different: a fetch
        # failure is chased with the network, this is chased with the source's `watch` list
        # against what upstream now serves.
        print(f"{len(watch_failed)} source(s) declared a `watch` path the document does "
              f"not contain, so they were NOT compared: "
              + ", ".join(watch_failed[:20]) + ("…" if len(watch_failed) > 20 else "")
              + ". Either upstream changed shape — worth knowing, and the reason this is "
                "reported rather than hashed as empty — or the path is wrong.",
              file=sys.stderr)
        _annotate("Watched paths missing",
                  f"{len(watch_failed)} source(s) were not compared because a declared "
                  f"`watch` path is absent from the fetched document: "
                  + ", ".join(watch_failed[:20]))
    if failed:
        print("failed sources (a fact about our access, not about upstream): "
              + ", ".join(failed[:20]) + ("…" if len(failed) > 20 else ""))
    systemic = n_total and len(failed) / n_total > 0.20
    if systemic:
        print(f"SYSTEMIC: {len(failed)}/{n_total} fetches failed — this is an "
              f"outage or a block, not noise.", file=sys.stderr)
    if nothing_checked:
        scope = f" matching --group {', '.join(args.group)}" if args.group else ""
        print(f"NOTHING WAS CHECKED: the manifest yielded 0 in-scope source(s){scope}. "
              f"A run that checked nothing is not a clean run — check the group name "
              f"against the breakdown a full run prints, or the manifest's `sources:`.",
              file=sys.stderr)
        _annotate("Drift run checked nothing",
                  f"0 sources were in scope{scope}; nothing was fetched or compared.")
    # THE VERDICT, in one object (ADR 0015). Each reason describes a run that did not do
    # what a green check says it did; drift is a signal, not an error, and is not here.
    verdict = RunVerdict(strict_failed=bool(failed and args.strict), systemic=bool(systemic),
                         nothing_checked=nothing_checked,
                         refused_write=bool(recorded and recorded["refused"]),
                         watch_failed=len(watch_failed), unreadable=len(unreadable))

    # THE ROLLING VIEW (ADR 0015): last run's state with every in-scope observation
    # replaced, held for groups this run did not check, pruned for sources retired from
    # groups it did — the rules `access-failures.json` already follows — then rendered.
    today = _utcnow_date()
    state = drift_report.update_drift_state(
        drift_report.load_drift_state(config.root), outcomes, changed,
        seeded=seeded_keys, accepted=accepted_keys, in_scope=in_scope,
        checked_groups=checked_groups, declared_groups=declared_groups, today=today)
    last_run = {
        "date": today.isoformat(),
        "toolkit_version": drift_report._toolkit_version(),
        "mode": mode,
        "group_filter": list(args.group) if args.group else None,
        "groups_in_scope": sorted(per_group),
        "red_reasons": verdict.reasons(),
        "totals": {"total": n_total, "changed": len(drifted),
                   "unchanged": sum(1 for o in outcomes if o.outcome == "unchanged"),
                   "seeded": len(seeded_keys), "accepted": len(accepted_keys),
                   "fetch_failed": len(failed), "unreadable_json": len(unreadable),
                   "watch_path_missing": len(watch_failed)},
    }
    drift_report.write_drift_state(config.root, state, last_run)
    drift_report.write_report(config.root, drift_report.render_drift_md(
        state, access_state, escalations, last_run,
        escalate_runs=ACCESS_FAILURE_ESCALATE_RUNS, escalate_days=ACCESS_FAILURE_ESCALATE_DAYS,
        today=today))
    print(f"DRIFT.md: {sum(1 for r in state.values() if r.outcome == 'changed')} source(s) "
          f"changed since baseline across the corpus, {len(access_state)} on a fetch-failure "
          f"streak ({len(escalations)} escalated).")
    if verdict.red:
        print("THIS RUN IS RED — it could not do its job: " + "; ".join(verdict.reasons()),
              file=sys.stderr)
    sys.exit(1 if verdict.red else 0)


if __name__ == "__main__":
    main()
