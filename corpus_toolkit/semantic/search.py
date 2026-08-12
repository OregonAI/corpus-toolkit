"""The `semantic_search_module` plugin: vector search over a prebuilt embeddings artifact.

Declare it in a corpus's `_meta/corpus.yml`:

    plugins:
      semantic_search_module: "corpus_toolkit.semantic.search"

`plugins.load_module` looks for `<root>/corpus_toolkit/semantic/search.py` first and falls
back to `importlib.import_module`, so an installed-package path resolves without a per-repo
shim file. Seven corpora share this module rather than seven copies drifting apart.

THE CONTRACT. `CorpusFramework` calls `make(config)` when the loaded module exposes it and
uses the returned object; otherwise it duck-types the MODULE itself, which is what a
corpus-supplied semantic module written before `make` existed looks like. Either way the
query surface is the same three methods, and only the first two are required:

    available() -> bool
    rank(query, want) -> [doc_id]
    rank_chunks(query, want) -> [{doc_id, ordinal, heading, preview, score}]

Note what `corpus_toolkit.mcp.backends` does with them: `rank` is called with a POOL size
(max(limit*4, 40)), not the caller's limit; it must not apply doc_type/issuing_body filters,
which the backend applies afterwards; and its results are fused with BM25 by reciprocal
rank, so only the ORDER matters, never the scores.

WHY `make(config)` (corpus-toolkit#74). The seam used to pass no corpus at all, so this
module had nothing to resolve paths against and nowhere to keep per-corpus state, and
reached for two side channels instead: `artifact_dir()` read `Path.cwd()`, and the loaded
index lived in a MODULE GLOBAL. Two consequences, both silent:

  * a server started outside the repo root found no artifact and served keyword-only;
  * two corpora in one process shared whichever index loaded first, because
    `load_module` hands them the same installed module object.

The builder never had this problem — `semantic/build.py` writes to `cfg.root/_meta/
embeddings` and takes an explicit `--out`. The two halves of one artifact simply disagreed
about where it lives, and only the reader could be wrong without saying so.

FAILURE IS SILENT BY DESIGN AND THAT IS THE SHARP EDGE. Every load error is swallowed here
and `available()` returns False, at which point search_corpus serves keyword-only with no
field in the response saying so. A missing mount looks exactly like working search with
worse results. The cause is now printed to stderr on first load, but corpora that enable
this should still assert `available()` in their healthcheck rather than trusting that a
green container means semantic search is running.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .embedders import make_embedder


def artifact_dir(config=None) -> Path:
    """Where this corpus's vectors live.

    Precedence, and the order is deliberate:

      1. `CORPUS_SEMANTIC_DIR` — an operator's explicit answer, e.g. a volume mounted
         somewhere other than the repo. It wins over everything so that no existing
         deployment changes behaviour.
      2. `config.root/_meta/embeddings` — the same path `semantic/build.py` writes to.
         This is the one that was missing: the reader used to guess where the writer had
         already been told to go.
      3. `<cwd>/_meta/embeddings` — the historical default, still correct in the
         containers (WORKDIR is the repo root) and when running from a checkout. Reached
         only by the module-level shims, which have no config to consult.

    Note that a corpus building with `--out` (oregon-legislature keeps a different
    artifact at the default path) still has to set `CORPUS_SEMANTIC_DIR` at serve time —
    the builder's flag and the reader's env var are two ways to say one thing, tracked
    separately.
    """
    env = os.environ.get("CORPUS_SEMANTIC_DIR")
    if env:
        return Path(env)
    root = getattr(config, "root", None)
    if root is not None:
        return Path(root) / "_meta" / "embeddings"
    return Path.cwd() / "_meta" / "embeddings"


def _load_index(directory: Path):
    """Open one artifact directory, or None if it cannot serve. Never raises."""
    try:
        # The model backends hit huggingface.co to check the model revision on every load
        # unless told not to; the serve side only ever uses a model already cached or
        # mounted locally.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import numpy as np
        d = Path(directory)
        meta = json.loads((d / "meta.json").read_text())
        vecs = np.load(d / "vectors.i8.npy")
        # Full row metadata, not just doc_id: heading/ordinal/preview were computed at
        # build time and then discarded by the serve path for a year — rank_chunks()
        # exists to stop throwing them away (the five biggest federal documents are 92%
        # of that corpus's chunks and used to collapse to five bare doc ids).
        rows = [json.loads(line)
                for line in (d / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
                if line]
        doc_ids = [r["doc_id"] for r in rows]
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
        return (np, vecs, doc_ids, embedder, meta, rows)
    except Exception as e:
        # STILL SWALLOWED -- the caller must degrade to keyword rather than 500 -- but no
        # longer SILENT. Every distinct cause reports `available() -> False` and the response
        # says nothing, so a missing mount, an unreachable encoder, a model-id mismatch and a
        # missing numpy are indistinguishable from outside. Each of those has now cost real
        # debugging time on this platform; the last was a corpus reporting healthy and serving
        # keyword-only because numpy was not installed.
        #
        # The DIRECTORY is named because the commonest cause is looking in the wrong one,
        # and the message that omitted it sent people to check the mount instead.
        print(f"semantic search unavailable ({directory}): {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


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


class SemanticIndex:
    """One corpus's vectors, loaded lazily on first query.

    Lazy because construction happens at server startup, where a corpus that has no
    artifact must not pay a load attempt, and because the failure it would report is only
    actionable once someone searches.
    """

    def __init__(self, directory):
        self._dir = Path(directory)
        self._index = "unset"        # "unset" -> not tried; None -> unavailable; tuple -> loaded

    def _loaded(self):
        if self._index == "unset":
            self._index = _load_index(self._dir)
        return self._index

    def available(self) -> bool:
        return self._loaded() is not None

    def rank_chunks(self, query: str, want: int) -> list[dict]:
        """Best chunk per document, WITH the chunk's identity — [{doc_id, ordinal, heading,
        preview, score}] in similarity order. rank() keeps its bare-ids contract; this is
        the richer sibling the MCP search layer attaches to hits so an agent landing on a
        900 KB statute learns WHERE in it the match lives."""
        idx = self._loaded()
        if not idx:
            return []
        np, vecs, doc_ids, embedder, _meta, rows = idx
        scores = self._scores(np, vecs, embedder, query)
        best: dict[str, dict] = {}
        for i in np.argsort(-scores):
            d = doc_ids[i]
            if d not in best:
                r = rows[int(i)]
                best[d] = {"doc_id": d, "ordinal": r.get("ordinal"),
                           "heading": r.get("heading"), "preview": r.get("preview"),
                           "score": float(scores[i])}
                if len(best) >= want:
                    break
        return sorted(best.values(), key=lambda h: -h["score"])

    def rank(self, query: str, want: int) -> list:
        """Doc ids by best-chunk cosine similarity (empty when the index is unavailable).

        Many chunks share a doc_id; the first hit in similarity order is that document's
        best chunk, so iterating argsort and keeping first-seen gives best-chunk-per-
        document.
        """
        idx = self._loaded()
        if not idx:
            return []
        np, vecs, doc_ids, embedder, _meta, _rows = idx
        scores = self._scores(np, vecs, embedder, query)
        best: dict[str, float] = {}
        for i in np.argsort(-scores):
            d = doc_ids[i]
            if d not in best:
                best[d] = float(scores[i])
                if len(best) >= want:
                    break
        return sorted(best, key=lambda d: -best[d])

    @staticmethod
    def _scores(np, vecs, embedder, query: str):
        # Already float32 unless low-memory mode kept int8, where the astype is the
        # deliberate cost. Both paths are cosine: rows are L2-normalized and the int8 form
        # is scaled by 127, which cancels in the ranking.
        q = embedder.encode([query])[0].astype(np.float32)
        return (vecs @ q) if vecs.dtype == np.float32 else (vecs.astype(np.float32) @ q)


def make(config) -> SemanticIndex:
    """The seam `CorpusFramework` prefers: one index per CORPUS, path from its config."""
    return SemanticIndex(artifact_dir(config))


# --------------------------------------------------------- module-level compatibility

# The pre-#74 surface. Kept so that nothing in any corpus's `_meta/corpus.yml` changes and
# so a caller holding the module itself still works; it resolves its directory from the
# environment or the cwd, because it has no config to ask.
_DEFAULT: SemanticIndex | None = None


def _default() -> SemanticIndex:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SemanticIndex(artifact_dir())
    return _DEFAULT


def available() -> bool:
    return _default().available()


def rank(query: str, want: int) -> list:
    return _default().rank(query, want)


def rank_chunks(query: str, want: int) -> list[dict]:
    return _default().rank_chunks(query, want)


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
    class _E:
        def encode(self, xs):
            r = rng.standard_normal((len(xs), D)).astype(np.float32)
            return r / np.linalg.norm(r, axis=1, keepdims=True)

    def _fixture(mat):
        """A SemanticIndex over pre-loaded synthetic vectors, bypassing the artifact."""
        ix = SemanticIndex("/nonexistent")
        ids = [f"doc-{i // 4}" for i in range(N)]          # 4 chunks per document
        rows = [{"doc_id": d, "ordinal": i % 4, "heading": None, "preview": ""}
                for i, d in enumerate(ids)]
        ix._index = (np, mat, ids, _E(), {"dim": D}, rows)
        return ix

    for label, mat in (("float32 (default)", f32), ("int8 (low-memory)", vi8)):
        got = _fixture(mat).rank("anything", 5)
        if len(got) != 5:
            fails.append(f"{label}: rank() returned {len(got)} docs, want 5")
        if len(set(got)) != len(got):
            fails.append(f"{label}: rank() returned a duplicate doc_id")

    # 3. UNAVAILABLE MUST BE SURVIVABLE, not an exception. backends.py calls
    #    self._semantic.rank(...) with no guard, so a raise here would take down
    #    search_corpus for a corpus whose only fault is a missing mount -- turning a
    #    silent degrade into an outage.
    dead = SemanticIndex("/nonexistent")
    dead._index = None
    if dead.rank("anything", 5) != []:
        fails.append("rank() with no index must return [], not results")
    if dead.rank_chunks("anything", 5) != []:
        fails.append("rank_chunks() with no index must return [], not results")
    if dead.available() is not False:
        fails.append("available() must be False when the index failed to load")

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

    # 5. THE ARTIFACT DIRECTORY COMES FROM THE CORPUS, not the process's cwd
    #    (corpus-toolkit#74). Without this the module resolves paths against wherever it
    #    happens to be running, and a server started one directory up serves keyword-only
    #    while reporting healthy.
    class _Cfg:
        root = Path("/srv/some-corpus")

    os.environ.pop("CORPUS_SEMANTIC_DIR", None)
    if artifact_dir(_Cfg()) != Path("/srv/some-corpus/_meta/embeddings"):
        fails.append("artifact_dir(config) did not resolve against config.root")
    if artifact_dir() != Path.cwd() / "_meta" / "embeddings":
        fails.append("artifact_dir() without a config must keep the historical cwd default")
    os.environ["CORPUS_SEMANTIC_DIR"] = "/mnt/vectors"
    try:
        if artifact_dir(_Cfg()) != Path("/mnt/vectors"):
            fails.append("CORPUS_SEMANTIC_DIR must win over the config — an operator's "
                         "explicit mount is the most specific answer")
    finally:
        os.environ.pop("CORPUS_SEMANTIC_DIR", None)

    total = 8 + 4 + 3 + 2 + 3
    for f in fails:
        print(f"FAIL {f}")
    print(f"semantic search selftest: {total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(selftest())
