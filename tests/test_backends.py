"""The retrieval seam: protocol conformance, backend selection, and the guarantee that
introducing it changed nothing for file-backed corpora.

The parity guarantee is the important one. The FTS schema is load-bearing in ways that do
not fail loudly, so these tests build a real corpus on disk and assert on actual
responses rather than on the index's internals.

That distinction became literal with contentless FTS (corpus-toolkit#17): the `fts` table
no longer stores text, so reading any of its columns returns NULL instead of raising.
Tests that inspected `fts.body` were asserting on an implementation detail; they now
assert on what a caller can observe — whether a term is findable.
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
    """The text that FEEDS fts.body, taken from the function that produces it.

    Was `SELECT body FROM fts WHERE id='d1'`. That stopped being possible with contentless
    FTS -- both the selected column and the WHERE clause read NULL -- and it was the wrong
    observable anyway: every assertion below is about which SECTIONS get indexed, which is
    `_searchable_body`'s job, not SQLite's.

    The end-to-end property (indexed text is actually findable, non-indexed text is not) is
    covered separately by `test_indexed_sections_are_searchable_and_others_are_not`, which
    goes through a real MATCH.
    """
    from corpus_toolkit.repo import parse_frontmatter
    p = fw.config.root / "docs" / "d1.md"
    fm, body = parse_frontmatter(p)
    return fw.backend._searchable_body(body, fm["doc_type"])


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


# ---------- contentless FTS (corpus-toolkit#17) ----------

def test_excerpt_marks_matches_and_keeps_citations_whole():
    """The excerpt builder replaces snippet(). Two things it must not get wrong: a citation
    is one token (the word pattern accepts '.' and '-'), and trailing sentence punctuation
    belongs OUTSIDE the markers -- '[Diem.]' looks like a corpus typo."""
    from corpus_toolkit.mcp.backends import make_excerpt
    got = make_excerpt("Payment of Per Diem. See ORS 192.355 and OAR 137-090-0000.",
                       ["diem", "192.355", "137-090-0000"], width=20)
    assert "[Diem]." in got and "[Diem.]" not in got
    assert "[192.355]" in got
    assert "[137-090-0000]" in got


def test_excerpt_windows_on_the_densest_region():
    from corpus_toolkit.mcp.backends import make_excerpt
    text = ("filler " * 40) + "alpha beta gamma " + ("filler " * 40) + "alpha "
    got = make_excerpt(text, ["alpha", "beta", "gamma"], width=8)
    assert "[alpha] [beta] [gamma]" in got
    assert got.startswith(" … ") and got.endswith(" … ")   # truncated both ends


def test_excerpt_falls_back_to_the_head_when_nothing_matches():
    """A document can be a legitimate hit on its title, citation or tags and contain no
    query term in its body. That must produce readable text, not an empty string."""
    from corpus_toolkit.mcp.backends import make_excerpt
    got = make_excerpt("The department shall keep records of every application.", ["zebra"])
    assert got.startswith("The department shall keep")
    assert "[" not in got


def test_excerpt_approximates_porter_stemming():
    """Highlighting only -- FTS5 already decided what matched. Inflections must still be
    marked, or excerpts for a stemmed hit show no highlight at all."""
    from corpus_toolkit.mcp.backends import _stem_match
    assert _stem_match("governed", "governing")
    assert _stem_match("governs", "governing")
    assert not _stem_match("car", "cat")        # short words: exact or nothing

def test_indexed_sections_are_searchable_and_others_are_not(tmp_path):
    """The end-to-end half of what _body_col used to assert, through a real MATCH.

    This is the test that would catch a broken docs<->fts join: contentless FTS returns
    NULL for `f.id`, so the previous `JOIN docs d ON d.id = f.id` matched zero rows and
    every query came back empty while the index itself was perfectly fine.
    """
    fw = _corpus_with(tmp_path, "dataset_doc", {"dataset_doc": ["Summary", "Full text"]})
    assert [h["id"] for h in fw.search_corpus("SUMMARYWORD")] == ["d1"]
    assert [h["id"] for h in fw.search_corpus("FULLTEXTWORD")] == ["d1"]
    # Boilerplate is excluded from the index, so it must not be findable.
    assert fw.search_corpus("PROVENANCEWORD") == []


def test_search_returns_a_real_excerpt_not_null(tmp_path):
    """snippet() returns NULL on a contentless table rather than failing, so a naive port
    ships every result with an empty (or crashing) snippet. Assert there is real text and
    that the matched term is marked in it."""
    fw = _corpus_with(tmp_path, "dataset_doc", {"dataset_doc": ["Summary", "Full text"]})
    hit = fw.search_corpus("FULLTEXTWORD")[0]
    assert hit["snippet"], "snippet is empty — snippet() NULL leaked through"
    assert "[FULLTEXTWORD]" in hit["snippet"]


def test_stale_schema_forces_a_rebuild_even_when_content_is_unchanged(tmp_path):
    """A toolkit upgrade under a warm cache must rebuild.

    The content state key cannot detect this: the corpus has not changed, so it matches,
    and the index is reused with a `docs` table that lacks columns the new queries select.
    Without the schema key that is an OperationalError on the first request of every
    deployed server simultaneously.
    """
    import sqlite3
    fw = _corpus_with(tmp_path, "statute")
    db = fw.backend._db_path
    fw.backend.ensure_index().close()

    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET v='1' WHERE k='schema'")   # pretend an older builder
    # Empty the catalog. A rebuild is the only thing that can put the row back, so this
    # detects reuse without needing ALTER TABLE ... DROP COLUMN (sqlite >= 3.35, which the
    # 3.10 CI runners cannot be assumed to have).
    con.execute("DELETE FROM docs")
    con.commit(); con.close()

    assert fw.backend.index_status()[0] is False
    con = fw.backend.ensure_index()          # must rebuild rather than reuse
    assert con.execute("SELECT COUNT(*) FROM docs WHERE body_chars > 0").fetchone()[0] == 1
    assert con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0] == "2"


def test_empty_body_health_warning_still_fires(tmp_path):
    """This check read `fts.body = ''`, which on a contentless table is never true — so it
    would have gone quiet forever while reporting a healthy corpus. It now reads
    docs.body_chars. Deleting that column's use must fail this test."""
    fw = _corpus_with(tmp_path, "dataset_doc", {"dataset_doc": ["No Such Heading"]})
    detail = fw.backend.health()["detail"]
    assert "WARNING" in detail and "dataset_doc" in detail


# ---------- review findings: guardrails that used to pass silently ----------

def test_scalar_index_headings_is_rejected_at_load(tmp_path):
    """A YAML scalar is truthy AND iterable, so the loop walked CHARACTERS and emptied
    the doc_type's index in total silence. The likeliest authoring mistake here."""
    (tmp_path / "docs").mkdir(); (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus: {id: t, name: T, jurisdiction: oregon, archetype: document}
        content_roots:
          - {path: docs, doc_type: statute}
        index_headings: {statute: "Full text"}
    """).strip() + "\n")
    with pytest.raises(ValueError, match="string, not a list"):
        load_config(str(tmp_path / "_meta" / "corpus.yml"))


def test_health_warns_when_configured_headings_match_nothing(corpus):
    """Config validation cannot catch a well-formed list naming a heading no document
    uses (["Full Text"] vs "## Full text"). health() must."""
    cfg = corpus / "_meta" / "corpus.yml"
    cfg.write_text(cfg.read_text() + 'index_headings:\n  statute: ["Full Text"]\n')
    h = CorpusFramework(load_config(str(cfg))).backend.health()
    assert "WARNING" in h["detail"]
    assert h["empty_body_by_doc_type"] == {"statute": 2}


def test_health_silent_for_unconfigured_doc_type_with_no_body(tmp_path):
    """A doc_type nobody configured, holding metadata-only documents, is the corpus's
    own choice — warning about it trains operators to ignore the message."""
    (tmp_path / "docs").mkdir(); (tmp_path / "_meta").mkdir()
    (tmp_path / "docs" / "x.md").write_text(DOC.format(
        id="ref-1", title="T", doc_type="external_reference", citation="C",
        sha="e" * 64, tag="x", glance="only a glance", body="").replace(
            "## Full text\n\n\n", ""))
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus: {id: t, name: T, jurisdiction: oregon, archetype: document}
        content_roots:
          - {path: docs, doc_type: external_reference}
    """).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    h = CorpusFramework(load_config(str(tmp_path / "_meta" / "corpus.yml"))).backend.health()
    assert "WARNING" not in h["detail"]


def test_resolve_citation_consults_the_backend_when_there_is_no_graph(tmp_path):
    """graph() degrades to {} with no graph.json, so resolve_citation reported documents
    the server was actively serving as nonexistent — a false statement about its own
    contents, not a missing answer."""
    (tmp_path / "docs").mkdir(); (tmp_path / "_meta").mkdir()
    (tmp_path / "docs" / "d.md").write_text(DOC.format(
        id="ors-1.010", title="Water", doc_type="statute", citation="ORS 1.010",
        sha="f" * 64, tag="ors", glance="g", body="Body."))
    (tmp_path / "cite.py").write_text(
        "from corpus_toolkit.mcp.framework import register_scheme\n"
        "register_scheme('ors', r'\\bORS\\s+(\\d{1,3}\\.\\d{3})\\b', 'ors-{0}')\n")
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus: {id: t, name: T, jurisdiction: oregon, archetype: document}
        content_roots:
          - {path: docs, doc_type: statute}
        plugins: {citation_module: "cite"}
    """).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    fw = CorpusFramework(load_config(str(tmp_path / "_meta" / "corpus.yml")))
    assert not (tmp_path / "_meta" / "graph.json").is_file()
    assert [m["id"] for m in fw.resolve_citation("ORS 1.010")["matches"]] == ["ors-1.010"]


def test_two_corpora_do_not_share_a_scheme_registry(tmp_path):
    """Every corpus here uses the same `src.citations` convention, so the first one
    loaded was handed to all the others and their schemes never registered."""
    frameworks = []
    for name, scheme, pat in (("A", "ors", r"\bORS\s+(\d{1,3}\.\d{3})\b"),
                              ("B", "measure", r"\b(HB)\s+(\d+)\b")):
        r = tmp_path / name
        (r / "src").mkdir(parents=True); (r / "docs").mkdir(); (r / "_meta").mkdir()
        (r / "src" / "citations.py").write_text(
            "from corpus_toolkit.mcp.framework import register_scheme\n"
            f"register_scheme({scheme!r}, r'{pat}', 'x-{{0}}')\n")
        (r / "_meta" / "corpus.yml").write_text(textwrap.dedent(f"""
            corpus: {{id: corpus-{name}, name: {name}, jurisdiction: oregon, archetype: document}}
            content_roots:
              - {{path: docs, doc_type: statute}}
            plugins: {{citation_module: "src.citations"}}
        """).strip() + "\n")
        subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        frameworks.append(CorpusFramework(load_config(str(r / "_meta" / "corpus.yml"))))
    assert [s[0] for s in frameworks[0].schemes] == ["ors"]
    assert [s[0] for s in frameworks[1].schemes] == ["measure"]


# ---------- mirrored doc_types (oregon-audits P7, federal-reference P8, oregon-kpm P10) ----------

@pytest.mark.parametrize("doc_type",
                         ["audit_report", "federal_instrument", "performance_report"])
def test_mirrored_doc_type_is_wired_in_all_three_places(doc_type):
    """A doc_type we reproduce must be in THREE coupled places or it half-works.

    The enum alone makes the type valid. Missing from the schema's verbatim-required
    conditional it stops requiring source_sha256/content_mode; missing from
    provenance.VERBATIM_REQUIRED it stops requiring content_mode: verbatim. Either omission
    yields a type that validates cleanly while SKIPPING the guarantee that the text is the
    real text -- for an audit finding that means a paraphrase ships as the record, and for
    a federal requirement it means a paraphrase ships as the law.

    Parametrized so the next mirrored type cannot be added to the enum alone.
    """
    import json
    from pathlib import Path
    from corpus_toolkit.validate.provenance import VERBATIM_REQUIRED
    schema = json.loads((Path(__file__).parent.parent / "corpus_toolkit" / "schemas"
                         / "document.frontmatter.v1.schema.json").read_text())
    assert doc_type in schema["properties"]["doc_type"]["enum"]
    assert doc_type in schema["allOf"][0]["if"]["properties"]["doc_type"]["enum"], \
        f"{doc_type} is valid but is not required to carry a snapshot hash"
    assert doc_type in VERBATIM_REQUIRED, f"{doc_type} would not be required to be verbatim"


def test_external_reference_stays_summary_only():
    """The other half of the copyright determination, and it must not drift.

    federal_instrument means "we may reproduce this"; external_reference means "we may
    not". If external_reference ever stopped forcing summary, third-party or
    distribution-restricted material could be mirrored in full with nothing objecting.
    """
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parent.parent / "corpus_toolkit" / "schemas"
                         / "document.frontmatter.v1.schema.json").read_text())
    cond = next(c for c in schema["allOf"]
                if c["if"]["properties"]["doc_type"].get("const") == "external_reference")
    assert cond["then"]["properties"]["content_mode"]["const"] == "summary"


def test_state_authored_alias_still_resolves():
    """The old name was importable, so it stays. Renaming it silently would break callers
    that never see a deprecation warning."""
    from corpus_toolkit.validate.provenance import STATE_AUTHORED, VERBATIM_REQUIRED
    assert STATE_AUTHORED is VERBATIM_REQUIRED


# ---------- mcp.extra_document_fields (corpus-toolkit#21) ----------

def _corpus_with_custom_field(tmp_path: Path, declare: bool) -> CorpusFramework:
    """A corpus whose documents carry a domain field the toolkit knows nothing about."""
    (tmp_path / "statutes").mkdir()
    (tmp_path / "_meta").mkdir()
    doc = DOC.format(id="ors-1.010", title="T", doc_type="statute", citation="ORS 1.010",
                     sha="a" * 64, tag="ors", glance="G", body="B")
    doc = doc.replace("tags: [\"ors\"]",
                      "tags: [\"ors\"]\naudited_period_start: \"2018-07-01\"")
    (tmp_path / "statutes" / "ors-1.010.md").write_text(doc)
    cfg = textwrap.dedent("""
        corpus: {id: t, name: T, jurisdiction: oregon, archetype: document}
        content_roots:
          - path: statutes
            doc_type: statute
    """).strip() + "\n"
    if declare:
        cfg += "mcp:\n  extra_document_fields: [audited_period_start]\n"
    (tmp_path / "_meta" / "corpus.yml").write_text(cfg)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return CorpusFramework(load_config(str(tmp_path / "_meta" / "corpus.yml")))


def test_custom_frontmatter_is_absent_unless_declared(tmp_path):
    """The default must not change: a corpus adding a frontmatter key does not silently
    change what its server emits. The response shape is an interface contract."""
    d = _corpus_with_custom_field(tmp_path, declare=False).get_document("ors-1.010")
    assert "audited_period_start" not in d


def test_declared_custom_frontmatter_reaches_get_document(tmp_path):
    """The bug this fixes. Without it a field can be REQUIRED by a corpus's own schema
    checks and still be unreachable by every caller -- it validates, then vanishes.
    oregon-audits hit exactly this: `audited_period_start` is the value that stops a 2019
    finding being read as current, and no agent could see it."""
    d = _corpus_with_custom_field(tmp_path, declare=True).get_document("ors-1.010")
    assert d.get("audited_period_start") == "2018-07-01"


def test_declaring_a_field_no_document_carries_is_harmless(tmp_path):
    """Declaring is a promise to serve the field WHEN PRESENT, not an assertion that every
    document has it. A KeyError here would make the config brittle for no benefit."""
    fw = _corpus_with_custom_field(tmp_path, declare=True)
    fw.config.mcp_extra_document_fields = ["audited_period_start", "no_such_field"]
    d = fw.get_document("ors-1.010")
    assert d.get("audited_period_start") == "2018-07-01"
    assert "no_such_field" not in d


# ---------- subsections & chunks: big instruments become navigable ----------
#
# The five federal monsters (up to 1.1 MB) used to be a binary — 400-byte glance or
# the whole body — because `sections` saw only `## ` headings and every monster has
# exactly two. These tests pin the three affordances that fixed it: ### listing on
# the big-doc gate, prefix-matched ### retrieval, and deterministic chunk paging.

def _sectioned_doc(corpus: Path, big: bool) -> str:
    body = "\n\n".join(
        f"### SEC. {n}. {name}\n\n" + (f"Text of section {n}. " * (2000 if big else 3))
        for n, name in ((101, "PURPOSES"), (188, "NONDISCRIMINATION"),
                        (189, "ADMINISTRATIVE PROVISIONS")))
    (corpus / "statutes" / "pl-0-0.md").write_text(DOC.format(
        id="pl-0-0", title="Test Act", doc_type="statute", citation="Test Act",
        sha="c" * 64, tag="ors", glance="A sectioned act.", body=body))
    subprocess.run(["git", "add", "-A"], cwd=corpus, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "act"], cwd=corpus, check=True)
    return "pl-0-0"


def test_big_doc_auto_lists_subsections(corpus):
    doc_id = _sectioned_doc(corpus, big=True)
    d = fw(corpus).get_document(doc_id)                      # part="auto"
    assert "body" not in d and "at_a_glance" in d
    assert "SEC. 188. NONDISCRIMINATION" in d["subsections"]
    assert "subsections" in d["note"] or "subsection" in d["note"]


def test_get_subsection_by_prefix(corpus):
    doc_id = _sectioned_doc(corpus, big=False)
    d = fw(corpus).get_document(doc_id, part="SEC. 188.")
    assert d["section"] == "SEC. 188. NONDISCRIMINATION"
    assert "Text of section 188." in d["body"]
    assert "Text of section 101." not in d["body"]           # span ends at next ###


def test_ambiguous_subsection_prefix_is_an_error_not_a_guess(corpus):
    doc_id = _sectioned_doc(corpus, big=False)
    d = fw(corpus).get_document(doc_id, part="SEC. 1")       # matches 101, 188, 189? no —
    # prefix "SEC. 1" matches all three (101, 188, 189 all start "SEC. 1")
    assert "error" in d
    assert d.get("subsections_matching")


def test_chunk_part_pages_deterministically(corpus):
    doc_id = _sectioned_doc(corpus, big=True)
    d0 = fw(corpus).get_document(doc_id, part="chunk:0")
    assert d0["section"] == "chunk:0" and d0["body"]
    d_missing = fw(corpus).get_document(doc_id, part="chunk:999")
    assert "error" in d_missing and "ordinals are 0-based" in d_missing["error"]


def test_search_hits_carry_chunk_identity_from_rank_chunks(corpus):
    class FakeSemantic:
        @staticmethod
        def available():
            return True

        @staticmethod
        def rank_chunks(query, want):
            return [{"doc_id": "ors-1.010", "ordinal": 3, "heading": "Full text",
                     "preview": "…appropriate water without a permit…", "score": 0.9}]

        @staticmethod
        def rank(query, want):
            return ["ors-1.010"]

    b = FileBackend(load_config(str(corpus / "_meta" / "corpus.yml")),
                    semantic=FakeSemantic())
    hits = b.search(query="water permit", mode="hybrid")
    hit = next(h for h in hits if h["id"] == "ors-1.010")
    assert hit["chunk"]["ordinal"] == 3
    assert "chunk:3" in hit["chunk"]["fetch"]
