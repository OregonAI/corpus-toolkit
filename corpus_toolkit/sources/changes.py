#!/usr/bin/env python3
"""corpus-detect-changes — re-fetch every source declared in the corpus's
source manifest, diff content hashes, write changed-sources.tsv, optionally
open a GitHub issue per drifted source. Ported from
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

Exit codes: 0 on a run that could actually detect drift. 1 when a fetch fails under
`--strict`, when failures are systemic (>20%), when the issue cap truncated the report
(a capped run is not a clean run), when no in-scope source has a baseline at all, when
the scope came out empty so nothing was checked, and when `--record-baseline` refused a
rewrite it could not account for. The common thread: a run that could not do the thing
it reports on must not report success. A changed source is still a signal, not an error.
"""
import argparse
import copy
import difflib
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit.config import iter_manifest_sources
from corpus_toolkit.repo import content_hash

USER_AGENT = "corpus-toolkit-change-detector"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _format_for(url: str, declared: str | None) -> str:
    if declared:
        return declared
    path = url.lower().split("?")[0]
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "html"
    return ext if ext in ("pdf", "xls", "xlsx", "docx", "xml") else "html"


ISSUE_LABEL = "source-change"

# Opening more issues than this in one run is not reporting, it is a stampede. A drift
# run that wants hundreds of issues is almost always describing a broken BASELINE -- a
# manifest whose `sha256` values were never recorded compares unequal to everything, so
# every source reads as changed, forever. Capping and saying so surfaces that; opening
# 618 issues buries it. See OregonAI/oregon-collective-bargaining#14 for the case that
# motivated this.
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


def _open_issue(source_id, url, old, new) -> bool:
    """Open one drift issue. Returns True only if an issue now exists for this source.

    RETURN VALUES ARE CHECKED HERE, unlike the version this replaces, which called
    `subprocess.run` bare and discarded every failure. That is how 618 consecutive
    creation failures produced a log reading `618 changed, 58 fetch failure(s)` and no
    other sign -- the summary counts what DRIFTED, and an operator reasonably reads it
    as what was REPORTED (corpus-toolkit#53).
    """
    if not shutil.which("gh"):
        print(f"NOTE: 'gh' not on PATH — skipping issue creation for {source_id}", file=sys.stderr)
        return False
    title = f"Source changed: {source_id}"
    existing = subprocess.run(
        ["gh", "issue", "list", "--label", ISSUE_LABEL, "--state", "open",
         "--search", f'in:title "{title}"', "--json", "number", "--jq", "length"],
        capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() not in ("", "0"):
        print(f"Issue already open for {source_id}, skipping")
        return True
    body = (f"Automated detection.\n\n- **Document id**: {source_id}\n"
            f"- **Source URL**: {url}\n- **Previous sha256**: {old}\n"
            f"- **New sha256**: {new}\n")
    r = subprocess.run(["gh", "issue", "create", "--label", ISSUE_LABEL,
                        "--title", title, "--body", body],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"FAILED to open issue for {source_id}: {r.stderr.strip()[:300]}",
              file=sys.stderr)
        return False
    return True


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
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace() or stripped.startswith("- "):
            cur = None                      # a new sequence entry, or a top-level key
        m = _ID_RE.match(line)
        if m:
            sid = _scalar(m.group("value"))
            cur = sid if sid in wanted and sid not in plan else None
            if cur is not None:
                plan[cur] = [i, None, len(m.group("lead"))]
            continue
        m = _SHA_RE.match(line)
        if m and cur is not None and plan[cur][1] is None:
            plan[cur][1] = i
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


def _record_baselines(config, fetched: dict, in_scope: set, mode: str) -> dict:
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
    for path, group in config_mod.load_source_manifest_group_files(config):
        gname = group.get("group") or "manifest"
        sources = [s for s in (group.get("sources") or []) if isinstance(s, dict)]
        occurrences: dict[str, int] = {}
        for s in sources:
            sid = str(s.get("id", ""))
            occurrences[sid] = occurrences.get(sid, 0) + 1
        updates: dict[str, str] = {}
        for s in sources:
            sid = str(s.get("id", ""))
            key = (gname, sid)
            if key not in in_scope:
                continue
            if occurrences[sid] > 1:
                if sid not in [r[1] for r in tally["refused"]]:
                    tally["refused"].append(
                        (path, sid, "duplicate id in this group file — cannot tell which "
                                    "entry the fetched hash belongs to. No baseline was "
                                    "written for this id; the other sources in this file "
                                    "were written normally. Give the entries distinct ids "
                                    "and re-run."))
                continue
            new = fetched.get(key)
            if new is None:
                tally["failed_fetch"] += 1
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
        parts.append(f"{name} {st['changed']}/{st['total']}{unseeded}")
    print("drift by group (changed/checked): " + ", ".join(parts))


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

    if args.check_robots:
        return _report_robots(config, args.group)

    patterns = config.volatile_patterns
    pattern_hits = [0] * len(patterns)
    pattern_bytes = [0] * len(patterns)
    n_normalizable = normalizable_bytes = 0
    changed, failed = [], []
    per_group: dict[str, dict] = {}
    fetched: dict[tuple[str, str], str] = {}
    in_scope: set[tuple[str, str]] = set()
    n_total = n_unseeded = 0
    for s in iter_manifest_sources(config):
        if args.group and s.get("_group") not in args.group:
            continue
        n_total += 1
        # `or ""`, not a plain get: `sha256:` with no value parses to None, and the
        # CHANGED line's `old[:12]` then dies mid-crawl — on a manifest shape that is
        # exactly what corpus-toolkit#68 is about, i.e. in front of the operator running
        # the documented remedy.
        sid, url = s["id"], s["url"]
        old = str(s.get("sha256") or "").strip()
        gname = s.get("_group", "manifest")
        stats = per_group.setdefault(gname, {"changed": 0, "total": 0, "unseeded": 0})
        stats["total"] += 1
        in_scope.add((gname, sid))
        if not old:
            n_unseeded += 1
            stats["unseeded"] += 1
        fmt = _format_for(url, s.get("format"))
        try:
            raw = fetch(url)
            if fmt in ("html", "xml"):
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
            new = content_hash(raw, fmt, patterns)
        except Exception as e:
            failed.append(sid)
            print(f"FETCH FAILED {sid}: {url} ({e})")
            continue
        fetched[(gname, sid)] = new
        if new != old:
            changed.append((sid, url, old, new))
            stats["changed"] += 1
            print(f"CHANGED  {sid}: {old[:12]}… -> {new[:12]}…")

    out = config.root / "changed-sources.tsv"
    out.write_text("".join(f"{a}\t{b}\t{c}\t{d}\n" for a, b, c, d in changed))

    recorded = None
    if args.record_baseline:
        recorded = _record_baselines(config, fetched, in_scope, args.record_baseline)

    # A run with no baseline at all cannot detect drift: every source compares unequal to
    # `''`, so 100% "changed" is a fact about the manifest, not about upstream. Measured,
    # never inferred — ERF was told to go look for empty baselines it did not have.
    inert = bool(n_total) and n_unseeded == n_total and not args.record_baseline
    # And a run that checked NOTHING — an empty group filter, a typo'd `--group`, a
    # manifest whose `sources:` is empty — is not a clean run either. This used to be
    # switched off by the `bool(n_total)` guard above, so `--group nosuchgroup` printed
    # "0 changed ... of 0 checked" and exited 0: could-not-check reported as not-there,
    # inside the expression written to prevent exactly that.
    nothing_checked = n_total == 0

    if args.github_output:
        # `changed` drives whatever the calling workflow does next. On an inert run every
        # source "changed" against an empty baseline, which is not a finding, so it must
        # not fire downstream work; the unseeded count rides along so a workflow can react
        # to the seeding condition itself.
        with open(args.github_output, "a") as f:
            f.write(f"changed={'true' if changed and not inert else 'false'}\n")
            f.write(f"unseeded={n_unseeded}\n")

    opened = attempted = 0
    capped = False
    if args.open_issues and changed and not inert:
        _ensure_label()
        for sid, url, old, new in changed:
            if attempted >= MAX_ISSUES_PER_RUN:
                capped = True
                break
            attempted += 1
            opened += bool(_open_issue(sid, url, old, new))

    # THE SUMMARY REPORTS WHAT WAS REPORTED, not only what drifted. The previous version
    # printed the changed count alone, which read as "these were filed" even when every
    # single filing had failed. The unseeded count is here for the same reason: it is the
    # difference between drift that means something and drift that cannot.
    print(f"\n{len(changed)} changed, {len(failed)} fetch failure(s), "
          f"{n_unseeded} with no recorded baseline, of {n_total} checked.")
    _print_group_breakdown(per_group)
    if recorded is not None:
        print(f"{recorded['written']} baseline(s) recorded, "
              f"{recorded['already_current']} already current, "
              f"{recorded['left_alone']} left alone, "
              f"{recorded['failed_fetch']} skipped (fetch failed).")
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
    if n_unseeded and not args.record_baseline:
        print(f"{n_unseeded} of {n_total} in-scope source(s) have NO recorded baseline. A "
              f"source with `sha256: ''` can never compare equal, so it reports CHANGED "
              f"every run and its drift means nothing. Seed them with "
              f"`corpus-detect-changes --config {args.config} --record-baseline`.",
              file=sys.stderr)
    for pat, hits, removed in zip(patterns, pattern_hits, pattern_bytes):
        if not n_normalizable:
            break
        shown = pat.pattern.decode("utf-8", "replace")
        share = removed / normalizable_bytes if normalizable_bytes else 0.0
        if not hits:
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
            print(f"{opened} issue(s) opened or already open, "
                  f"{attempted - opened} failed, of {len(changed)} changed source(s).")
        if capped:
            dropped = len(changed) - attempted
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
            if n_unseeded:
                print(f"MEASURED: {n_unseeded} of {n_total} in-scope source(s) have no "
                      f"recorded baseline and therefore drift every run. Seed with "
                      f"`--record-baseline` before raising the cap.", file=sys.stderr)
            else:
                print(f"MEASURED: 0 of {n_total} in-scope source(s) are missing a "
                      f"baseline, so an unrecorded baseline is NOT the cause here — this "
                      f"message used to assert it was, and was wrong for ERF.",
                      file=sys.stderr)
            _annotate("Drift report truncated",
                      f"{dropped} of {len(changed)} changed sources were not reported "
                      f"(cap {MAX_ISSUES_PER_RUN}). See the per-group breakdown in the log.")
        if attempted and opened == 0:
            print("EVERY issue creation failed — drift is being detected and NOT "
                  "reported. This is the silent-reporting failure of corpus-toolkit#53.",
                  file=sys.stderr)
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
    sys.exit(1 if (failed and args.strict) or systemic or capped or inert
             or nothing_checked or refused_write else 0)


if __name__ == "__main__":
    main()
