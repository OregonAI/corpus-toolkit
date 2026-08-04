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
      sha256: <hash recorded at last ingest/refresh>
      format: html   # optional; inferred from the URL's extension otherwise

Exit code is 0 unless a fetch fails outright — a changed source is a signal,
not an error.
"""
import argparse
import shutil
import subprocess
import sys
import urllib.request

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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--open-issues", action="store_true",
                    help="open a GitHub issue per changed source (requires `gh` + GH_TOKEN)")
    ap.add_argument("--github-output", help="path to $GITHUB_OUTPUT")
    ap.add_argument("--group", action="append",
                    help="directory-mode only: check just these source group(s) "
                         "(repeatable) — the per-cadence cron's knob")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on ANY fetch failure (the pre-M4 behavior). Default "
                         "tolerates isolated failures: over ~2,000 sources a weekly "
                         "run has a near-certain transient, and one dead fetch "
                         "failing the whole run is how ERF's drift detection ended "
                         "up retired with 813 sources frozen. Systemic failure "
                         "(>20%% of fetches) still exits 1 either way.")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    _warn_recheck_is_not_honoured(config)

    changed, failed = [], []
    n_total = 0
    for s in iter_manifest_sources(config):
        if args.group and s.get("_group") not in args.group:
            continue
        n_total += 1
        sid, url, old = s["id"], s["url"], s.get("sha256", "")
        fmt = _format_for(url, s.get("format"))
        try:
            new = content_hash(fetch(url), fmt)
        except Exception as e:
            failed.append(sid)
            print(f"FETCH FAILED {sid}: {url} ({e})")
            continue
        if new != old:
            changed.append((sid, url, old, new))
            print(f"CHANGED  {sid}: {old[:12]}… -> {new[:12]}…")

    out = config.root / "changed-sources.tsv"
    out.write_text("".join(f"{a}\t{b}\t{c}\t{d}\n" for a, b, c, d in changed))
    if args.github_output:
        with open(args.github_output, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    opened = attempted = 0
    capped = False
    if args.open_issues and changed:
        _ensure_label()
        for sid, url, old, new in changed:
            if attempted >= MAX_ISSUES_PER_RUN:
                capped = True
                break
            attempted += 1
            opened += bool(_open_issue(sid, url, old, new))

    # THE SUMMARY REPORTS WHAT WAS REPORTED, not only what drifted. The previous version
    # printed the changed count alone, which read as "these were filed" even when every
    # single filing had failed.
    print(f"\n{len(changed)} changed, {len(failed)} fetch failure(s) "
          f"of {n_total} checked.")
    if args.open_issues:
        print(f"{opened} issue(s) opened or already open, "
              f"{attempted - opened} failed, of {len(changed)} changed source(s).")
        if capped:
            print(f"STOPPED after {MAX_ISSUES_PER_RUN} — {len(changed) - attempted} "
                  f"changed source(s) were not reported. A run this large usually means "
                  f"the manifest baseline is empty rather than that upstream moved: a "
                  f"source with `sha256: ''` can never compare equal, so it drifts every "
                  f"run. Check the manifest before raising the cap.", file=sys.stderr)
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
    sys.exit(1 if (failed and args.strict) or systemic else 0)


if __name__ == "__main__":
    main()
