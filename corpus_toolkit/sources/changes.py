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


def _open_issue(source_id, url, old, new):
    if not shutil.which("gh"):
        print(f"NOTE: 'gh' not on PATH — skipping issue creation for {source_id}", file=sys.stderr)
        return
    title = f"Source changed: {source_id}"
    existing = subprocess.run(
        ["gh", "issue", "list", "--label", "source-change", "--state", "open",
         "--search", f'in:title "{title}"', "--json", "number", "--jq", "length"],
        capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() not in ("", "0"):
        print(f"Issue already open for {source_id}, skipping")
        return
    body = (f"Automated detection.\n\n- **Document id**: {source_id}\n"
            f"- **Source URL**: {url}\n- **Previous sha256**: {old}\n"
            f"- **New sha256**: {new}\n")
    subprocess.run(["gh", "issue", "create", "--label", "source-change",
                    "--title", title, "--body", body])


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
    if args.open_issues:
        for sid, url, old, new in changed:
            _open_issue(sid, url, old, new)

    print(f"\n{len(changed)} changed, {len(failed)} fetch failure(s) "
          f"of {n_total} checked.")
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
