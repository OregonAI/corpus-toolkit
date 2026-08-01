"""Embedding backends, shared by the build side and the serve side.

THE BUILD AND SERVE SIDES MUST AGREE, and this module is how. `make_embedder` is called
once by the builder to encode the corpus and again by the query path to encode a query;
querying vectors with a different model than built them does not degrade gracefully, it
returns confident nonsense. The artifact records `backend` and `model` in meta.json and
the serve side reconstructs from those rather than from its own defaults.

Ported from oregon-policy-repo/src/build_embeddings.py, which was the only implementation
and was reachable by exactly one corpus.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request

DEFAULT_M2V = "minishlab/potion-retrieval-32M"     # static, CPU-fast
DEFAULT_MODEL = "BAAI/bge-m3"                       # transformer, 1024-dim


class HashingEmbedder:
    """Deterministic stdlib+numpy fallback. Hashes 3-5-char n-grams into `dim` buckets
    with sub-linear tf weighting, then L2-normalizes. Reproducible and dependency-light,
    but NOT semantic -- it exists so CI can exercise the code path without a model."""
    name = "hashing"

    def __init__(self, dim=384, model="hashing-ngram-v1"):
        self.dim = dim
        self.model = model or "hashing-ngram-v1"

    def encode(self, texts):
        import numpy as np
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, t in enumerate(texts):
            t = re.sub(r"\s+", " ", t.lower())
            for n in (3, 4, 5):
                for i in range(len(t) - n + 1):
                    g = t[i:i + n]
                    b = int.from_bytes(
                        hashlib.blake2b(g.encode(), digest_size=4).digest(),
                        "little") % self.dim
                    out[r, b] += 1.0
            row = out[r]
            nz = row > 0
            row[nz] = 1.0 + np.log(row[nz])          # sub-linear tf
            out[r] = row / (float(np.linalg.norm(row)) or 1.0)
        return out


class Model2VecEmbedder:
    """Static (model2vec) embeddings -- token-vector lookup + pooling, NO transformer
    forward pass, so it runs ~10,000 texts/sec on CPU. Lower retrieval quality than a
    transformer, but it needs no GPU to build and little RAM to serve."""
    name = "model2vec"

    def __init__(self, model=DEFAULT_M2V):
        import numpy as np
        from model2vec import StaticModel
        self.model = model or DEFAULT_M2V
        self._m = StaticModel.from_pretrained(self.model)
        self.dim = int(self._m.encode(["probe"]).shape[1])
        self._np = np

    def encode(self, texts):
        np = self._np
        v = self._m.encode(list(texts)).astype("float32")
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return v / n


class SentenceTransformerEmbedder:
    """Transformer backend -- higher quality, and what the platform builds with.

    Device-aware: CUDA + fp16 + a larger batch when a GPU is present, fp32/CPU with a
    conservative batch otherwise. max_seq_length is left at the model's own default,
    because chunking already bounds every input and sentence-transformers pads to the
    longest row in a batch rather than to the ceiling.

    RAM IS THE REASON THIS CLASS IS NOT INSTANTIATED PER CORPUS IN PRODUCTION. Measured
    on the deploy host, one loaded bge-m3 plus its vectors is ~2,993 MiB RSS, of which
    ~2,163 MiB is the model. Seven corpora each holding their own copy is ~14.8 GiB
    against a 14.1 GiB host. See RemoteEmbedder.
    """
    name = "sentence-transformers"

    def __init__(self, model=DEFAULT_MODEL):
        import torch
        from sentence_transformers import SentenceTransformer
        self.model = model or DEFAULT_MODEL
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._m = SentenceTransformer(self.model, device=self.device)
        if self.device == "cuda":
            self._m.half()
        get_dim = getattr(self._m, "get_embedding_dimension", None) or \
            self._m.get_sentence_embedding_dimension
        self.dim = get_dim()
        self.batch_size = 64 if self.device == "cuda" else 32

    def encode(self, texts):
        return self._m.encode(list(texts), batch_size=self.batch_size,
                              normalize_embeddings=True,
                              show_progress_bar=False).astype("float32")


class RemoteEmbedder:
    """Encode via a shared encoder service instead of loading the model in-process.

    WHY THIS EXISTS. The model is identical in every corpus; only the vectors differ. A
    per-container SentenceTransformerEmbedder duplicates ~2.16 GiB of weights per corpus,
    which is what confined semantic search to a single corpus on a 14 GiB host. One
    encoder serving N corpora costs the model once and leaves each corpus holding only
    its own matrix -- measured 14.8 GiB down to ~3.7 GiB across seven corpora.

    It also removes a cold-start cliff: the first semantic query against a freshly
    restarted container took 18.5 s on 2 cores, almost all of it loading weights. A warm
    shared encoder pays that once for the stack.

    The service is asked for its model id and dim at construction, and BOTH are checked
    against the artifact by the caller. A remote encoder quietly running a different
    model than the vectors were built with is the one failure this must not have.
    """
    name = "remote"

    def __init__(self, url: str, model: str | None = None, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        info = self._call("/info", None)
        self.model = info.get("model")
        self.dim = int(info["dim"])
        if model and self.model and model != self.model:
            raise ValueError(
                f"encoder at {self.url} serves {self.model!r} but this artifact was "
                f"built with {model!r} -- refusing to encode queries with a different "
                "model than the vectors")

    def _call(self, path: str, payload):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url + path, data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def encode(self, texts):
        import numpy as np
        out = self._call("/encode", {"texts": list(texts)})
        return np.asarray(out["vectors"], dtype=np.float32)


def make_embedder(backend, dim, model=None):
    """Build the embedder for a backend. `model` is honoured so the serve side
    reconstructs the SAME model the artifact was built with."""
    if backend == "hashing":
        return HashingEmbedder(dim=dim, model=model or "hashing-ngram-v1")
    if backend == "model2vec":
        return Model2VecEmbedder(model)
    if backend == "sentence-transformers":
        # A shared encoder replaces the in-process model when one is configured. The
        # artifact still says `sentence-transformers`, because the BACKEND is unchanged --
        # only where the weights live moves.
        url = os.environ.get("CORPUS_SEMANTIC_ENCODER_URL")
        if url:
            return RemoteEmbedder(url, model)
        return SentenceTransformerEmbedder(model)
    # auto: prefer the transformer when a GPU is available, else the static model.
    #
    # PIN THE BACKEND FOR PRODUCTION BUILDS. This auto-selection reads the BUILD host's
    # GPU, not the serve host's, and that mismatch already caused one outage: a rebuild on
    # a GPU workstation silently produced an artifact the 2-core deploy host could not
    # serve.
    try:
        import torch
        gpu = torch.cuda.is_available()
    except ImportError:
        gpu = False
    ctors = [lambda: SentenceTransformerEmbedder(model), lambda: Model2VecEmbedder(model)]
    if not gpu:
        ctors.reverse()
    for ctor in ctors:
        try:
            return ctor()
        except Exception:
            continue
    print("note: no embedding model available; using the hashing fallback (no semantic "
          "quality).", file=sys.stderr)
    return HashingEmbedder(dim=dim)
