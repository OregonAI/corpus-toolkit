#!/usr/bin/env python3
"""corpus-detect-changes — re-fetch every source in `_meta/source-manifest.yml`,
diff content hashes, write changed-sources.tsv, optionally open a GitHub issue
per drifted source. Ported from oregon-policy-repo/src/detect_changes.py; the
Oregon-specific SharePoint-listing diff (`check_sp_listing`) is NOT ported —
it re-queries a specific vendor's list-view API and doesn't generalize. A
corpus that needs it keeps that check as its own local script (it can still
import `corpus_toolkit.repo.content_hash` etc.) and runs it alongside this one.

Manifest shape (`_meta/source-manifest.yml`):
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

import yaml

from corpus_toolkit import config as config_mod
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--open-issues", action="store_true",
                    help="open a GitHub issue per changed source (requires `gh` + GH_TOKEN)")
    ap.add_argument("--github-output", help="path to $GITHUB_OUTPUT")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    manifest = yaml.safe_load(config.source_manifest_path.read_text()) or {}

    changed, failed = [], []
    for s in manifest.get("sources", []):
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

    print(f"\n{len(changed)} changed, {len(failed)} fetch failure(s).")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
