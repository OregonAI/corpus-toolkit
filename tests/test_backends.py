"""The retrieval seam: protocol conformance, backend selection, and the guarantee that
introducing it changed nothing for file-backed corpora.

The parity guarantee is the important one. `FileBackend` is a pure refactor of code that
previously lived inline in `CorpusFramework`, and the FTS schema is load-bearing in ways
that do not fail loudly: `snippet(fts, 5, ...)` addresses the `body` column BY INDEX, so
reordering the virtual table returns the wrong text rather than an error. These tests
build a real corpus on disk and assert on actual responses.
"""
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp.backends import FileBackend, RetrievalBackend
from corpus_toolkit.mcp.framework import CorpusFramework

DOC = """---
schema_version: 1
id: {id}
title: "{title}"
doc_type: {doc_type}
citation: "{citation}"
authority_level: statute
issuing_body: "Test Body"
agency: statewide
source_url: "https://example.invalid/{id}"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: "2026-07-26"
verified_by: "@test"
tags: ["{tag}"]
---

## At a glance

{glance}

## Full text

{body}
"""


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A minimal but REAL corpus: git repo, config, content files. The FTS index keys on
    `git rev-parse HEAD`, so an un-inited directory would rebuild on every call and mask
    caching bugs."""
    (tmp_path / "statutes").mkdir()
    (tmp_path / "_meta").mkdir()
    docs = [
        dict(id="ors-1.010", title="Definitions for water rights", doc_type="statute",
             citation="ORS 1.010", sha="a" * 64, tag="ors",
             glance="Defines terms used for water appropriation.",
             body="A person may not appropriate water without a permit issued by the department."),
        dict(id="ors-2.020", title="Permit application fees", doc_type="statute",
             citation="ORS 2.020", sha="b" * 64, tag="ors",
             glance="Sets the fee schedule.",
             body="The fee for a permit application is $250 payable at the time of filing."),
    ]
    for d in docs:
        (tmp_path / "statutes" / f"{d['id']}.md").write_text(DOC.format(**d))
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: test-corpus
          name: Test Corpus
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: statutes
            doc_type: statute
        graph_path: _meta/graph.json
    """).strip() + "\n")
    (tmp_path / "_meta" / "graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def fw(corpus: Path) -> CorpusFramework:
    return CorpusFramework(load_config(str(corpus / "_meta" / "corpus.yml")))


# ---------- protocol ----------

def test_file_backend_satisfies_the_protocol(corpus):
    assert isinstance(FileBackend(load_config(str(corpus / "_meta" / "corpus.yml"))),
                      RetrievalBackend)


def test_default_backend_is_the_file_backend(corpus):
    assert fw(corpus).backend.name == "file"


# ---------- parity: retrieval still works through the seam ----------

def test_search_finds_a_document_and_keeps_the_hit_shape(corpus):
    hits = fw(corpus).search_corpus("appropriate water permit", limit=5)
    assert [h["id"] for h in hits] == ["ors-1.010"]
    assert set(hits[0]) == {"id", "title", "citation", "doc_type",
                            "issuing_body", "path", "snippet"}


def test_snippet_comes_from_the_body_column(corpus):
    """Guards the FTS column-index coupling: snippet(fts, 5, ...) must address `body`.
    A reordered virtual table would return a title or tag here instead."""
    hits = fw(corpus).search_corpus("permit issued department", limit=1)
    assert "appropriate water" in hits[0]["snippet"] or "permit" in hits[0]["snippet"]
    assert "[" in hits[0]["snippet"]        # the match markers snippet() inserts


def test_get_document_carries_the_corpus_envelope(corpus):
    d = fw(corpus).get_document("ors-2.020")
    assert d["id"] == "ors-2.020"
    assert "$250" in d["body"]
    # added by CorpusFramework, not the backend — every archetype must get these
    assert d["corpus"] == "test-corpus"
    assert d["archetype"] == "document"
    assert "NON-AUTHORITATIVE" in d["disclaimer"]
    assert d["source_url"] == "https://example.invalid/ors-2.020"


def test_missing_document_suggests_alternatives(corpus):
    d = fw(corpus).get_document("ors-9.999")
    assert "error" in d and "did_you_mean" in d


def test_get_section_by_heading(corpus):
    d = fw(corpus).get_document("ors-1.010", part="At a glance")
    assert d["section"] == "At a glance"
    assert "water appropriation" in d["body"]


def test_overview_counts_documents(corpus):
    o = fw(corpus).corpus_overview()
    assert o["documents_by_type"] == {"statute": 2}
    assert o["corpus"] == "test-corpus" and o["archetype"] == "document"


# ---------- health: the failure mode that used to be silent ----------

def test_health_reports_a_populated_corpus(corpus):
    h = fw(corpus).backend.health()
    assert h["reachable"] is True and "2 document(s)" in h["detail"]


def test_empty_corpus_is_unreachable_not_merely_empty(tmp_path):
    """The behaviour this seam exists to fix. A misconfigured corpus used to answer
    'nothing found', which a caller cannot tell apart from a genuine no-match."""
    (tmp_path / "statutes").mkdir()
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: empty
          name: Empty
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: statutes
            doc_type: statute
    """).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    h = CorpusFramework(load_config(str(tmp_path / "_meta" / "corpus.yml"))).backend.health()
    assert h["reachable"] is False
    assert "EMPTY" in h["detail"]


# ---------- custom backends ----------

class _StubBackend:
    """A non-file backend, as an API archetype would supply."""
    name = "stub"

    def __init__(self, config, semantic=None):
        self.config = config

    def search(self, query, *, doc_type=None, issuing_body=None, limit=10, mode="hybrid"):
        return [{"id": "stub:1", "title": "Stub", "citation": "STUB 1",
                 "doc_type": "record", "issuing_body": "", "path": "", "snippet": query}]

    def get(self, doc_id, *, part="auto"):
        return {"id": doc_id, "title": "Stub", "body": "live", "executed_at": "2026-07-26T00:00:00Z"}

    def exists(self, doc_id):
        return {"id": doc_id, "title": "Stub", "doc_type": "record"}

    def overview(self):
        return {"documents_by_type": {"record": 1}, "commit": ""}

    def health(self):
        return {"reachable": True, "checked_at": "2026-07-26T00:00:00Z", "detail": "stub"}


class _BrokenBackend(_StubBackend):
    """Missing health() — the kind of adapter bug that must fail at startup."""
    health = None


def _with_backend(corpus: Path, factory_path: str) -> CorpusFramework:
    cfg_path = corpus / "_meta" / "corpus.yml"
    cfg_path.write_text(cfg_path.read_text() + textwrap.dedent(f"""
        plugins:
          retrieval_module: "{factory_path}"
    """))
    (corpus / "backend_mod.py").write_text(
        "from tests.test_backends import _StubBackend, _BrokenBackend\n")
    return CorpusFramework(load_config(str(cfg_path)))


def test_custom_backend_replaces_file_retrieval(corpus, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_StubBackend")
    assert f.backend.name == "stub"
    assert f.search_corpus("anything")[0]["id"] == "stub:1"
    d = f.get_document("stub:1")
    assert d["body"] == "live"
    # the envelope is still applied — a custom backend cannot drop the disclaimer
    assert "NON-AUTHORITATIVE" in d["disclaimer"] and d["corpus"] == "test-corpus"


def test_incomplete_backend_fails_at_startup(corpus):
    """Better a TypeError when the server boots than an AttributeError on the first
    query — for a search tool the latter looks like an empty corpus."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    with pytest.raises(TypeError, match="RetrievalBackend"):
        _with_backend(corpus, "backend_mod:_BrokenBackend")


def test_ensure_index_is_meaningless_without_files(corpus):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_StubBackend")
    with pytest.raises(AttributeError, match="not file-backed"):
        f.ensure_index()


# ---------- G1: per-doc_type heading selection ----------

MULTI = """---
schema_version: 1
id: {id}
title: "T"
doc_type: {doc_type}
citation: "C"
authority_level: statute
issuing_body: "B"
agency: statewide
source_url: "https://example.invalid/x"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: "2026-07-26"
verified_by: "@t"
tags: []
---

## At a glance

GLANCEWORD

## Summary

SUMMARYWORD

## Full text

FULLTEXTWORD

## Provenance & change history

PROVENANCEWORD
"""


def _corpus_with(tmp_path, doc_type, index_headings=None):
    (tmp_path / "docs").mkdir()
    (tmp_path / "_meta").mkdir()
    (tmp_path / "docs" / "d1.md").write_text(
        MULTI.format(id="d1", doc_type=doc_type, sha="c" * 64))
    cfg = textwrap.dedent(f"""
        corpus:
          id: t
          name: T
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: docs
            doc_type: {doc_type}
    """).strip() + "\n"
    if index_headings:
        cfg += "index_headings:\n"
        for dt, hs in index_headings.items():
            cfg += f"  {dt}: [{', '.join(repr(h) for h in hs)}]\n"
    (tmp_path / "_meta" / "corpus.yml").write_text(cfg)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return CorpusFramework(load_config(str(tmp_path / "_meta" / "corpus.yml")))


def _body_col(fw):
    con = fw.backend.ensure_index()
    return con.execute("SELECT body FROM fts WHERE id='d1'").fetchone()[0]


def test_unconfigured_doc_type_keeps_historical_behaviour(tmp_path):
    """The guarantee the whole option was chosen for: a corpus that does not set
    index_headings indexes exactly as before — '## Full text' only."""
    body = _body_col(_corpus_with(tmp_path, "statute"))
    assert "FULLTEXTWORD" in body
    assert "SUMMARYWORD" not in body        # NOT indexed, as before
    assert "PROVENANCEWORD" not in body


def test_configured_doc_type_indexes_the_named_sections(tmp_path):
    fw = _corpus_with(tmp_path, "dataset_doc",
                      {"dataset_doc": ["Summary", "Full text"]})
    body = _body_col(fw)
    assert "SUMMARYWORD" in body and "FULLTEXTWORD" in body
    assert "PROVENANCEWORD" not in body     # boilerplate still excluded


def test_configured_headings_are_concatenated_in_order(tmp_path):
    fw = _corpus_with(tmp_path, "dataset_doc",
                      {"dataset_doc": ["Summary", "Full text"]})
    body = _body_col(fw)
    assert body.index("SUMMARYWORD") < body.index("FULLTEXTWORD")


def test_config_for_one_doc_type_does_not_affect_another(tmp_path):
    """Blast-radius guard: configuring measures must not change how statutes index."""
    fw = _corpus_with(tmp_path, "statute", {"dataset_doc": ["Summary"]})
    body = _body_col(fw)
    assert "FULLTEXTWORD" in body and "SUMMARYWORD" not in body


def test_summary_only_document_is_searchable_when_configured(tmp_path):
    """A measure with no bill text must still be findable by its metadata — the gap
    the step-4 ingest flagged."""
    (tmp_path / "docs").mkdir(); (tmp_path / "_meta").mkdir()
    (tmp_path / "docs" / "d1.md").write_text(
        MULTI.format(id="d1", doc_type="dataset_doc", sha="d" * 64)
        .replace("## Full text\n\nFULLTEXTWORD\n\n", ""))
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: t
          name: T
          jurisdiction: oregon
          archetype: hybrid
        content_roots:
          - path: docs
            doc_type: dataset_doc
        index_headings:
          dataset_doc: ['Summary', 'Full text']
    """).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    fw = CorpusFramework(load_config(str(tmp_path / "_meta" / "corpus.yml")))
    assert [h["id"] for h in fw.search_corpus("SUMMARYWORD")] == ["d1"]
