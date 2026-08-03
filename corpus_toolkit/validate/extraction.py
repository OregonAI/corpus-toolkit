"""corpus-verify-extraction — does the committed text carry every token of the source?

    corpus-verify-extraction --config _meta/corpus.yml [--only <id>]

WHY THIS EXISTS (corpus-toolkit#24). `source_sha256` pins OUR RENDERING of a source
— hash_snapshot() hashes the committed .txt extraction, deterministically, and
verify-provenance compares against that. It therefore cannot notice extraction
loss: a truncated pdftotext run hashes cleanly and verifies forever. The check
that CAN notice is a content-token multiset comparison between the committed
`## Full text` and the RAW source file — pioneered by
federal-reference/src/check_extraction.py, generalized here.

THE PLUGIN SEAM. Page furniture (running headers, footers, page numbers) is a
corpus-specific decision that must be shared with the corpus's own extractor —
re-implementing it here would let this check pass by agreeing with a bug in the
other direction. A corpus registers `plugins.extraction_module:
"src.check_helpers"` exposing `raw_tokens(doc_id, raw_path) -> list[str]`; the
module owns furniture exclusion exactly as its ingester does.

WITHOUT the plugin the tool runs in REPORT-ONLY mode: a generic tokenizer over
the raw file (pdftotext for PDFs, tag-stripping for XML/HTML) that will flag
legitimate furniture as discrepancies — so it prints them and exits 0 with a
note to register the plugin. Enforcement without the shared furniture rules
would be a gate that fires on noise; a gate that fires on noise gets deleted.

SKIPS ARE REPORTED, NEVER SILENT. `snapshot_policy: hash-only` with no committed
raw file means there is nothing to compare — the doc is listed under SKIPPED so
nobody reads this check's green as covering it (the oregon-policy-repo EOs are
the canonical case).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter

from corpus_toolkit import config as config_mod
from corpus_toolkit.plugins import load_attr
from corpus_toolkit.repo import content_files, extract_fulltext, parse_frontmatter

TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/§-]*")


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def generic_raw_tokens(raw_path) -> list[str] | None:
    """Best-effort raw tokens with NO furniture handling — report-only quality."""
    suffix = raw_path.suffix.lower()
    if suffix == ".pdf":
        try:
            out = subprocess.run(["pdftotext", "-layout", str(raw_path), "-"],
                                 capture_output=True, text=True, check=True).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        return tokens(out)
    text = raw_path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".xml", ".html"):
        text = re.sub(r"<[^>]+>", " ", text)
    return tokens(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", action="append", help="check only these doc id(s)")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    plugin_fn = None
    if getattr(config, "extraction_module", None):
        plugin_fn = load_attr(config.extraction_module, config.root)

    checked = skipped = failed = 0
    for path in content_files(config):
        fm, body = parse_frontmatter(path)
        doc_id = fm.get("id", path.stem)
        if args.only and doc_id not in args.only:
            continue
        full = extract_fulltext(body)
        if not full:
            continue                       # summary docs have no verbatim claim to check
        snap_id = fm.get("snapshot_id") or doc_id
        fmt = fm.get("source_format", "html")
        raw = config.snapshot_dir / f"{snap_id}.{fmt}"
        if not raw.is_file():
            skipped += 1
            print(f"  skip  {doc_id}: no committed raw source "
                  f"({'hash-only' if fm.get('snapshot_policy') == 'hash-only' else 'missing'}) "
                  f"— extraction loss is UNCHECKABLE here")
            continue
        if plugin_fn:
            expect = plugin_fn(doc_id, raw)
        else:
            expect = generic_raw_tokens(raw)
        if expect is None:
            skipped += 1
            print(f"  skip  {doc_id}: no tokenizer for {raw.suffix} "
                  f"(register plugins.extraction_module)")
            continue
        got = tokens(full)
        checked += 1
        missing = Counter(expect) - Counter(got)
        extra = Counter(got) - Counter(expect)
        if not missing and not extra:
            print(f"  ok    {doc_id}: {len(got):,} tokens match the raw source")
            continue
        failed += 1
        print(f"  {'FAIL' if plugin_fn else 'DIFF'}  {doc_id}: "
              f"{sum(missing.values())} missing, {sum(extra.values())} unexpected")
        for tkn, n in list(missing.items())[:5]:
            print(f"           missing {n}x {tkn!r}")

    print(f"\n{checked} checked, {skipped} skipped (listed above), {failed} with "
          f"discrepancies.")
    if skipped and not checked:
        print("NOTE: nothing was checkable — this green asserts nothing.",
              file=sys.stderr)
    if not plugin_fn and failed:
        print("REPORT-ONLY: no plugins.extraction_module registered, so furniture "
              "exclusions are unavailable and discrepancies above may be page "
              "furniture. Register the plugin (sharing the ingester's furniture "
              "rules) to make this a hard gate.", file=sys.stderr)
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
