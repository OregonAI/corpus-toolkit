"""corpus-verify — record a HUMAN verification act on documents.

    corpus-verify --config _meta/corpus.yml --doc <id> [--doc <id> ...] \
        --by @handle --attest "read rendered text against the official source"
    corpus-verify --config _meta/corpus.yml --doc <id> --dry-run

THE ONE TOOL THAT WRITES last_verified/verified_by. Every corpus's AGENTS.md rule 6
says these fields are set by the human reviewer at approval and never fabricated —
and until M4 nothing on the platform could set them at all, which is how three
corpora ended up bulk-stamping ingestion dates into them (corrected in the M4
un-stamp). This tool exists so the honest path is also the easy one.

WHAT A VERIFICATION ACT IS depends on the document (docs/verification-loop.md):
  verbatim docs   read the rendered `## Full text` against the live official
                  source; confirm no drift the snapshot hash can't see
  summary docs    open the official link; confirm it is the claimed instrument,
                  the metadata (term, dates, version) matches, and the summary
                  misstates nothing
  dataset docs    re-fetch or re-derive the artifact; confirm the recorded hash
                  still matches and the figures the doc states are reproducible

THE TOOL DOES NOT PERFORM THE ACT — the human does. `--attest` is the required
one-line statement of WHAT WAS DONE; refusing to stamp without it is the point.
The stamp is today's date + the attester's handle, written into frontmatter with
a targeted line edit (never a YAML re-dump — hand formatting survives).

--dry-run prints the doc's checklist (doc_type, source_url, current hash state,
the act definition) and writes nothing — the smoke path, and the reviewer's
worksheet.
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys

from corpus_toolkit import config as config_mod
from corpus_toolkit.repo import content_files, parse_frontmatter

ACTS = {
    "verbatim": "read the rendered '## Full text' against the live official source",
    "summary": ("open the official link; confirm identity, metadata (term/dates/"
                "version), and that the summary misstates nothing"),
    "mixed": "both of the above, per section",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--doc", action="append", required=True)
    ap.add_argument("--by", help="the human reviewer's handle, e.g. @morficflux")
    ap.add_argument("--attest", help="one line: what verification act was performed")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not (args.by and args.attest):
        ap.error("--by and --attest are required to stamp (or use --dry-run). "
                 "The stamp asserts a human act; the tool will not assert one "
                 "nobody claims to have performed.")

    config = config_mod.load(args.config)
    by_id = {}
    for path in content_files(config):
        fm, _ = parse_frontmatter(path)
        by_id[fm.get("id", path.stem)] = (path, fm)

    today = datetime.date.today().isoformat()
    stamped = 0
    for doc_id in args.doc:
        if doc_id not in by_id:
            print(f"  ERROR {doc_id}: no such document", file=sys.stderr)
            return 1
        path, fm = by_id[doc_id]
        mode = fm.get("content_mode", "verbatim")
        act = ACTS.get(mode, ACTS["verbatim"])
        print(f"  {doc_id}")
        print(f"    doc_type: {fm.get('doc_type')} · content_mode: {mode}")
        print(f"    official source: {fm.get('source_url')}")
        print(f"    currently: last_verified={fm.get('last_verified')!r} "
              f"verified_by={fm.get('verified_by')!r}")
        print(f"    the act: {act}")
        if args.dry_run:
            continue
        text = path.read_text(encoding="utf-8")
        text, n1 = re.subn(r'^last_verified:.*$',
                           f'last_verified: "{today}"', text, count=1, flags=re.M)
        text, n2 = re.subn(r'^verified_by:.*$',
                           f'verified_by: "{args.by}"', text, count=1, flags=re.M)
        if n1 != 1 or n2 != 1:
            print(f"  ERROR {doc_id}: frontmatter fields not found for stamping",
                  file=sys.stderr)
            return 1
        path.write_text(text, encoding="utf-8")
        stamped += 1
        print(f"    STAMPED {today} by {args.by} — attested: {args.attest}")

    if not args.dry_run:
        print(f"\n{stamped} document(s) stamped. Commit these in a PR whose "
              f"message carries the attestation — the diff is the review record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
