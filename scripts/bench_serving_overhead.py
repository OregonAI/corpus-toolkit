#!/usr/bin/env python3
"""Local benchmark for the three fixed-overhead hot paths named in corpus-toolkit#207:

  1. `ensure_index()` -> `repo_state()` (two `git` subprocesses per call)
  2. `resolve_citation()` -> `_resolve_in_sibling()` -> `load_sibling_index()`
     (re-parses the cached sibling index's JSON per call)
  3. `corpus_overview()` (re-sums the whole graph's edge count per call)

Builds a small fixture corpus on disk (a real git repo, a sibling index, and a graph
with a few thousand edges — the shape, not the scale, of ERF's), warms the server once,
then times N SEQUENTIAL calls to each hot path and reports the mean per-call overhead in
milliseconds. Run it on this worktree, then again on `origin/main` (e.g. from a second
`git worktree add`), to see the before/after numbers quoted in the fix's PR/commit.

    python3 scripts/bench_serving_overhead.py [N]   # default N=200
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit.config import load as load_config          # noqa: E402
from corpus_toolkit.mcp.framework import (                      # noqa: E402
    CorpusFramework, clear_schemes, register_scheme,
)

DOC = """\
---
schema_version: 1
id: ors-{n}.010
title: "Statute {n}"
doc_type: statute
citation: "ORS {n}.010"
authority_level: statute
issuing_body: "Test Body"
agency: statewide
source_url: "https://example.invalid/ors-{n}.010"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: "2026-07-26"
verified_by: "@test"
tags: ["ors"]
---

## At a glance

Statute {n}.

## Full text

Statute {n} full text, long enough to be realistic. Permits, fees, water rights.
"""

SIBLING_INDEX = {
    "corpus": "sibling-corpus",
    "contract_version": 1,
    "n_documents": 500,
    "documents": {
        f"sib-doc-{i}": [f"Sibling Document {i}", "administrative-rule",
                        f"rules/sib-{i}.md"]
        for i in range(500)
    },
}


def build_fixture(root: Path, n_docs: int = 50, n_edges: int = 3000) -> Path:
    (root / "statutes").mkdir(parents=True)
    (root / "_meta").mkdir()
    for i in range(n_docs):
        (root / "statutes" / f"ors-{i}.010.md").write_text(
            DOC.format(n=i, sha="a" * 64))

    nodes = [{"id": f"ors-{i}.010", "title": f"Statute {i}", "doc_type": "statute"}
             for i in range(n_docs)]
    edges = [{"from": f"ors-{i % n_docs}.010", "to": f"ors-{(i + 1) % n_docs}.010",
             "type": "references"} for i in range(n_edges)]
    (root / "_meta" / "graph.json").write_text(json.dumps({"nodes": nodes, "edges": edges}))

    sib_idx = root / "_meta" / "sibling-index.json"
    sib_idx.write_text(json.dumps(SIBLING_INDEX))

    (root / "_meta" / "corpus.yml").write_text(textwrap.dedent(f"""
        corpus:
          id: bench-corpus
          name: Bench Corpus
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: statutes
            doc_type: statute
        graph_path: _meta/graph.json
        siblings:
          - id: sibling-corpus
            index_path: {sib_idx}
            web_base: https://example.invalid/blob/main/
    """).strip() + "\n")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "bench fixture"], cwd=root, check=True)
    return root / "_meta" / "corpus.yml"


def time_calls(label: str, fn, n: int) -> None:
    fn()                                   # warm: pay any one-time cost first
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    elapsed = time.perf_counter() - t0
    per_call_ms = (elapsed / n) * 1000
    print(f"{label:<45} {per_call_ms:8.3f} ms/call  ({n} sequential calls, "
          f"{elapsed:.3f}s total)")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    tmp = Path(tempfile.mkdtemp(prefix="corpus-bench-"))
    try:
        clear_schemes()
        register_scheme("ors-section", r"ORS\s+(?P<num>\d+\.\d+)", "sib-doc-{num}",
                        corpus="sibling-corpus")
        cfg_path = build_fixture(tmp)
        f = CorpusFramework(load_config(cfg_path))

        print(f"Fixture: {tmp}  (N={n} sequential calls per hot path)\n")
        time_calls("ensure_index() [repo_state x2 git]", f.ensure_index, n)
        time_calls("resolve_citation() [sibling index]",
                   lambda: f.resolve_citation("ORS 1.010"), n)
        time_calls("corpus_overview() [graph edge count]", f.corpus_overview, n)
    finally:
        clear_schemes()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
