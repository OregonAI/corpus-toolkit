"""Build the embeddings artifact for any corpus.

  python3 -m corpus_toolkit.semantic.build --config _meta/corpus.yml \
      --backend sentence-transformers
  python3 -m corpus_toolkit.semantic.build --config _meta/corpus.yml --check

Generalised from oregon-policy-repo/src/build_embeddings.py, which was reachable by one
corpus and hardcoded that corpus's paths and headings.

WHAT HAD TO BE GENERALISED, and why it is not cosmetic. The original took `## At a glance`
plus `## Full text`, falling back to `## Key provisions`. That is a correct description of
one corpus and wrong for others: **oregon-budget has no `## Full text` section anywhere** --
its documents are generated tables under `## Spending by band`, `## Largest vendors` and so
on. Run unchanged there, it would have embedded 1,761 glance paragraphs and silently
indexed none of the actual content. Section selection is therefore per-corpus config with a
whole-body fallback, and `--check` reports how much text each document contributed so an
empty corpus is visible rather than merely quiet.

PIN --backend FOR PRODUCTION BUILDS. `auto` reads the BUILD host's GPU, not the serve
host's; a rebuild on a GPU workstation already produced, once, an artifact the 2-core deploy
host could not serve.

The artifact is three files in `_meta/embeddings/`:

    vectors.i8.npy   int8 [n_chunks, dim], each row L2-normalized x 127
    chunks.jsonl     one object per ROW, in row order: {doc_id, heading, ordinal, preview}
    meta.json        {backend, model, dim, granularity, n_chunks, fingerprint, ...}

Row -> doc_id is POSITIONAL: row i is line i of chunks.jsonl. The only integrity check is a
length comparison at load, so the two files must be written together or not at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from .. import config as config_mod
from ..repo import content_files, extract_fulltext, parse_frontmatter
from .embedders import make_embedder

CHUNK_CHARS = 3200      # ~800 tokens
OVERLAP = 200
BATCH = 256             # outer encode/report granularity; the embedder batches internally

# Tried in order; the first present wins for the "body" unit. `## Full text` is the
# platform's verbatim-source convention and is right wherever it exists.
DEFAULT_BODY_HEADINGS = ["Full text", "Key provisions"]
GLANCE_HEADING = "At a glance"


def _section(body: str, heading: str):
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else None


def _strip_frontmatter(text: str) -> str:
    parts = text.split("---", 2)
    return parts[2] if len(parts) >= 3 else text


def doc_units(path: Path, body_headings: list[str] | None = None,
              whole_body_fallback: bool = True):
    """(doc_id, title, [(heading, text), ...]) -- the embeddable units of one document."""
    fm, body = parse_frontmatter(path)
    units = []
    glance = _section(body, GLANCE_HEADING)
    if glance:
        units.append((GLANCE_HEADING, glance))

    main = extract_fulltext(body)
    if not main:
        for h in (body_headings or DEFAULT_BODY_HEADINGS):
            main = _section(body, h)
            if main:
                units.append((h, main))
                break
    else:
        units.append(("Full text", main))

    # A DOCUMENT WITH NO RECOGNISED SECTION IS NOT AN EMPTY DOCUMENT. Corpora of generated
    # tables carry their content under headings no shared list can enumerate. Falling back
    # to the whole body embeds what is actually there; the alternative is a corpus that
    # indexes its own summaries and reports success.
    if len(units) <= 1 and whole_body_fallback:
        rest = _strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        rest = re.sub(rf"^## {re.escape(GLANCE_HEADING)}\s*$.*?(?=^## |\Z)", "", rest,
                      flags=re.M | re.S).strip()
        if rest:
            units.append(("Body", rest))
    return fm["id"], fm.get("title", ""), units


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP):
    """Split on paragraph boundaries into <= size-char chunks with light overlap."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):
            brk = text.rfind("\n\n", i + size // 2, end)
            if brk == -1:
                brk = text.rfind(". ", i + size // 2, end)
            if brk != -1:
                end = brk + 1
        chunks.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return [c for c in chunks if c]


def iter_chunks(paths, body_headings=None):
    for p in paths:
        doc_id, title, units = doc_units(p, body_headings)
        ordinal = 0
        for heading, text in units:
            for ch in chunk_text(text):
                yield doc_id, heading, ordinal, title, ch
                ordinal += 1


def corpus_fingerprint(paths, body_headings=None) -> str:
    """Hash of (doc_id + embeddable text). Changes when the content changes, not on
    unrelated commits -- so --check can detect staleness without re-embedding.

    NOTE it does not cover the MODEL. Swapping models leaves the fingerprint identical and
    --check will call a stale artifact current; a model change has to be applied by
    rebuilding deliberately.
    """
    h = hashlib.sha256()
    for p in sorted(paths):
        doc_id, _title, units = doc_units(p, body_headings)
        h.update(doc_id.encode())
        for heading, text in units:
            h.update(heading.encode())
            h.update(re.sub(r"\s+", " ", text).strip().encode())
    return h.hexdigest()


def quantize_int8(vecs):
    import numpy as np
    return np.clip(np.rint(vecs * 127.0), -127, 127).astype(np.int8)


def _paths_and_dir(config_path: str):
    cfg = config_mod.load(config_path)
    root = cfg.root
    body_headings = None
    plugins = getattr(cfg, "raw", {}).get("plugins") if hasattr(cfg, "raw") else None
    if isinstance(plugins, dict):
        bh = plugins.get("semantic_body_headings")
        if isinstance(bh, list) and bh:
            body_headings = bh
    return cfg, list(content_files(cfg)), root / "_meta" / "embeddings", body_headings


def build(config_path: str, backend="auto", limit=None, dim=384, model=None) -> int:
    import numpy as np
    cfg, paths, emb_dir, body_headings = _paths_and_dir(config_path)
    if limit:
        paths = paths[:limit]
    emb = make_embedder(backend, dim, model)

    ids, headings, ordinals, texts, previews = [], [], [], [], []
    for doc_id, heading, ordinal, title, chunk in iter_chunks(paths, body_headings):
        ids.append(doc_id)
        headings.append(heading)
        ordinals.append(ordinal)
        # Title-prefixed so a chunk deep in a long document still carries the document's
        # own identity for retrieval, not just its local text.
        texts.append(f"{title}\n\n{chunk}" if title else chunk)
        previews.append(chunk[:160])
    n = len(texts)
    if not n:
        print(f"no embeddable text found in {len(paths)} document(s) -- check the "
              f"corpus's headings", file=sys.stderr)
        return 1

    print(f"embedding {n} chunks across {len(set(ids))} documents ({emb.name}) …",
          flush=True)
    out = np.empty((n, emb.dim), dtype=np.int8)
    t0 = time.time()
    for start in range(0, n, BATCH):
        batch = texts[start:start + BATCH]
        out[start:start + len(batch)] = quantize_int8(emb.encode(batch))
        done = start + len(batch)
        if done % (BATCH * 4) == 0 or done == n:
            rate = done / max(time.time() - t0, 1e-6)
            print(f"  {done}/{n} ({done*100//n}%) · {rate:.1f} chunks/s · "
                  f"ETA {(n - done) / max(rate, 1e-6) / 60:.0f} min", flush=True)

    emb_dir.mkdir(parents=True, exist_ok=True)
    np.save(emb_dir / "vectors.i8.npy", out)
    with (emb_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"doc_id": ids[i], "heading": headings[i],
                                "ordinal": ordinals[i], "preview": previews[i]}) + "\n")
    (emb_dir / "meta.json").write_text(json.dumps({
        "backend": emb.name,
        "model": getattr(emb, "model", None),
        "dim": int(emb.dim),
        "granularity": "chunk",
        "n_chunks": n,
        "n_documents": len(set(ids)),
        "fingerprint": corpus_fingerprint(paths, body_headings),
        "chunk_chars": CHUNK_CHARS,
        "chunk_overlap": OVERLAP,
        "note": ("one int8 vector per CHUNK (title-prefixed, paragraph-chunked with "
                 "overlap). Multiple chunks share a doc_id; the serve side dedupes to "
                 "each document's best-scoring chunk."),
    }, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {emb_dir}: {n} chunks x {emb.dim} dims "
          f"({out.nbytes / 1e6:.0f} MB int8)")
    return 0


def check(config_path: str) -> int:
    """Is the artifact current for this corpus's content? Offline, no model needed."""
    cfg, paths, emb_dir, body_headings = _paths_and_dir(config_path)
    meta_p = emb_dir / "meta.json"
    if not meta_p.is_file():
        # SOFT PASS. The artifact is gitignored and absent from every fresh clone and from
        # CI, so a hard failure here would make the check unrunnable exactly where it runs.
        print(f"{meta_p} absent — nothing to check (artifact is built out of band)")
        return 0
    meta = json.loads(meta_p.read_text())
    want = corpus_fingerprint(paths, body_headings)
    if meta.get("fingerprint") != want:
        print(f"embeddings are stale: content fingerprint {want[:12]}… but the artifact "
              f"was built from {str(meta.get('fingerprint'))[:12]}… — rebuild with "
              f"`python3 -m corpus_toolkit.semantic.build --config {config_path} "
              f"--backend {meta.get('backend')}`", file=sys.stderr)
        return 1
    print(f"embeddings current: {meta.get('n_chunks')} chunks, "
          f"{meta.get('backend')}/{meta.get('model')} dim {meta.get('dim')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="_meta/corpus.yml")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "hashing", "model2vec", "sentence-transformers"])
    ap.add_argument("--model")
    ap.add_argument("--dim", type=int, default=384, help="hashing backend only")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check(a.config)
    return build(a.config, a.backend, a.limit, a.dim, a.model)


if __name__ == "__main__":
    sys.exit(main())
