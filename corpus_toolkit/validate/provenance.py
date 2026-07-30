#!/usr/bin/env python3
"""corpus-verify-provenance — verify provenance under the full-text-first
content policy. Ported from oregon-policy-repo/src/verify_provenance.py; the
one corpus-specific piece (which portion of a shared snapshot a document's
'## Full text' should cover) is delegated to the `snapshot_slice_module`
plugin hook (default: identity, whole text) instead of hardcoded per-corpus
slicing rules.

For every content file: the source snapshot must exist and match the
recorded content hash (hash_snapshot — deterministic, no re-extraction).

content_mode: verbatim (required for state-authored doc_types):
  - a '## Full text' section must exist;
  - every non-empty line of it, whitespace-normalized, must appear in the
    snapshot text IN ORDER — mechanically prevents fabrication;
  - coverage (normalized full-text length / normalized snapshot-slice
    length) must be >= coverage_fail_threshold, warn under
    coverage_warn_threshold (both configurable in corpus.yml's
    `provenance:` block; default 0.70 / 0.90).

Non-verbatim state-authored docs fail unless frontmatter carries
content_exception or migration_pending (warn instead). external_reference
must be summary. Legacy [VERBATIM] quote blocks are checked wherever present.

  --changed [ref]     verify only files changed vs ref (merge-base origin/main
                      if omitted)
  -j N / --jobs N     parallelize across N processes (default: all CPUs)
"""
import argparse
import multiprocessing as mp
import os

from corpus_toolkit import config as config_mod
from corpus_toolkit.plugins import snapshot_slice_fn
from corpus_toolkit.repo import (
    Reporter, changed_content_files, content_files, extract_fulltext,
    extract_verbatim_quotes, hash_snapshot, normalize_ws, parse_frontmatter,
)

STATE_AUTHORED = {"statute", "rule", "executive_order", "policy", "procedure",
                  "standard", "manual", "schedule", "ordinance",
                  # Audits Division reports: state-authored, and quoted verbatim like any
                  # other. A finding paraphrased is a finding changed.
                  "audit_report"}

_CONFIG = None
_SLICE_FN = None


def _init_worker(config, slice_fn):
    global _CONFIG, _SLICE_FN
    _CONFIG = config
    _SLICE_FN = slice_fn


def quote_in_text(quote: str, text: str) -> bool:
    segments = [normalize_ws(s) for s in quote.split("…")]
    pos = 0
    for seg in segments:
        if not seg:
            continue
        i = text.find(seg, pos)
        if i == -1:
            return False
        pos = i + len(seg)
    return True


def check_file(path):
    """Verify one content file. Returns (rel, findings, checked, fulltexts, quotes)."""
    config, slice_fn = _CONFIG, _SLICE_FN
    rel = path.relative_to(config.root)
    findings = []
    checked = fulltexts = quotes = 0
    try:
        fm, body = parse_frontmatter(path)
    except ValueError as e:
        return rel, [("error", str(e))], 0, 0, 0
    doc_id = fm.get("id")
    snap_id = fm.get("snapshot_id") or doc_id
    fmt = fm.get("source_format", "html")
    raw = config.snapshot_dir / f"{snap_id}.{fmt}"
    txt = config.snapshot_dir / f"{snap_id}.txt"
    hash_only = fm.get("snapshot_policy") == "hash-only"

    if hash_only:
        # Raw source deliberately not committed (e.g. huge image-scan PDFs). If a
        # .txt extraction is committed, the recorded hash is the normalized-text
        # sha256 and full-text checks run against it as usual; with no .txt the
        # recorded raw-byte hash of the uncommitted source cannot be re-verified
        # here — those docs should carry content_exception.
        if txt.is_file():
            import hashlib
            digest = hashlib.sha256(
                normalize_ws(txt.read_text(encoding="utf-8", errors="replace"))
                .encode("utf-8")).hexdigest()
            if digest != fm.get("source_sha256"):
                findings.append(("error", f"source_sha256 mismatch: frontmatter {fm.get('source_sha256')} != committed .txt {digest}"))
            source_text = normalize_ws(txt.read_text(encoding="utf-8", errors="replace"))
        elif fm.get("content_mode") == "verbatim":
            findings.append(("error", "snapshot_policy: hash-only verbatim doc has no committed .txt to verify against"))
            return rel, findings, checked, fulltexts, quotes
        else:
            source_text = ""
    else:
        if not raw.is_file():
            findings.append(("error", f"missing source snapshot {raw.relative_to(config.root)}"))
            return rel, findings, checked, fulltexts, quotes
        digest = hash_snapshot(snap_id, fmt, config.snapshot_dir)
        if digest != fm.get("source_sha256"):
            findings.append(("error", f"source_sha256 mismatch: frontmatter {fm.get('source_sha256')} != snapshot {digest}"))
        source_text = normalize_ws(
            txt.read_text(encoding="utf-8") if txt.is_file()
            else raw.read_text(encoding="utf-8", errors="replace")
        )
    checked = 1

    mode = fm.get("content_mode")
    state_authored = fm.get("doc_type") in STATE_AUTHORED
    excused = bool(fm.get("content_exception") or fm.get("migration_pending"))

    if state_authored and mode != "verbatim":
        msg = f"state-authored doc with content_mode: {mode}"
        if excused:
            findings.append(("warn", msg + " (excused: content_exception/migration_pending)"))
        else:
            findings.append(("error", msg + " — full text required, or add content_exception"))

    if mode == "verbatim":
        ft = extract_fulltext(body)
        if ft is None:
            findings.append(("error", "content_mode: verbatim but no '## Full text' section"))
        else:
            fulltexts = 1
            pos, bad = 0, None
            for line in ft.splitlines():
                nl = normalize_ws(line)
                if not nl:
                    continue
                i = source_text.find(nl, pos)
                if i == -1:
                    bad = nl
                    break
                pos = i + len(nl)
            if bad is not None:
                findings.append(("error", f"full-text line not found in snapshot (in order): \"{bad[:90]}\""))
            raw_txt = (txt.read_text(encoding="utf-8", errors="replace")
                       if txt.is_file() else raw.read_text(encoding="utf-8", errors="replace"))
            denom = len(normalize_ws(slice_fn(doc_id, snap_id, raw_txt))) or 1
            cover = len(normalize_ws(ft)) / denom
            if cover < config.coverage_fail_threshold:
                findings.append(("error", f"full-text coverage {cover:.0%} of source slice (< {config.coverage_fail_threshold:.0%})"))
            elif cover < config.coverage_warn_threshold:
                findings.append(("warn", f"full-text coverage {cover:.0%} of source slice"))

    for q in extract_verbatim_quotes(body):
        quotes += 1
        if not quote_in_text(q, source_text):
            findings.append(("error", f"[VERBATIM] quote not found in snapshot: \"{normalize_ws(q)[:80]}…\""))

    return rel, findings, checked, fulltexts, quotes


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--changed", nargs="?", const="", metavar="REF",
                    help="verify only files changed vs REF (default: merge-base with origin/main)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="worker processes (default: all CPUs)")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    slice_fn = snapshot_slice_fn(config)

    if args.changed is not None:
        paths = changed_content_files(config, args.changed or None)
        if not paths:
            print("No changed content files to verify.")
            return
        print(f"Verifying {len(paths)} changed file(s).")
    else:
        paths = list(content_files(config))

    r = Reporter()
    checked = quotes = fulltexts = 0
    jobs = max(1, args.jobs)
    if jobs == 1 or len(paths) < 50:
        _init_worker(config, slice_fn)
        results = [check_file(p) for p in paths]
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(jobs, initializer=_init_worker, initargs=(config, slice_fn)) as pool:
            results = list(pool.imap_unordered(check_file, paths, chunksize=64))
    for rel, findings, c, f, q in results:
        for level, msg in findings:
            (r.error if level == "error" else r.warn)(rel, msg)
        checked += c; fulltexts += f; quotes += q

    r.finish(f"OK: {checked} file(s); {fulltexts} full-text section(s) and "
             f"{quotes} legacy quote(s) verified against snapshots.")


if __name__ == "__main__":
    main()
