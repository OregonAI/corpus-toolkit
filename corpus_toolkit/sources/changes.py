#!/usr/bin/env python3
"""corpus-detect-changes — re-fetch every source declared in the corpus's
source manifest, diff content hashes, write changed-sources.tsv, optionally
open a GitHub issue per drifted source — plus, for a group in which EVERY
COMPARED source changed, one `Group drifted:` finding that says they changed
together and nothing about why (ADR 0010). Ported from
oregon-policy-repo/src/detect_changes.py; the Oregon-specific SharePoint-
listing diff (`check_sp_listing`) is NOT ported — it re-queries a specific
vendor's list-view API and doesn't generalize. A corpus that needs it keeps
that check as its own local script (it can still import
`corpus_toolkit.repo.content_hash` etc.) and runs it alongside this one.

Manifest shape (`_meta/corpus.yml`'s `source_manifest_path`, a file or a
directory of group files — see `corpus_toolkit.config.load_source_manifest_groups`):
  sources:
    - id: some-doc-id
      url: https://...
      sha256: <content_hash of the source as of the last review>
      format: html   # optional; inferred from the URL's extension otherwise
      watch:         # optional, json sources only (corpus-toolkit#72): hash ONLY these
        - rowsUpdatedAt          # paths, so vendor counters that move on their own are
        - columns[].name         # inert by construction. Absent = hash the whole document.

THE BASELINE IS WRITTEN BY THIS TOOL, under `--record-baseline` (corpus-toolkit#68).
It used to be written by nothing at all: the docstring said "recorded at last
ingest/refresh" and delegated the job to a per-corpus ingester the toolkit neither
ships nor checks for, so oregon-counties (3,447 sources) and oregon-kpm (789) ran
their whole lifetimes with `sha256: ''` everywhere — every source CHANGED every run,
25 spurious issues, the rest dropped, exit 0.

Do NOT seed it from a document's frontmatter `source_sha256`. That is a different
hash over different input (`hash_snapshot`, over committed `.txt`), and the two agree
only for image-only scans where both fall back to raw bytes — so a corpus that seeds
from frontmatter and spot-checks a scan sees a clean result and gets permanent drift
on every text-layer PDF, now with a populated "previous" hash that reads as a genuine
upstream change.

AN UNSEEDED SOURCE FILES NO TICKET (corpus-toolkit#145). `sha256: ''` compares unequal to
everything, so such a source reported CHANGED on every run and used to file
`Source changed: <id>` with an EMPTY previous hash — a drift report about a comparison
that never happened — and to spend a slot of the issue budget doing it, pushing genuine
drift out of a capped run. ADR 0010's rule that an uncompared source is not a changed
source held for the group finding and not for the tickets; it now holds for both. The
source is still counted, still in the group breakdown and still in `changed-sources.tsv`
with an empty `old` column. What replaces the ticket is a stderr line NAMING those ids
and `--record-baseline`, a CI annotation, and a non-zero exit — because removing the
tickets and saying nothing more would report could-not-check as nothing-to-report, which
is the same rule broken the other way round.

THREE ARTIFACTS, EACH WITH ITS OWN MEANING, ALL PUBLIC SURFACE the moment this runs inside
a corpus repo (AGENTS.md: "anything reachable from a corpus repo is public, whether or not
this repo calls it"). All three are written on every run that reaches the fetch loop —
including a run that fails every fetch, which is the run each of them exists for — and
none is written when `--check-robots` returns before fetching anything.

* `changed-sources.tsv`, at the corpus root. Four tab-separated columns —
  `id, url, old_sha256, new_sha256` — one row per source whose hash MOVED, in manifest
  (detection) order. STABLE SINCE ITS INTRODUCTION and pinned by test
  (`ChangedSourcesTsvIsPublicSurfaceTest`,
  `ChangedSourcesTsvByteIdenticalGuaranteeTest`): a corpus repo may keep reading it
  positionally. A source that was compared and held still, one whose fetch failed, and
  one out of this run's `--group` scope are ALL absent from it, indistinguishably — this
  file only ever answers "what changed", never "what happened to everything else"
  (corpus-toolkit#160).
* `source-outcomes.json`, at the corpus root, new in corpus-toolkit#160 — the companion
  that answers what the tsv structurally cannot. One `SourceOutcome` per source this run
  had in scope (`changed`, `unchanged`, `no_baseline`, `fetch_failed`,
  `unreadable_json`, or `watch_path_missing` — see `SourceOutcome`'s docstring for what
  each means and why they do not collapse into each other), plus the per-group breakdown
  and run totals that otherwise exist only on stdout. A source outside `--group` scope is
  simply absent from it, same as from the tsv, and that absence is documented rather than
  reported as a seventh outcome (`_source_outcomes_report`'s docstring). STABILITY: the
  top-level shape (`schema_version`, `mode`, `group_filter`, `groups_in_scope`, `groups`,
  `totals`, `sources`), the six outcome strings, and the keys of a `groups` entry
  (`checked` plus one per outcome) are the contract; `schema_version` bumps on a breaking
  change to any of them. `groups` is the artifact's OWN tally, counted off the same
  per-source records as `totals` so one file never holds two readings of `changed`; the
  log's `drift by group (changed/checked)` counts an unseeded source as changed, so the
  printed pair is `(changed + no_baseline, checked)` and is recoverable exactly. Additive fields on a `groups` entry or a `sources`
  entry are NOT breaking, the same convention `docs/mcp-interface-contract.md` uses for
  the MCP tool responses this toolkit also serves.
* `access-failures.json`, at the corpus root, new in corpus-toolkit#166 — the ONE artifact
  here that is not a per-run snapshot: it carries `{(group, id): consecutive fetch-failure
  count}` FORWARD across separate invocations, so a source that fails every scheduled run
  can be told apart from one that failed once, which `source-outcomes.json` alone cannot do
  (it is rebuilt from nothing every run). Read at the start of the fetch loop, folded with
  this run's own outcomes (`_update_access_failure_streaks`: `fetch_failed` increments, any
  other outcome — the fetch succeeded — resets), and written back before `--open-issues`
  ever runs. A source outside this run's `--group` scope keeps whatever streak was already
  recorded for it, untouched — see that function's docstring for why zeroing it would be
  "could not check" wearing the shape of "is fine now" (CONTEXT.md's outranking rule). At
  `ACCESS_FAILURE_STREAK_THRESHOLD` (currently 3) consecutive failures, `--open-issues`
  files ONE `Source access failing: <id>` ticket, out of the SAME `MAX_ISSUES_PER_RUN`
  budget the drift tickets and group findings already share — see
  `ACCESS_FAILURE_STREAK_THRESHOLD`'s docstring for why a run count, not a calendar window.
  UNLIKE the manifest, this is not curated data a human reviews — it is machine bookkeeping,
  the same kind STATUS.md already is, and it needs the SAME kind of commit step in the
  calling workflow to survive an ephemeral runner (corpus-toolkit's own
  `detect-upstream-changes.yml` gained one alongside this change). A workflow that never
  commits it degrades safely: the streak restarts at zero, which only delays an escalation,
  never invents a false one.

Exit codes: 0 on a run that could actually detect drift. 1 when a fetch fails under
`--strict`, when failures are systemic (>20%), when the issue cap truncated the report
(a capped run is not a clean run), when ANY in-scope source has no recorded baseline (it
was not compared, and outside `--record-baseline` it never will be until somebody seeds
it), when the scope came out empty so nothing was checked, when `--record-baseline` refused
a rewrite it could not account for, and — regardless of `--strict` — when a source declaring
`watch` returned a document missing a declared path or one that will not parse as json.
Those two are unconditional because the bytes ARRIVED: this is not upstream being briefly
unreachable, and the source stays uncompared on every run until somebody looks. The common
thread: a run that could not do the thing it reports on must not report success. A changed
source is still a signal, not an error.
"""
import argparse
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from pathlib import Path
from typing import NamedTuple

import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit.config import (iter_manifest_sources,
                                   validate_watch_declarations)
from corpus_toolkit.repo import (WatchedDocumentUnreadable, WatchedPathMissing,
                                 content_hash)
from corpus_toolkit.sources import tls

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

ISSUE_LABEL = "source-change"

# Opening more issues than this in one run is not reporting, it is a stampede. A drift
# run that wants hundreds of issues is almost always describing a broken BASELINE -- a
# manifest whose `sha256` values were never recorded compares unequal to everything, so
# every source reads as changed, forever. Capping and saying so surfaces that; opening
# 618 issues buries it. See OregonAI/oregon-collective-bargaining#14 for the case that
# motivated this.
#
# HOW MANY, not which: `_tickets_in_spend_order` decides which sources the budget buys
# (corpus-toolkit#69). The two are separate on purpose — the cap is the guardrail and does
# not move, while the allocation is a policy about whose drift is worth a ticket first.
MAX_ISSUES_PER_RUN = 25

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


def _ensure_label() -> bool:
    """Create the label if absent. Returns False if we cannot.

    `gh issue create --label X` FAILS when X does not exist, and the label is not part
    of any repo template -- so a corpus that never hand-created it got silent no-op
    reporting the first time drift fired, which is exactly what happened
    (corpus-toolkit#53).
    """
    r = subprocess.run(["gh", "label", "create", ISSUE_LABEL, "--force",
                        "--color", "D93F0B",
                        "--description", "Upstream source content changed since last ingest"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"WARNING: could not ensure the {ISSUE_LABEL!r} label exists "
              f"({r.stderr.strip()[:200]}). Issue creation will likely fail.",
              file=sys.stderr)
        return False
    return True


def _file_once(title: str, body: str, subject: str) -> bool:
    """Open one issue unless one carrying this exact title is already open.

    RETURN VALUES ARE CHECKED HERE, unlike the version this replaces, which called
    `subprocess.run` bare and discarded every failure. That is how 618 consecutive
    creation failures produced a log reading `618 changed, 58 fetch failure(s)` and no
    other sign -- the summary counts what DRIFTED, and an operator reasonably reads it
    as what was REPORTED (corpus-toolkit#53).

    ONE implementation for the per-source ticket and the group drift finding, because
    dedup and the title are the same mechanism: the search is `in:title "<title>"`, so
    the caller's title is the identity of the condition. A title that moves while the
    condition persists -- a count in it -- files a fresh issue every run (ADR 0010).
    """
    if not shutil.which("gh"):
        print(f"NOTE: 'gh' not on PATH — skipping issue creation for {subject}",
              file=sys.stderr)
        return False
    existing = subprocess.run(
        ["gh", "issue", "list", "--label", ISSUE_LABEL, "--state", "open",
         "--search", f'in:title "{title}"', "--json", "number", "--jq", "length"],
        capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() not in ("", "0"):
        print(f"Issue already open for {subject}, skipping")
        return True
    r = subprocess.run(["gh", "issue", "create", "--label", ISSUE_LABEL,
                        "--title", title, "--body", body],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"FAILED to open issue for {subject}: {r.stderr.strip()[:300]}",
              file=sys.stderr)
        return False
    return True


def _open_issue(source_id, url, old, new) -> bool:
    """Open one drift issue. Returns True only if an issue now exists for this source."""
    body = (f"Automated detection.\n\n- **Document id**: {source_id}\n"
            f"- **Source URL**: {url}\n- **Previous sha256**: {old}\n"
            f"- **New sha256**: {new}\n")
    return _file_once(f"Source changed: {source_id}", body, source_id)


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
_ID_RE = re.compile(r"^(?P<lead>[ \t]*(?:-[ \t]+)?)id:[ \t]*"
                    r"(?P<value>'[^']*'|\"[^\"]*\"|[^#\n]*?)[ \t]*(?:#.*)?$")
_SHA_RE = re.compile(r"^(?P<lead>[ \t]*(?:-[ \t]+)?)sha256:(?P<sp>[ \t]*)"
                     r"(?P<value>'[^']*'|\"[^\"]*\"|[^#\n]*?)"
                     r"(?P<gap>[ \t]*)(?P<comment>#[^\n]*)?(?P<eol>\r?\n?)$")


def _scalar(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _plan_sha_edits(lines: list[str], wanted: set[str]) -> dict[str, list]:
    """{source id: [id line index, sha256 line index or None, key column]}.

    Only ids in `wanted`, and only their FIRST occurrence — an id that appears twice is
    dropped by the caller before we get here, because guessing which entry a hash belongs
    to is how a manifest acquires a baseline that is wrong for a source nobody re-examines.
    """
    plan: dict[str, list] = {}
    cur = None
    # `sha256:` lines seen since the current entry began, as (index, lead length). An entry
    # may write `sha256:` ABOVE `id:`, and scanning forward from the id line alone never
    # claimed those -- `_rewrite_sha256` then concluded the entry had none and INSERTED a
    # second one, which both verification guards accept: PyYAML resolves duplicate keys
    # last-wins so the re-parse reads back the inserted value, and the line diff sees one
    # added line carrying a wanted value. The run reported success and left a stale key in a
    # curated file (corpus-toolkit#119).
    pending: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            cur = None                      # a top-level key ends the entry
            pending = []
        elif stripped.startswith("- "):
            # A `- ` INSIDE the entry is not a new entry. This reset was unconditional,
            # which was safe only while no source key held a block sequence -- `watch:`
            # (corpus-toolkit#72) is the first, and with it written above `sha256:` the sha
            # line was never associated with its id: a second `sha256:` was inserted after
            # `id:`, the re-parse check saw the old trailing value win, and the whole group
            # file was refused, taking every other source in it down too. That is the
            # adoption path MIGRATION.md prescribes, broken by the feature it adopts.
            #
            # Compared against the entry's own key column, so a sibling `- id:` (marker to
            # the LEFT of that column) still ends it -- writing a's hash into b's line is
            # the wrong-entry write this function refuses by name.
            marker = len(line) - len(line.lstrip())
            if cur is None or marker < plan[cur][2]:
                cur = None
                # A NEW ENTRY STARTS HERE, so anything seen above belongs to the previous
                # one. Keeping it would let a `sha256:` from the entry before be claimed by
                # this one -- the wrong-entry write, from the other direction.
                pending = []
        m = _ID_RE.match(line)
        if m:
            sid = _scalar(m.group("value"))
            cur = sid if sid in wanted and sid not in plan else None
            if cur is not None:
                col = len(m.group("lead"))
                plan[cur] = [i, None, col]
                # AT THE ENTRY'S OWN KEY COLUMN, exactly as the forward scan requires.
                # Without the column test this claims the first `sha256:` above `id:` at any
                # depth, so an entry whose `attachments:` list carries per-file digests above
                # its own id gets the source's hash written into the attachment.
                for idx, lead in pending:
                    if lead == col:
                        plan[cur][1] = idx
                        break
            continue
        m = _SHA_RE.match(line)
        # AT THE ENTRY'S OWN KEY COLUMN, exactly. Relaxing the `- ` reset above so a nested
        # block sequence no longer ends the entry also let the first `sha256:` INSIDE that
        # sequence be claimed as the entry's own -- an `attachments:` list with per-file
        # digests had the source's hash written into the attachment, the re-parse check
        # failed, and the group file was refused with nothing written. The entry's own keys
        # are at `plan[cur][2]`; anything deeper belongs to something else.
        if m:
            if (cur is not None and plan[cur][1] is None
                    and len(m.group("lead")) == plan[cur][2]):
                plan[cur][1] = i
            elif cur is None:
                # Above an `id:` we have not reached yet. Recorded with its column so the
                # id line can claim it only if it is that entry's own key.
                pending.append((i, len(m.group("lead"))))
    return plan


def _rewrite_sha256(text: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    """Set each id's `sha256` in place. Returns (new text, ids that could not be located)."""
    lines = text.splitlines(keepends=True)
    plan = _plan_sha_edits(lines, set(updates))
    # An inserted key takes the file's OWN line ending, not this platform's: a CRLF
    # manifest that grows one LF line is a mixed-ending file, which every later diff shows.
    nl = "\r\n" if "\r\n" in text else "\n"
    inserts: dict[int, list[str]] = {}
    for sid, (id_i, sha_i, col) in plan.items():
        value = updates[sid]
        if sha_i is not None:
            m = _SHA_RE.match(lines[sha_i])
            lines[sha_i] = (f"{m.group('lead')}sha256:{m.group('sp') or ' '}\"{value}\""
                            f"{m.group('gap')}{m.group('comment') or ''}"
                            f"{m.group('eol') or nl}")
        else:
            # No `sha256:` key at all: insert one immediately after `id:`, at the same
            # indentation. Appending at the end of the entry instead would land inside
            # whatever nested block happened to come last.
            if not lines[id_i].endswith("\n"):
                lines[id_i] += nl
            inserts.setdefault(id_i, []).append(f"{' ' * col}sha256: \"{value}\"{nl}")
    if inserts:
        rebuilt: list[str] = []
        for i, line in enumerate(lines):
            rebuilt.append(line)
            rebuilt.extend(inserts.get(i, ()))
        lines = rebuilt
    return "".join(lines), sorted(set(updates) - set(plan))


def _record_baselines(config, fetched: dict, in_scope: set, mode: str,
                      uncompared: dict | None = None) -> dict:
    """Write freshly computed hashes into the manifest group files. Returns a tally.

    THE MANIFEST IS CURATED DATA a human reviews in a PR, and every rule here follows from
    that:

    * It is written only under `--record-baseline`, never as a side effect of a drift run.
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
             "files": [], "left_alone_ids": [], "refused": []}
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
        tally["files"].append(path)
    return tally


def _rewrite_problem(before: str, after: str, updates: dict[str, str],
                     unlocated: list[str]) -> str | None:
    """Why this rewrite must not be written, or None if it is sound.

    TWO CHECKS, because they catch different mistakes. The YAML comparison catches a value
    written into the wrong place — a nested block, a neighbouring entry — by insisting the
    reparsed document equals the original with exactly these `sha256` values changed. The
    LINE comparison catches everything a parse cannot see: a reflowed scalar, a lost
    comment, a translated line ending. Only `sha256:` lines carrying one of the new values
    may differ, byte for byte, and every other line must survive untouched.
    """
    if unlocated:
        return (f"could not locate {', '.join(unlocated)} in the file, so the value has "
                f"nowhere to go.")
    expected = copy.deepcopy(yaml.safe_load(before) or {})
    for s in expected.get("sources") or []:
        if isinstance(s, dict) and str(s.get("id", "")) in updates:
            s["sha256"] = updates[str(s["id"])]
    try:
        actual = yaml.safe_load(after)
    except yaml.YAMLError as e:
        return f"the rewritten file did not parse as YAML ({e})."
    if actual != expected:
        return ("the rewritten file did not re-parse to the original with only these "
                "sha256 values changed.")
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    wanted = set(updates.values())
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for line in old_lines[i1:i2]:
            if not _SHA_RE.match(line):
                return (f"a line that is not a `sha256:` line would have changed: "
                        f"{line.strip()[:60]!r}.")
        for line in new_lines[j1:j2]:
            m = _SHA_RE.match(line)
            if not m or _scalar(m.group("value")) not in wanted:
                return (f"a line that is not one of the new `sha256:` values would have "
                        f"been written: {line.strip()[:60]!r}.")
    return None


ACCESS_FAILURES_FILE = "access-failures.json"
"""Name only, at `config.root` — declared once so the reader and the writer cannot name two
different files (corpus-toolkit#166)."""


def _load_access_failure_streaks(config) -> dict[tuple[str, str], int]:
    """{(group, id): consecutive fetch failures recorded as of the LAST run}, from
    `access-failures.json` at the corpus root.

    Missing, unreadable, or schema-mismatched is `{}`, never a crash. A run that cannot read
    its own bookkeeping must still be able to check sources — the streak merely restarts at
    zero for everyone, which delays an escalation rather than ever inventing one, the same
    direction every other defensive default in this file errs in (an unseeded baseline does
    not crash the run either). This is NOT "could not check reported as not there" (CONTEXT.md):
    nothing here claims the sources were fine, only that how many times in a row they failed
    is not currently known.
    """
    path = config.root / ACCESS_FAILURES_FILE
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return {}
    out: dict[tuple[str, str], int] = {}
    groups = raw.get("consecutive_fetch_failures")
    if not isinstance(groups, dict):
        return {}
    for gname, ids in groups.items():
        if not isinstance(ids, dict):
            continue
        for sid, n in ids.items():
            if isinstance(n, int) and n > 0:
                out[(gname, sid)] = n
    return out


def _write_access_failure_streaks(config, streaks: dict[tuple[str, str], int]) -> None:
    """Write `access-failures.json` at the corpus root. Entries at 0 are PRUNED, not written
    at 0 — the file's whole content is "who is currently on a failing streak", so a source
    that recovered leaves no trace rather than a zero a future reader has to filter out.

    Grouped back into `{group: {id: n}}` on write, the shape a corpus's own manifest already
    uses (group files of sources), so a human skimming this file recognises the layout.
    """
    groups: dict[str, dict[str, int]] = {}
    for (gname, sid), n in streaks.items():
        if n > 0:
            groups.setdefault(gname, {})[sid] = n
    payload = {"schema_version": 1, "consecutive_fetch_failures": groups}
    (config.root / ACCESS_FAILURES_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n")


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


def _update_access_failure_streaks(outcomes: list[SourceOutcome],
                                   previous: dict[tuple[str, str], int]
                                   ) -> dict[tuple[str, str], int]:
    """This run's outcomes folded into `previous` — the pure merge, no filesystem involved.

    ONLY the (group, id) keys THIS RUN OBSERVED are touched. Everything else in `previous`
    rides through unmodified — a `--group oar` run says nothing about `oam`, so `oam`'s
    recorded streak is neither bumped nor reset. Silently zeroing an out-of-scope group's
    streak would report "could not check" as "is fine now", the one collapse CONTEXT.md's
    outranking rule forbids everywhere else in this file.

    An outcome of anything OTHER THAN `fetch_failed` resets the streak (by omission — the
    entry is simply not carried forward), because every other outcome means the fetch itself
    succeeded: bytes arrived, whether or not they compared clean. This counter is about
    ACCESS specifically, not about drift, so a source that was reached at all is not on an
    access-failure streak regardless of what its comparison found.
    """
    out = dict(previous)
    for o in outcomes:
        key = (o.group, o.id)
        if o.outcome == "fetch_failed":
            out[key] = previous.get(key, 0) + 1
        else:
            out.pop(key, None)
    return out


class PersistentAccessFailure(NamedTuple):
    """One source whose consecutive fetch-failure streak reached the threshold this run —
    the escalation candidate `_open_access_failure_ticket` may file a ticket for
    (corpus-toolkit#166).

    `group` is FIRST, matching `ChangedSource`, so this type is a drop-in for
    `_count_by_group` (which reads only `.group`) — the same allocation policy #69 already
    established for the ticket budget applies here without a second implementation of it.
    """
    group: str
    id: str
    url: str
    consecutive_failures: int


ACCESS_FAILURE_STREAK_THRESHOLD = 3
"""How many CONSECUTIVE scheduled runs a source must fail to fetch before it is escalated
(corpus-toolkit#166) — the band between `--strict` (fails on any single failure) and the
20% systemic guard, where a source can fail every run forever and nothing says so today.

A RUN COUNT, not a calendar window, because this file has no idea what cadence is calling
it — `--group` alone lets one manifest run monthly-cadence groups and quarterly ones from
the same corpus (see executive-regulatory-frameworks/.github/workflows/scheduled.yml, whose
`monthly-drift` and `full-sweep` jobs check DIFFERENT groups on DIFFERENT crons). A
duration-based threshold would silently mean something different for each.

3, not lower, because the live case that motivated this (ERF#140, 45 sources unfetchable for
22 days) makes a single transient blip the wrong bar — the issue's own framing is "a weekly
run failing TWICE may be a transient", i.e. two is explicitly not enough. 3, not higher: the
platform's actual schedules are monthly and quarterly (see scheduled.yml above), so a much
larger threshold would let a genuinely stuck source run for the better part of a year, unlike
`MAX_ISSUES_PER_RUN`, before anything says so — the opposite failure this ticket exists to
fix. Not the same knob as `MAX_ISSUES_PER_RUN` (out of scope for corpus-toolkit#166) and not
the 20% systemic threshold: both are about ONE run; this is about many.
"""


def _persistent_access_failures(outcomes: list[SourceOutcome],
                                streaks: dict[tuple[str, str], int]
                                ) -> list[PersistentAccessFailure]:
    """Sources whose streak, AFTER this run's update, is at or above the threshold.

    `>=`, not `==`: `_open_access_failure_ticket` is `_file_once`-deduplicated exactly like
    `_open_issue` and `_open_group_finding`, so a source still failing on run 4, 5, 6 after
    crossing the threshold on run 3 keeps landing here and keeps finding its own ticket
    already open — "reported once, not once per run" is enforced by the dedup search, not by
    only ever calling this at the exact moment of crossing. That also makes the mechanism
    self-healing if the streak file is ever unreadable for one run: the next run that CAN
    read it re-derives the same candidate rather than needing to have caught an exact tick.

    Filtered on THIS RUN's outcome as well as the streak number, not the streak alone: a
    source that just recovered is not a candidate even if a caller passed a stale streak for
    it — the outcome is the ground truth for whether the fetch worked just now.
    """
    out = []
    for o in outcomes:
        if o.outcome != "fetch_failed":
            continue
        n = streaks.get((o.group, o.id), 0)
        if n >= ACCESS_FAILURE_STREAK_THRESHOLD:
            out.append(PersistentAccessFailure(o.group, o.id, o.url, n))
    return out


def _access_failures_in_spend_order(failures: list[PersistentAccessFailure]
                                    ) -> list[PersistentAccessFailure]:
    """Deterministic order for spending `MAX_ISSUES_PER_RUN` on these — the SAME policy
    `_tickets_in_spend_order` uses (corpus-toolkit#69: ascending per-group count, then group
    name, then detection order), reused rather than reinvented because the issue this
    function exists for (corpus-toolkit#166) asks for exactly that: share the budget and its
    allocation policy, not invent a second one.
    """
    per_group = _count_by_group(failures)
    return sorted(failures, key=lambda f: (per_group[f.group], f.group))


def _count_by_group(entries: list[ChangedSource]) -> dict[str, int]:
    """How many of `entries` fall in each group.

    ONE DEFINITION, because the sort key that spends the budget and the denominator that
    reports the spend have to be the same count. Two tallies three lines apart is the
    corpus-toolkit#67 `not compared` disagreement — an operator reading one number and
    counting a different one on the next line, with no way to tell which was wrong.
    """
    out: dict[str, int] = {}
    for c in entries:
        out[c.group] = out.get(c.group, 0) + 1
    return out


def _tickets_in_spend_order(changed: list[ChangedSource]) -> list[ChangedSource]:
    """The sources a ticket may be filed for, ordered smallest-drifting-group first (#69).

    THE ONE GATE ON THE TICKET-OPENING PATH, and the reason the filter lives here rather
    than at the call site: `MAX_ISSUES_PER_RUN` decides HOW MANY issues a run files and
    this decides WHICH, so a source this function does not return can neither get a ticket
    nor spend a slot. A caller cannot forget it.

    AN UNSEEDED SOURCE IS NOT A CHANGED SOURCE, the rule `_was_compared` states and ADR
    0010 decided (corpus-toolkit#145). Such a source is still counted, still in the group
    breakdown and still in `changed-sources.tsv` with an empty `old` column; what it no
    longer gets is a ticket asserting upstream drift, and `main` says so on its own line
    instead.

    `MAX_ISSUES_PER_RUN` decides HOW MANY issues a run files; this decides WHICH. The
    budget used to be spent in manifest iteration order, so whichever group the loop
    reached first consumed it. ERF run 31022774644 is the case: the 52-source DEQ group
    came first and took all 25, so `oar` — 484 of the 544 changed sources, 89% of the
    drift — got no issue at all, and neither did five apparently genuine changes spread
    across three other agencies.

    Ascending drift COUNT, because a group with two changed sources is the shape a human
    can act on and a group with 484 is a template change, a broken fetch, or a stale
    baseline that one ticket describes as well as 484 do. Small genuine findings therefore
    file before a bulk false positive absorbs the run. The alternative considered was a
    per-group share of the budget; it needs a policy for unused slices and would still let
    a 52-source noisy group spend its whole share ahead of a two-source real one.

    Deterministic and stable, so a re-run over the same drift files the same set and a
    changed set is a fact about upstream rather than about iteration luck:

    1. the group's total changed count, ascending;
    2. then the group name, so two groups of equal size never trade places;
    3. then detection order within a group — Python's sort is stable and `changed` is in
       manifest order, so a group's own sources keep the order the manifest gave them.

    This does NOT reduce what a run drops. A capped run still leaves `len(changed) -
    MAX_ISSUES_PER_RUN` sources unreported and still exits non-zero; the dropped ones are
    now the tail of the largest group instead of an arbitrary prefix.
    """
    ticketable = [c for c in changed if _was_compared(c.old)]
    # COUNTED OFF `ticketable`, not read from `per_group["changed"]` and not derived from
    # it either. Two reasons, and the second is the one that bites. First, the sort key is
    # "how many tickets will this group buy", and after the filter that is no longer the
    # group's changed count: a group of 484 unseeded sources and 2 genuinely drifted ones
    # buys two tickets and is the small, human-actionable shape this ordering exists to
    # file first — sorting it at 486 would starve it exactly the way corpus-toolkit#69
    # starved `oar`. Second, `per_group["changed"] - per_group["unseeded"]` is the
    # subtraction the per-group tally already warns about: an unseeded source whose fetch
    # then failed never reached `changed` at all, so subtracting it over-subtracts. The
    # list IS the tickets; counting it cannot disagree with itself.
    per_group_ticketable = _count_by_group(ticketable)
    return sorted(ticketable, key=lambda c: (per_group_ticketable[c.group], c.group))


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
    # LARGEST FIRST. ADR 0010 settles the findings' order against the TICKETS and says
    # nothing about their order among themselves, so this is a choice, not a decision it
    # made. It is observable only when the findings alone exhaust `MAX_ISSUES_PER_RUN` —
    # and in that regime no per-source ticket files at all, so no group here is "covered by
    # its own tickets" and none of them is worse off than the others on that count. What is
    # left to prefer by is the evidence: a finding says "these N changed together", so
    # dropping the one with the most sources behind it discards the most of what the run
    # observed. Ties by group name, so the order is a fact about the drift rather than
    # about manifest position, and a re-run over the same drift files the same set.
    return sorted(out, key=lambda f: (-len(f.ids), f.group))


def _run_url() -> str | None:
    """The Actions run this process is, or None when the environment does not say.

    None rather than a partial url: a link assembled from variables that are not set is a
    dead link that reads as a citation, and this is a report whose whole claim is that the
    numbers in it were observed somewhere specific.
    """
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not (server and repo and run_id):
        return None
    return f"{server}/{repo}/actions/runs/{run_id}"


def _open_group_finding(group: str, changed_ids: list[str], compared: int,
                        in_scope: int) -> bool:
    """Open one group drift finding: every COMPARED source in `group` changed (ADR 0010).

    CORRELATION, NOT CAUSE, in the wording as well as the rule. The tool observes that
    bytes moved, and the three whole-group events on record had three different reasons --
    a footer version bump, a set of urls that stopped serving, and, in oregon-counties, no
    change at all. So this says they changed together and stops there.

    It does NOT replace the individual tickets, and it is not filed instead of them.

    The equality is GATED here, not re-decided here: `_group_drift_findings` owns the rule
    and this refuses to write a report that does not satisfy it. `480 of 484` is the ">80%"
    finding ADR 0010 rejected, and the two numbers arrive independently -- without the gate,
    one careless call site is all it takes to publish the shape the decision rules out.
    """
    if len(changed_ids) != compared:
        raise ValueError(
            f"group {group!r}: a group drift finding reports that EVERY compared source "
            f"changed, and {len(changed_ids)} of {compared} did. A partial group is not "
            f"this finding: its sources have their own `Source changed:` tickets, and the "
            f"ones that held still are evidence against the pattern this would assert "
            f"(ADR 0010).")
    run = _run_url()
    body = (f"Automated detection: every compared source in the `{group}` group changed in "
            f"one run.\n\n"
            f"This reports that they changed TOGETHER and says nothing about why. A "
            f"whole-group change has been a template edit, a set of URLs that stopped "
            f"serving, and a manifest whose baselines were never recorded — the tool "
            f"observes bytes, not causes. The individual `Source changed:` tickets for "
            f"this group stand on their own and are not replaced by this finding "
            f"(ADR 0010).\n\n"
            f"- **Group**: {group}\n"
            f"- **Compared sources that changed**: {len(changed_ids)} of {compared}\n"
            # WHAT THE DENOMINATOR LEAVES OUT, on the face of the report. `2 of 2` is
            # true of a group of two and of a group of five where three were never
            # compared, and those are the two shapes corpus-toolkit#67 built the
            # per-group breakdown to separate.
            f"- **Sources in the group**: {in_scope}, of which "
            f"{in_scope - compared} not compared this run (no recorded baseline, or the "
            f"fetch did not complete)\n"
            f"- **Sample ids**: " + ", ".join(changed_ids[:10])
            + ("…" if len(changed_ids) > 10 else "") + "\n"
            + (f"- **Run**: {run}\n" if run else ""))
    return _file_once(f"Group drifted: {group}", body, f"group {group}")


def _open_access_failure_ticket(group: str, source_id: str, url: str,
                                consecutive_failures: int) -> bool:
    """Open one persistent-access-failure ticket (corpus-toolkit#166): a source that has
    failed to fetch on `consecutive_failures` scheduled runs in a row, at or past
    `ACCESS_FAILURE_STREAK_THRESHOLD`.

    THE TITLE CARRIES NO COUNT, for the identical reason `_open_group_finding` gives: the
    dedup search in `_file_once` is `in:title "<title>"`, so a title that moves while the
    source is still failing — `Source access failing: oar-7 (6 runs)` today, `(9 runs)` next
    month — breaks the search and the same unresolved condition files a fresh ticket every
    time it is checked. The count belongs in the body, which IS free to change on an edit
    this function does not perform (each failing run re-opens the search, finds the ticket
    already there, and stops).

    THE WORDING STATES ONLY WHAT WAS MEASURED: that our crawler could not fetch this URL on
    N consecutive scheduled runs. It does not say the document was removed, moved, or that
    upstream did anything at all — "45 sources unfetchable" is true, "45 sources removed
    upstream" is not, and nothing this tool observes can tell the two apart (the issue's own
    framing, and CONTEXT.md's outranking rule: "could not check" is never reported as "is
    not there"). Compare `_open_group_finding`'s "changed together, not why" — the same
    discipline, applied to access instead of content.
    """
    body = (f"Automated detection: this source has failed to fetch on "
            f"{consecutive_failures} consecutive scheduled run(s).\n\n"
            f"This is a fact about OUR ACCESS to the URL below, not a claim about what "
            f"upstream did. A fetch failure this persistent has usually meant a host that "
            f"started refusing or challenging this crawler, a URL that moved or 404s, or a "
            f"DNS or TLS change — the tool cannot tell which, only that {consecutive_failures} "
            f"attempts in a row did not get a response it could hash.\n\n"
            f"- **Group**: {group}\n"
            f"- **Source id**: {source_id}\n"
            f"- **Source URL**: {url}\n"
            f"- **Consecutive failed fetches**: {consecutive_failures}\n")
    return _file_once(f"Source access failing: {source_id}", body,
                      f"access failure {source_id}")


def _drifting_groups_in_spend_order(changed: list[ChangedSource]) -> list[str]:
    """Every group that drifted, in the order the budget is spent on them.

    DERIVED from `_tickets_in_spend_order` rather than re-deriving its sort key: the caller
    uses this to narrate an allocation the run already made. A second copy of the key that
    drifted from the first would print an authoritative-looking account of an order nobody
    used — the failure mode the rest of this file's reporting exists to prevent.
    """
    return list(dict.fromkeys(c.group for c in _tickets_in_spend_order(changed)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--open-issues", action="store_true",
                    help="open a GitHub issue per changed source (requires `gh` + GH_TOKEN)")
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
                    help="WRITE the computed hash into the manifest, into the working "
                         "tree only — the manifest is curated data and the diff goes "
                         "through review. Bare/`seed` fills sources with NO recorded "
                         "baseline and leaves every recorded one alone; `refresh` also "
                         "replaces recorded baselines, which is accepting the observed "
                         "change (the reconciliation path after adding a "
                         "`volatile_patterns:` entry). Sources whose fetch failed are "
                         "never written. Opens no issues (corpus-toolkit#68).")
    args = ap.parse_args()

    if args.record_baseline and args.open_issues:
        # Seeding is not a drift report. Filing tickets for sources whose baseline this
        # same run is about to write is guaranteed noise, and quietly ignoring one of the
        # two flags would leave an operator believing the other one ran.
        ap.error("--record-baseline and --open-issues do not go together: seeding a "
                 "baseline is not a drift report, and a source seeded by this run has no "
                 "drift to file. Seed first, review the manifest diff, then run again "
                 "with --open-issues.")

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
    # LAST RUN'S consecutive-fetch-failure streaks (corpus-toolkit#166), read BEFORE the
    # fetch loop so this run's own outcomes can be folded onto them once it is done. Missing
    # or unreadable reads as `{}` — see `_load_access_failure_streaks`'s docstring for why
    # that is not the same collapse CONTEXT.md's outranking rule forbids.
    previous_access_streaks = _load_access_failure_streaks(config)
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
            print(f"CHANGED  {sid}: {old[:12]}… -> {new[:12]}…")
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

    # THE CROSS-RUN COUNTER (corpus-toolkit#166), updated and written HERE, beside
    # `source-outcomes.json` and for the same reason: a wholly-failed run is exactly the run
    # this exists to remember, so it is unconditional, not gated behind `--open-issues` or
    # `--record-baseline`. Only sources THIS run observed are touched — see
    # `_update_access_failure_streaks`'s docstring for why an out-of-scope `--group` run must
    # not silently reset a group it never looked at.
    #
    # UNLIKE the source manifest, this file is not curated data a human reviews line by
    # line — it is machine bookkeeping, the same kind STATUS.md already is, and like
    # STATUS.md it needs a commit step in the calling workflow to actually persist past one
    # ephemeral run; corpus-toolkit's own reusable workflow gains that step alongside this
    # change. A workflow that never commits it degrades safely: the streak restarts at zero,
    # which only delays an escalation, never invents one.
    access_streaks = _update_access_failure_streaks(outcomes, previous_access_streaks)
    _write_access_failure_streaks(config, access_streaks)

    recorded = None
    if args.record_baseline:
        recorded = _record_baselines(config, fetched, in_scope, args.record_baseline,
                                     uncompared=uncompared_counts)

    # THE SOURCES A TICKET MAY BE FILED FOR, computed once and used everywhere the run
    # talks about what it reported: the spend loop, the cap arithmetic, the summary line
    # and the workflow output. Computing it twice is how the three "not compared" counts
    # of corpus-toolkit#67 came to disagree with each other.
    ticketable = _tickets_in_spend_order(changed)

    # SOURCES CROSSING THE PERSISTENT-ACCESS-FAILURE THRESHOLD THIS RUN (corpus-toolkit#166),
    # computed off the UPDATED streaks so a source failing its Nth consecutive run is a
    # candidate the moment N is reached, not one run later. Independent of `changed`/
    # `ticketable`: a persistently unfetchable source never produced a hash to compare, so it
    # cannot be drift, and a corpus with zero content changes can still owe this report.
    access_failures = _access_failures_in_spend_order(
        _persistent_access_failures(outcomes, access_streaks))

    # A run with no baseline at all cannot detect drift: every source compares unequal to
    # `''`, so 100% "changed" is a fact about the manifest, not about upstream. Measured,
    # never inferred — ERF was told to go look for empty baselines it did not have.
    #
    # STILL ITS OWN CONCEPT after corpus-toolkit#145, and deliberately so. The filter in
    # `_tickets_in_spend_order` suppresses every ticket this predicate used to suppress, so
    # longer decides anything about individual filings — but it is not the same statement.
    # It is a diagnosis of the RUN ("this detected seeding, not drift"), where the filter
    # is a fact about one source, and it is what backs the refusal message, the annotation
    # and `changed=false` on a corpus that has never been seeded at all. Folding it into
    # the per-source rule would delete a report of the whole-corpus condition and leave an
    # operator to infer it from N identical per-source lines.
    inert = bool(n_total) and n_unseeded == n_total and not args.record_baseline
    # And a run that checked NOTHING — an empty group filter, a typo'd `--group`, a
    # manifest whose `sources:` is empty — is not a clean run either. This used to be
    # switched off by the `bool(n_total)` guard above, so `--group nosuchgroup` printed
    # "0 changed ... of 0 checked" and exited 0: could-not-check reported as not-there,
    # inside the expression written to prevent exactly that.
    nothing_checked = n_total == 0

    if args.github_output:
        # `changed` drives whatever the calling workflow does next. An unseeded source
        # "changed" against an empty baseline, which is not a finding, so it must not fire
        # downstream work; the unseeded count rides along so a workflow can react to the
        # seeding condition itself. `ticketable` rather than `changed and not inert`
        # because the inert test only caught the WHOLLY unseeded corpus — one seeded source
        # anywhere switched it off, and a partly-seeded run then fired the workflow off
        # sources nothing had compared (corpus-toolkit#145). `ticketable` is empty in both
        # cases and needs no second predicate to say so.
        with open(args.github_output, "a") as f:
            f.write(f"changed={'true' if ticketable else 'false'}\n")
            f.write(f"unseeded={n_unseeded}\n")

    opened = attempted = 0
    # Per group, because on a capped run "which groups got a ticket" is now a DECISION this
    # run made and not a fact about the drift. BOTH numbers, for the reason the summary
    # line below carries both: a group whose two filings were attempted and both failed has
    # reported nothing, and printing only the attempt reinstates precisely the
    # corpus-toolkit#53 confusion — on the loudest line of a run that is already failing.
    attempted_by_group: dict[str, int] = {}
    opened_by_group: dict[str, int] = {}
    capped = False
    findings = _group_drift_findings(changed, per_group)
    findings_attempted = findings_opened = 0
    filed_findings: list[str] = []
    # corpus-toolkit#166: same budget as the drift tickets and findings, not a second one —
    # `access_attempted` is added into every cap check below alongside `findings_attempted`.
    access_attempted = access_opened = 0
    filed_access_failures: list[str] = []
    # `ticketable OR access_failures`, not `ticketable` alone: a persistently unfetchable
    # source produced no hash and is never in `ticketable`, so a corpus with zero drifted
    # sources but one source stuck failing every fetch must still enter this block.
    if args.open_issues and (ticketable or access_failures) and not inert:
        _ensure_label()
        # BEFORE the individual tickets, and out of the same budget (ADR 0010). Before,
        # because the spend order is smallest-drifting-group-first and therefore reaches
        # the largest group last — a finding queued behind the tickets is precisely the
        # finding that never files, which is the case corpus-toolkit#132 was opened about.
        # Out of the same budget, because a cap that some issues are exempt from is not a
        # cap: a corpus with 27 bulk-drifting groups would file 27 issues past a limit of
        # 25. There is at most one finding per group, so filing them first cannot flood.
        for f in findings:
            if attempted + findings_attempted + access_attempted >= MAX_ISSUES_PER_RUN:
                capped = True
                break
            findings_attempted += 1
            if _open_group_finding(f.group, f.ids, f.compared, f.in_scope):
                findings_opened += 1
                filed_findings.append(f.group)
        # AFTER the group findings, BEFORE the per-source drift tickets: an access-failure
        # escalation is rarer and more actionable than the typical drift ticket pool (it
        # took several consecutive runs to even qualify), so it should not be starved by a
        # large `ticketable` the way corpus-toolkit#69 found happening to whole groups —
        # while a group finding, having already replaced up to hundreds of individual
        # tickets, still goes first.
        if not capped:
            for pf in access_failures:
                if attempted + findings_attempted + access_attempted >= MAX_ISSUES_PER_RUN:
                    capped = True
                    break
                access_attempted += 1
                if _open_access_failure_ticket(pf.group, pf.id, pf.url,
                                               pf.consecutive_failures):
                    access_opened += 1
                    filed_access_failures.append(pf.id)
        for c in ticketable:
            if attempted + findings_attempted + access_attempted >= MAX_ISSUES_PER_RUN:
                capped = True
                break
            attempted += 1
            attempted_by_group[c.group] = attempted_by_group.get(c.group, 0) + 1
            ok = bool(_open_issue(c.id, c.url, c.old, c.new))
            opened += ok
            opened_by_group[c.group] = opened_by_group.get(c.group, 0) + ok

    # THE SUMMARY REPORTS WHAT WAS REPORTED, not only what drifted. The previous version
    # printed the changed count alone, which read as "these were filed" even when every
    # single filing had failed. The unseeded count is here for the same reason: it is the
    # difference between drift that means something and drift that cannot.
    # `not compared` is on this line and not only in the detail below it because this is
    # the line an operator reads: "of 3 checked" while one of the three was never compared
    # to anything is the same could-not-check-as-checked the rest of this file refuses.
    # ONE DEFINITION, used on every line that says it: a source that was in scope and never
    # compared to a baseline. Three adjacent lines carried three different sets -- this one
    # counted watch misses only, the group breakdown counted fetch failures too, and the
    # baseline tally used a third -- so an operator read `1 not compared` and counted 2 on
    # the next line, with no way to tell which was wrong except by reading the source.
    not_compared = len(failed) + len(watch_failed) + len(unreadable)
    why = ", ".join(filter(None, [
        f"{len(failed)} fetch failed" if failed else "",
        f"{len(watch_failed)} watched path missing" if watch_failed else "",
        f"{len(unreadable)} body not parseable as json" if unreadable else ""]))
    uncompared = f"{not_compared} not compared ({why}), " if not_compared else ""
    print(f"\n{len(changed)} changed, {len(failed)} fetch failure(s), {uncompared}"
          f"{n_unseeded} with no recorded baseline, of {n_total} checked.")
    _print_group_breakdown(per_group)
    if recorded is not None:
        print(f"{recorded['written']} baseline(s) recorded, "
              f"{recorded['already_current']} already current, "
              f"{recorded['left_alone']} left alone, "
              f"{recorded['failed_fetch']} skipped (not compared).")
        if recorded["files"]:
            print("manifest file(s) rewritten in the working tree — review the diff "
                  "before committing: "
                  + ", ".join(str(p) for p in recorded["files"]))
        if recorded["left_alone"]:
            shown = ", ".join(recorded["left_alone_ids"][:10])
            print(f"{recorded['left_alone']} source(s) already carry a recorded baseline "
                  f"that no longer matches upstream, and seed mode does not overwrite a "
                  f"curated value. Read the change, then re-run with "
                  f"`--record-baseline=refresh` to accept it. ({shown}"
                  f"{'…' if recorded['left_alone'] > 10 else ''})")
        for path, sid, why in recorded["refused"]:
            # `why` states what was and was not written FOR THAT BRANCH. It used to end in
            # a fixed "Nothing was written to that file", which was false on the
            # duplicate-id path — the other sources in the file are written normally — and
            # an operator who believed it and reverted lost good seeds.
            print(f"REFUSED to record {sid} in {path}: {why}", file=sys.stderr)
        if recorded["refused"]:
            _annotate("Baseline recording refused",
                      f"{len(recorded['refused'])} manifest entr(ies) could not be "
                      f"recorded; this run exits non-zero.")
    if n_unseeded and not args.record_baseline and not inert:
        # THE OTHER HALF OF corpus-toolkit#145, and the reason the ticket filter is not the
        # whole fix. Those sources used to be mentioned by the (wrong) tickets filed for
        # them; suppressing the tickets and saying nothing more would trade "could not
        # check reported as drift" for "could not check reported as absent", which is the
        # rule of CONTEXT.md broken in the other direction. So the ids are named here, the
        # remedy is named beside them, the annotation below puts it where CI looks, and the
        # exit status carries it out of the log entirely.
        #
        # THE PARTLY-SEEDED RUN ONLY, which is what `not inert` is doing. On a wholly
        # unseeded run the refusal below already speaks for the whole corpus and names the
        # same remedy; naming 3,447 of 3,447 ids beneath it adds a second annotation, a
        # great deal of length and no information, and that run's messaging is deliberately
        # left exactly as it was. Which sources is the missing fact only when some of them
        # were seeded and some were not.
        #
        # TRUNCATED, like every other listing in this file, and it says so rather than
        # trailing an unexplained `…`: at 3,447 unseeded sources this sentence would
        # otherwise claim to be their only report while naming 20 of them. The complete
        # list is `changed-sources.tsv`, where each carries the empty previous-hash column
        # that says what happened to it.
        shown_unseeded = ", ".join(unseeded_ids[:20])
        if n_unseeded > 20:
            shown_unseeded += f" (first 20 of {n_unseeded}; all of them are in "
            shown_unseeded += "changed-sources.tsv, with an empty previous-hash column)"
        print(f"{n_unseeded} of {n_total} in-scope source(s) have NO recorded baseline. A "
              f"source with `sha256: ''` can never compare equal, so it reports CHANGED "
              f"every run and its drift means nothing. NO `Source changed:` ticket was "
              f"filed for them and they spent none of the issue budget — an uncompared "
              f"source is not a changed source (ADR 0010) — so this line is the only "
              f"report they get: {shown_unseeded}. Seed them with "
              f"`corpus-detect-changes --config {args.config} --record-baseline`.",
              file=sys.stderr)
        _annotate("Sources have no recorded baseline and were not compared",
                  f"{n_unseeded} of {n_total} in-scope source(s) were never compared to "
                  f"anything and file no drift ticket: {shown_unseeded}. Seed with "
                  f"corpus-detect-changes --record-baseline.")
    elif n_unseeded and not args.record_baseline:
        # UNCHANGED WORDING, deliberately: this is the wholly-unseeded run's line and the
        # acceptance criterion for corpus-toolkit#145 is that its messaging does not move.
        print(f"{n_unseeded} of {n_total} in-scope source(s) have NO recorded baseline. A "
              f"source with `sha256: ''` can never compare equal, so it reports CHANGED "
              f"every run and its drift means nothing. Seed them with "
              f"`corpus-detect-changes --config {args.config} --record-baseline`.",
              file=sys.stderr)
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
    if args.open_issues:
        if inert:
            print(f"REFUSING to open issues: all {n_total} in-scope source(s) have no "
                  f"recorded baseline, so this run detected seeding, not drift. Filing "
                  f"against sources that never had a baseline is guaranteed noise — "
                  f"oregon-counties got 25 such tickets a week. Seed with "
                  f"`--record-baseline`, review the manifest diff, then run this again.",
                  file=sys.stderr)
            _annotate("Drift detection is inert",
                      f"{n_total} of {n_total} sources have no recorded baseline; no "
                      f"issues filed. Seed with corpus-detect-changes --record-baseline.")
        else:
            # THE DENOMINATOR IS WHAT THE RUN WAS ENTITLED TO FILE, not everything that
            # compared unequal. `2 opened, 0 failed, of 3 changed source(s)` invites the
            # subtraction that says one filing went missing, which is the
            # corpus-toolkit#53 confusion this line exists to prevent, reinstated by
            # understating instead of overstating. The sources left out are named on their
            # own line above, with their remedy.
            print(f"{opened} issue(s) opened or already open, "
                  f"{attempted - opened} failed, of {len(ticketable)} changed source(s) "
                  f"with a recorded baseline.")
            # SAID OUT LOUD, because a new kind of issue appearing in the tracker that the
            # run's own log never mentions is the corpus-toolkit#53 shape one level up.
            # Attempted and opened are both here for the same reason they are on the line
            # above: a finding that was attempted and does not exist is not a report.
            if findings_attempted:
                print(f"{findings_opened} group drift finding(s) opened or already open, "
                      f"{findings_attempted - findings_opened} failed"
                      + (": " + ", ".join(filed_findings) if filed_findings else "")
                      + ". Each names a group where EVERY COMPARED source changed in one "
                        "run: it reports that they changed together and asserts nothing "
                        "about why, and the individual tickets stand alongside it "
                        "(ADR 0010).")
            # corpus-toolkit#166, same reasoning as the findings line above: an escalation
            # that was attempted and does not exist afterwards is not a report.
            if access_attempted:
                print(f"{access_opened} persistent access-failure ticket(s) opened or "
                      f"already open, {access_attempted - access_opened} failed"
                      + (": " + ", ".join(filed_access_failures)
                         if filed_access_failures else "")
                      + f". Each names a source our crawler could not fetch on "
                        f"{ACCESS_FAILURE_STREAK_THRESHOLD}+ consecutive scheduled runs — a "
                        f"fact about OUR ACCESS, not a claim about what upstream did.")
        if capped:
            # `ticketable`, not `changed`: a source with no recorded baseline was never a
            # candidate for a ticket, so counting it here inflates the one number an
            # operator uses to judge how much of the report is missing and points them at
            # the cap for a condition the cap had nothing to do with.
            dropped = len(ticketable) - attempted
            print(f"STOPPED after {MAX_ISSUES_PER_RUN} — {dropped} changed source(s) were "
                  f"not reported, and THIS RUN EXITS NON-ZERO because a capped run is not "
                  f"a clean run (corpus-toolkit#67).", file=sys.stderr)
            # The SHAPE of the drift, not an asserted cause. A group at or near 100% has
            # been a template change (ERF's `oar`, 484/484 on a footer version bump), a
            # group of URLs that stopped serving what they used to (ERF's DEQ group,
            # 52/52), and an unseeded manifest (oregon-counties, 3,447/3,447) — the
            # breakdown above distinguishes them; this message no longer guesses.
            print(f"A group at or near 100% in the breakdown above is a template change, "
                  f"a broken fetch, or a stale baseline far more often than it is that "
                  f"many genuine upstream revisions. Check the largest group first.",
                  file=sys.stderr)
            # WHAT A CAPPED RUN MEANS CHANGED WITH #69, so the run says so rather than
            # leaving it to be inferred. A group sitting at 484/484 in the breakdown above
            # with no issue against it is now this allocation working as designed — but
            # "no issue for a group that drifted" is also exactly what the silent-reporting
            # failure of corpus-toolkit#53 looks like from the outside, and an operator
            # cannot tell those apart from the breakdown alone.
            spend_order = _drifting_groups_in_spend_order(changed)
            ticketable_by_group = _count_by_group(ticketable)
            print("BUDGET SPENT SMALLEST-GROUP-FIRST (corpus-toolkit#69): groups were "
                  "reported in ascending order of drift count — ties broken by group name, "
                  "then by manifest order within a group — so a small genuine finding files "
                  "ahead of a bulk one and what was dropped is the tail of the largest "
                  "group(s), NOT a delivery failure and not manifest order. Per group, "
                  "issues opened/attempted of sources changed: "
                  # THE SAME DENOMINATOR THE SPEND ORDER SORTED ON. `per_group['changed']`
                  # counts unseeded sources, which are neither in this order nor
                  # candidates for a ticket, so a group of 3 unseeded and 2 drifted
                  # sources sorted at 2 and printed `of 5` — a sequence that reads
                  # descending under a sentence asserting ascending order, inviting the
                  # subtraction that says three filings went missing.
                  + ", ".join(f"{g} ({opened_by_group.get(g, 0)}/"
                              f"{attempted_by_group.get(g, 0)} of "
                              f"{ticketable_by_group[g]})" for g in spend_order)
                  + ".", file=sys.stderr)
            # TWO DIFFERENT FINDINGS, and the earlier version of this block called both of
            # them "not reported at all". A group the budget never reached is this
            # allocation working; a group whose every filing failed is corpus-toolkit#53
            # happening, and the global `attempted and opened == 0` alarm below cannot see
            # it because a larger group's successful filings keep `opened` non-zero.
            unreached = [g for g in spend_order if not attempted_by_group.get(g)]
            all_failed = [g for g in spend_order
                          if attempted_by_group.get(g) and not opened_by_group.get(g)]
            if unreached:
                # "raising NO PER-SOURCE TICKET", not "raising nothing": since ADR 0010 a
                # group the ticket budget never reached may still have filed a group drift
                # finding, and this line would otherwise deny an issue that exists.
                also_found = [g for g in unreached if g in filed_findings]
                print(f"{len(unreached)} group(s) with drift were not reached by the "
                      f"budget at all: " + ", ".join(unreached) + ". Their drift is in the "
                      "breakdown above and in changed-sources.tsv; raising no per-source "
                      "ticket for them is the cap, not a finding about them."
                      + (f" {len(also_found)} of them did file a group drift finding, "
                         f"which reports only that their compared sources changed together "
                         f"(ADR 0010): " + ", ".join(also_found) + "."
                         if also_found else ""), file=sys.stderr)
            if all_failed:
                print(f"EVERY issue creation failed for {len(all_failed)} group(s) that "
                      f"drifted: " + ", ".join(all_failed) + ". Those sources were "
                      "attempted and NOTHING was filed — drift detected and not reported, "
                      "which is corpus-toolkit#53 and has nothing to do with the cap.",
                      file=sys.stderr)
            if n_unseeded:
                # NO LONGER A SUSPECT FOR THE CAP, and this used to name it as one. Since
                # corpus-toolkit#145 an unseeded source files no ticket and spends no
                # slot, so the sources that exhausted the budget here all had a baseline
                # and the drift is the drift. Still said, because they were not compared.
                print(f"MEASURED: {n_unseeded} of {n_total} in-scope source(s) have no "
                      f"recorded baseline. They are NOT the cause of this cap — an "
                      f"unseeded source files no ticket and spends no slot — but they "
                      f"were not compared either, and are named on their own line above. "
                      f"Seed them with `--record-baseline`; raising the cap will not "
                      f"reach them.", file=sys.stderr)
            else:
                print(f"MEASURED: 0 of {n_total} in-scope source(s) are missing a "
                      f"baseline, so an unrecorded baseline is NOT the cause here — this "
                      f"message used to assert it was, and was wrong for ERF.",
                      file=sys.stderr)
            # The findings are capped too, and a finding the budget never reached is the
            # silence corpus-toolkit#132 was opened about — said, not inferred.
            dropped_findings = [f.group for f in findings[findings_attempted:]]
            if dropped_findings:
                # NAMED, like every other line in this block. A bare count leaves the
                # reader to subtract two lists to find which group has no issue, and that
                # inference is what the message exists to remove.
                print(f"{len(dropped_findings)} group drift finding(s) were not filed "
                      f"either — the budget was spent before them: "
                      + ", ".join(dropped_findings[:20])
                      + ("…" if len(dropped_findings) > 20 else "")
                      + ". Those groups are in the breakdown above, at 100% of what was "
                        "compared.", file=sys.stderr)
            _annotate("Drift report truncated",
                      f"{dropped} of {len(changed)} changed sources were not reported "
                      f"(cap {MAX_ISSUES_PER_RUN}). See the per-group breakdown in the log.")
        if attempted and opened == 0:
            print("EVERY issue creation failed — drift is being detected and NOT "
                  "reported. This is the silent-reporting failure of corpus-toolkit#53.",
                  file=sys.stderr)
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
    # `capped`, `inert`, `nothing_checked` and a refused rewrite join the exit status
    # because each describes a run that did NOT do what a green check says it did: one
    # discarded 95% of its findings, one could not compare anything, one compared nothing
    # at all, and the last was ASKED to write baselines and did not write them — which in
    # CI is the documented remedy for #68 reporting success having done nothing. Drift
    # itself is still a signal, not an error.
    refused_write = bool(recorded and recorded["refused"])
    # AN UNSEEDED SOURCE WAS NOT COMPARED, and since corpus-toolkit#145 it files no ticket
    # either, so the exit status is what carries it out of a log nobody reads. Same
    # unconditional treatment as a missing watched path: the bytes arrived and the
    # comparison did not happen, and it will not happen on any future run until somebody
    # seeds it. NOT narrowed to "unseeded was the only finding" — whether a source was
    # compared is a fact about that source, and gating the signal on whether some other
    # source happened to drift would make it appear and disappear for unrelated reasons.
    # `--record-baseline` is exempt: that run is the remedy, and a remedy that reports
    # failure for the condition it just fixed never reports done.
    unchecked_baseline = bool(n_unseeded) and not args.record_baseline
    # `watch_failed` is unconditional, unlike `failed`, which needs --strict. Upstream being
    # briefly unreachable is ordinary; a source that was fetched successfully and still
    # could not be compared is not, and it stays uncompared on every subsequent run until
    # somebody looks. Could-not-check is never reported as nothing-to-report (CONTEXT.md).
    sys.exit(1 if (failed and args.strict) or watch_failed or unreadable
             or systemic or capped or inert or unchecked_baseline
             or nothing_checked or refused_write else 0)


if __name__ == "__main__":
    main()
