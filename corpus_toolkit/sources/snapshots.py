"""Recording a snapshot: the raw bytes, the extracted text, the two hashes, and the baseline.

Every ingester on the platform wrote this sequence by hand -- `_meta/snapshots/<id>.<fmt>`,
`<id>.txt`, `hash_snapshot(...)` for the document's `source_sha256`, and (in two corpora of
nine) the manifest baseline -- and they disagreed about the parts that matter:

  * which hash goes where. The manifest baseline is the DETECTOR's hash,
    `content_hash(raw, fmt, volatile_patterns)`; the document's `source_sha256` is
    `hash_snapshot`, which reads the committed `.txt` and is never re-derived from the
    source at verification time. They agree only for image-only PDFs
    (corpus-toolkit#207). ERF's `snapshot_identity.py` exists to guard the confusion.
  * whether ingest moves the baseline. kpm and federal-reference did; the others left it,
    so the next drift run reported every freshly ingested source as changed -- "a 100%
    false positive" in federal-reference's own words. A baseline is what the mirror HOLDS
    and ingest is what moves it (ADR-0016), so `record_snapshot` moves it, through the one
    writer of that field (`sources.manifest.record_baseline`).
  * when `retrieved` may advance: only when bytes were actually fetched.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import NamedTuple

from corpus_toolkit.repo import content_hash, hash_snapshot, parse_frontmatter
from corpus_toolkit.sources import manifest


class Snapshot(NamedTuple):
    source_id: str
    fmt: str
    raw_path: Path
    text_path: Path | None
    fresh: bool              # the raw bytes are new or changed on disk
    sha256: str              # for the document's `source_sha256` (hash_snapshot)
    content_hash: str        # the drift baseline (content_hash with this corpus's patterns)
    baseline: str            # "written" | "current" | "undeclared" | "skipped"


def record_snapshot(config, source_id: str, raw: bytes, fmt: str, text: str | None = None,
                    *, baseline: bool = True) -> Snapshot:
    """Write `<snapshot_dir>/<id>.<fmt>` (and `<id>.txt` when `text` is given), hash both
    ways, and move the manifest baseline. Returns what was written and computed.

    `fresh` is True when the raw bytes are new or differ from what was on disk; a re-run over
    unchanged bytes writes nothing and reports fresh=False, so `retrieved` can stay put.
    The `.txt` is written when the raw bytes are fresh or no `.txt` exists yet -- the
    committed text is what `hash_snapshot` reads, deliberately never re-derived on a later
    run whose extractor may differ.

    `baseline`: with a manifest entry for `source_id`, its `sha256` is set to the detector's
    hash (result "written", or "current" when it already matched). A snapshot that no group
    declares gets "undeclared" -- not every snapshot is a drift-tracked source. A duplicated
    id or a manifest the line editor cannot account for raises `manifest.BaselineRefused`,
    with nothing written to the manifest; the snapshot files are already on disk by then.
    `baseline=False` skips the manifest entirely ("skipped").

    The baseline hash of a PDF is taken over `pdftotext -layout` output, exactly as the drift
    detector takes it, so `pdftotext` must be installed wherever PDFs are ingested.
    """
    snapshot_dir = Path(config.snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    raw_path = snapshot_dir / f"{source_id}.{fmt}"
    fresh = not (raw_path.is_file() and raw_path.read_bytes() == raw)
    if fresh:
        raw_path.write_bytes(raw)
    text_path: Path | None = None
    if text is not None:
        text_path = snapshot_dir / f"{source_id}.txt"
        if fresh or not text_path.is_file():
            text_path.write_text(text, encoding="utf-8")
    sha = hash_snapshot(source_id, fmt, snapshot_dir)
    chash = content_hash(raw, fmt, getattr(config, "volatile_patterns", ()) or ())
    state = "skipped"
    if baseline:
        try:
            written = manifest.record_baseline(config, source_id, chash)
            state = "written" if written else "current"
        except manifest.UndeclaredSource:
            state = "undeclared"
    return Snapshot(source_id, fmt, raw_path, text_path, fresh, sha, chash, state)


def recorded_retrieved(doc_path: Path | None) -> str | None:
    """The `retrieved:` already published for this document, so it can be carried forward."""
    if doc_path is None or not Path(doc_path).is_file():
        return None
    fm, _ = parse_frontmatter(Path(doc_path))
    value = (fm or {}).get("retrieved")
    return str(value) if value else None


def retrieved_date(fresh: bool, doc_path: Path | None = None,
                   snapshot_path: Path | None = None, today: str | None = None) -> str:
    """The date for `retrieved:` -- from the FETCH, never from the wall clock alone.

    Advances to today only when bytes were actually fetched (`fresh`). Otherwise the
    published date is carried forward; for a document that does not exist yet, the
    snapshot's own mtime is the best available statement of when it was pulled.
    """
    if fresh:
        return today or time.strftime("%Y-%m-%d")
    carried = recorded_retrieved(doc_path)
    if carried:
        return carried
    if snapshot_path is not None and Path(snapshot_path).is_file():
        return time.strftime("%Y-%m-%d", time.localtime(Path(snapshot_path).stat().st_mtime))
    return today or time.strftime("%Y-%m-%d")
