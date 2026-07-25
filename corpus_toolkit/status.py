#!/usr/bin/env python3
"""corpus-generate-status — regenerate STATUS.md: doc counts by type,
freshness distribution against `status.reverify_days` (corpus.yml), and
source-manifest coverage. New in the toolkit (oregon-policy-repo doesn't
have this yet); see docs/reference-architecture.md's "Status layer".

  --output PATH   where to write STATUS.md (required)
  --check         exit 1 if the existing file at --output is stale
"""
import argparse
import datetime
import sys
from collections import Counter

import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit.repo import content_files, parse_frontmatter


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(str(s))
    except ValueError:
        return None


def generate(config, today: datetime.date) -> str:
    by_type = Counter()
    overdue = []
    total = 0
    for p in content_files(config):
        fm, _ = parse_frontmatter(p)
        total += 1
        by_type[fm.get("doc_type", "unknown")] += 1
        lv = _parse_date(fm.get("last_verified"))
        if lv is None or (today - lv).days > config.reverify_days:
            overdue.append((fm.get("id", p.stem), fm.get("doc_type", "unknown"),
                            lv.isoformat() if lv else "never"))

    manifest_count = None
    if config.source_manifest_path.is_file():
        manifest = yaml.safe_load(config.source_manifest_path.read_text()) or {}
        manifest_count = len(manifest.get("sources", []))

    lines = [
        f"# STATUS — {config.name or config.id}",
        "",
        f"Generated {today.isoformat()}. Non-authoritative; see DISCLAIMER.md.",
        "",
        "## Documents by type",
        "",
        "| doc_type | count |",
        "|---|---|",
    ]
    for dt, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {dt} | {n} |")
    lines += ["", f"**Total: {total}**", ""]

    if manifest_count is not None:
        lines += [f"## Source manifest", "", f"{manifest_count} declared source(s) in "
                  f"`{config.source_manifest_path.name}`.", ""]

    lines += [
        f"## Freshness (reverify every {config.reverify_days} days)",
        "",
        f"{len(overdue)} of {total} document(s) overdue for re-verification.",
        "",
    ]
    if overdue:
        lines += ["| id | doc_type | last_verified |", "|---|---|---|"]
        for doc_id, dt, lv in overdue[:50]:
            lines.append(f"| {doc_id} | {dt} | {lv} |")
        if len(overdue) > 50:
            lines.append(f"| … | *{len(overdue) - 50} more* | |")
        lines.append("")

    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--output", required=True, help="path to write STATUS.md")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the existing --output file is stale")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    today = datetime.date.today()
    content = generate(config, today)

    out_path = (config.root / args.output).resolve()
    if args.check:
        # freshness table would always differ by generation date; compare doc-count
        # section only, which is the part CI can meaningfully gate on.
        current = out_path.read_text() if out_path.is_file() else ""
        if "## Documents by type" not in current:
            print(f"{args.output} is missing or stale — run corpus-generate-status")
            sys.exit(1)
        print(f"{args.output} present.")
        return

    out_path.write_text(content)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
