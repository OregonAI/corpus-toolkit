"""The `semantic_search_module` plugin: vector search over a prebuilt embeddings artifact.

Declare it in a corpus's `_meta/corpus.yml`:

    plugins:
      semantic_search_module: "corpus_toolkit.semantic.search"

`plugins.load_module` looks for `<root>/corpus_toolkit/semantic/search.py` first and falls
back to `importlib.import_module`, so an installed-package path resolves without a per-repo
shim file. Seven corpora share this module rather than seven copies drifting apart.

THE CONTRACT IS TWO FUNCTIONS, duck-typed by corpus_toolkit.mcp.backends at query time:
`available() -> bool` and `rank(query, want) -> [doc_id]`. Note what the backend does with
them -- `rank` is called with a POOL size (max(limit*4, 40)), not the caller's limit; it
must not apply doc_type/issuing_body filters, which the backend applies afterwards; and its
results are fused with BM25 by reciprocal rank, so only the ORDER matters, never the scores.

FAILURE IS SILENT BY DESIGN AND THAT IS THE SHARP EDGE. Every load error is swallowed here
and `available()` returns False, at which point search_corpus serves keyword-only with no
field in the response saying so. A missing mount looks exactly like working search with
worse results. Corpora that enable this should assert `available()` in their healthcheck
rather than trusting that a green container means semantic search is running.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .embedders import make_embedder

_SEM = "unset"      # "unset" -> not tried; None -> unavailable; tuple -> loaded


def artifact_dir() -> Path:
    """Where the vectors live.

    Defaults to `<cwd>/_meta/embeddings`, which is correct in the containers (WORKDIR is
    the repo root) and when running from a checkout. `CORPUS_SEMANTIC_DIR` overrides it --
    needed because this module is shared, so it cannot resolve paths relative to its own
    file the way a per-repo copy did.
    """
    env = os.environ.get("CORPUS_SEMANTIC_DIR")
    return Path(env) if env else Path.cwd() / "_meta" / "embeddings"


def _semantic_index():
    global _SEM
    if _SEM != "unset":
        return _SEM
    try:
        # The model backends hit huggingface.co to check the model revision on every load
        # unless told not to; the serve side only ever uses a model already cached or
        # mounted locally.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import numpy as np
        d = artifact_dir()
        meta = json.loads((d / "meta.json").read_text())
        vecs = np.load(d / "vectors.i8.npy")
        doc_ids = [json.loads(line)["doc_id"]
                   for line in (d / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
                   if line]
        if vecs.shape[0] != len(doc_ids):
            raise ValueError("vectors/chunks length mismatch")

        # The artifact records the model by HUB ID, which sentence-transformers resolves
        # against the HF cache. In a container the weights are volume-mounted at a path
        # with no cache and no network, so the hub id would fail to resolve.
        # CORPUS_SEMANTIC_MODEL_PATH overrides it with a local directory.
        #
        # A SUBSTITUTION OF LOCATION ONLY, never of model: query vectors must come from the
        # same model as the artifact or the scores are meaningless rather than merely
        # worse. The dim probe below is the backstop.
        model_ref = meta.get("model")
        local = os.environ.get("CORPUS_SEMANTIC_MODEL_PATH")
        if local and meta["backend"] == "sentence-transformers" and Path(local).is_dir():
            model_ref = local
        embedder = make_embedder(meta["backend"], meta["dim"], model_ref)

        # A dim mismatch means the query encoder and the vectors disagree, which produces
        # confident nonsense rather than an error. Fail closed to keyword-only instead.
        probe = embedder.encode(["dimension probe"])[0]
        if len(probe) != meta["dim"]:
            raise ValueError(
                f"query encoder produces dim {len(probe)} but the artifact is dim "
                f"{meta['dim']} -- refusing to serve semantic search on mismatched vectors")

        # CONVERT ONCE, NOT PER QUERY. The artifact is int8 on disk and rank() used to do
        # `vecs.astype(np.float32) @ q`, rebuilding an 825 MiB float32 copy on EVERY call.
        # Measured on 211,102 x 1024:
        #
        #   astype(f32) @ q per query   277-301 ms
        #   f32 resident @ q              9.2 ms      <- 30x, +825 MiB RSS
        #   f16 resident @ q            443.2 ms      SLOWER than doing nothing
        #   int16 dot                   209.4 ms
        #
        # float16 is not a memory/speed tradeoff, it is a loss: numpy has no float16 GEMM
        # so it upcasts internally, paying the conversion anyway on a layout BLAS likes
        # less. Recorded because it is the obvious next idea and it is wrong. Blocked
        # matmul was measured too and does not help -- the cost is memory bandwidth, not
        # allocation.
        #
        # CORPUS_SEMANTIC_LOW_MEMORY=1 keeps int8 resident and pays per query instead.
        vecs = prepare_vectors(np, vecs)
        _SEM = (np, vecs, doc_ids, embedder, meta)
    except Exception:
        _SEM = None
    return _SEM


def prepare_vectors(np, vecs):
    """int8 artifact -> the resident form rank() multiplies against.

    Extracted so selftest() can reach it. Inline in the loader it could not be tested at
    all -- the loader needs the artifact, which is gitignored and absent from CI. A
    selftest that asserts the maths but cannot see whether the loader still converts is a
    guard that passes with the optimisation deleted; that happened on the first draft.
    """
    if os.environ.get("CORPUS_SEMANTIC_LOW_MEMORY") == "1":
        print("semantic: low-memory mode, ~280 ms/query", file=sys.stderr)
        return vecs
    return np.ascontiguousarray(vecs, dtype=np.float32)


def available() -> bool:
    return _semantic_index() is not None


def rank(query: str, want: int) -> list:
    """Doc ids by best-chunk cosine similarity (empty when the index is unavailable).

    Many chunks share a doc_id; the first hit in similarity order is that document's best
    chunk, so iterating argsort and keeping first-seen gives best-chunk-per-document.
    """
    idx = _semantic_index()
    if not idx:
        return []
    np, vecs, doc_ids, embedder, _meta = idx
    q = embedder.encode([query])[0].astype(np.float32)
    # Already float32 unless low-memory mode kept int8, where the astype is the deliberate
    # cost. Both paths are cosine: rows are L2-normalized and the int8 form is scaled by
    # 127, which cancels in the ranking.
    scores = (vecs @ q) if vecs.dtype == np.float32 else (vecs.astype(np.float32) @ q)
    best: dict[str, float] = {}
    for i in np.argsort(-scores):
        d = doc_ids[i]
        if d not in best:
            best[d] = float(scores[i])
            if len(best) >= want:
                break
    return sorted(best, key=lambda d: -best[d])


# --------------------------------------------------------------------------- selftest

def selftest() -> int:
    """Prove the convert-once optimisation is behaviour-preserving, on synthetic vectors.

    Runs without the artifact, which is gitignored and absent from a fresh clone -- so this
    is checkable in CI, where the real index will never exist.

    The property that matters is NOT that float32 is faster. It is that converting once at
    load cannot change what the corpus returns. int8*127 and its float32 copy differ by a
    positive scale factor, which cannot reorder a ranking -- but "cannot" is worth
    asserting, because if it were ever false every answer the semantic arm gives would
    change and nothing would report it.
    """
    import numpy as np
    fails = []
    rng = np.random.default_rng(11)
    N, D = 4000, 128
    v = rng.standard_normal((N, D), dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    vi8 = np.round(v * 127).astype(np.int8)
    f32 = np.ascontiguousarray(vi8, dtype=np.float32)

    # 1. Identical ranking, both paths, over several queries.
    for k in range(8):
        q = rng.standard_normal(D).astype(np.float32)
        q /= np.linalg.norm(q)
        old = np.argsort(-(vi8.astype(np.float32) @ q))[:100]
        new = np.argsort(-(f32 @ q))[:100]
        if not np.array_equal(old, new):
            fails.append(f"query {k}: converting at load reordered the top 100")

    # 2. rank() must work whichever dtype the index holds, because low-memory mode keeps
    #    int8. A dtype check that only ever saw float32 would let the fallback rot.
    global _SEM
    saved = _SEM
    try:
        class _E:
            def encode(self, xs):
                r = rng.standard_normal((len(xs), D)).astype(np.float32)
                return r / np.linalg.norm(r, axis=1, keepdims=True)
        ids = [f"doc-{i // 4}" for i in range(N)]          # 4 chunks per document
        for label, mat in (("float32 (default)", f32), ("int8 (low-memory)", vi8)):
            _SEM = (np, mat, ids, _E(), {"dim": D})
            got = rank("anything", 5)
            if len(got) != 5:
                fails.append(f"{label}: rank() returned {len(got)} docs, want 5")
            if len(set(got)) != len(got):
                fails.append(f"{label}: rank() returned a duplicate doc_id")

        # 3. UNAVAILABLE MUST BE SURVIVABLE, not an exception. backends.py calls
        #    self._semantic.rank(...) with no guard, so a raise here would take down
        #    search_corpus for a corpus whose only fault is a missing mount -- turning a
        #    silent degrade into an outage.
        _SEM = None
        if rank("anything", 5) != []:
            fails.append("rank() with no index must return [], not results")
        if available() is not False:
            fails.append("available() must be False when the index failed to load")
    finally:
        _SEM = saved

    # 4. THE LOADER MUST ACTUALLY CONVERT. The checks above assert the maths and the dtype
    #    fallback; none notices if prepare_vectors() stops converting, which IS the bug.
    #    The first draft of this selftest passed with the optimisation reverted.
    os.environ.pop("CORPUS_SEMANTIC_LOW_MEMORY", None)
    if prepare_vectors(np, vi8).dtype != np.float32:
        fails.append("prepare_vectors() left the matrix int8 by default -- every query "
                     "pays the ~280 ms conversion again")
    os.environ["CORPUS_SEMANTIC_LOW_MEMORY"] = "1"
    try:
        if prepare_vectors(np, vi8).dtype != np.int8:
            fails.append("CORPUS_SEMANTIC_LOW_MEMORY=1 still converted; the memory opt-out "
                         "does nothing and a small host pays 825 MiB anyway")
    finally:
        os.environ.pop("CORPUS_SEMANTIC_LOW_MEMORY", None)

    total = 8 + 4 + 2 + 2
    for f in fails:
        print(f"FAIL {f}")
    print(f"semantic search selftest: {total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(selftest())
