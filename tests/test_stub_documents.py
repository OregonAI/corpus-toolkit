"""corpus-toolkit#213: stub documents (`content_mode: stub`) and the `coverage` tool.

A stub says "this instrument exists, and is not public": frontmatter, the disclaimer and a
one-line reason, with no verbatim text and no snapshot. It is a DOCUMENT, so get_document
and resolve_citation answer for it; it is never a search passage; coverage counts it apart.
"""
import jsonschema

from corpus_toolkit.validate.frontmatter import bundled_schema


def _stub(**overrides):
    """The trial document from the issue: a `policy` with no snapshot at all."""
    doc = {
        "schema_version": 1, "corpus": "test-corpus", "jurisdiction": "us-ca",
        "id": "ca-sam-5300", "title": "State Administrative Manual 5300",
        "doc_type": "policy", "citation": "SAM 5300", "issuing_body": "Dept of Technology",
        "source_url": "https://example.org/sam", "source_format": "html",
        "status": "current", "content_mode": "stub",
        "stub_reason": "Not published; the official site refuses automated fetches.",
        "last_verified": "2026-11-10", "verified_by": "@test", "maintainer": "@test",
    }
    doc.update(overrides)
    return doc


def _errors(doc):
    return [e.message for e in jsonschema.Draft202012Validator(bundled_schema()).iter_errors(doc)]


# ---------- schema ----------

def test_a_stub_validates_without_a_snapshot_or_authority_level():
    assert _errors(_stub()) == []


def test_a_stub_must_say_why():
    doc = _stub()
    del doc["stub_reason"]
    assert any("stub_reason" in m for m in _errors(doc))
    assert _errors(_stub(stub_reason=""))
    assert _errors(_stub(stub_reason="line one\nline two"))


def test_a_stub_carries_no_snapshot_pin():
    # A hash on a stub would claim a snapshot the gate has none of to check.
    assert _errors(_stub(source_sha256="a" * 64))
    assert _errors(_stub(snapshot_id="x"))
    assert _errors(_stub(snapshot_policy="hash-only"))


def test_the_same_document_without_stub_mode_still_fails_as_before():
    """No rule is weakened for a non-stub: the issue's trial document is still refused."""
    for mode in ("summary", "verbatim", "mixed"):
        msgs = _errors(_stub(content_mode=mode))
        assert any("'retrieved' is a required property" in m for m in msgs), mode
        assert any("'source_sha256' is a required property" in m for m in msgs), mode
        assert any("'authority_level' is a required property" in m for m in msgs), mode
    doc = _stub()
    del doc["content_mode"]
    assert any("'content_mode' is a required property" in m for m in _errors(doc))


def test_a_stub_may_be_an_external_reference():
    assert _errors(_stub(doc_type="external_reference")) == []


# ---------- the corpus fixture, and the provenance gate ----------

from pathlib import Path   # noqa: E402

from tests.test_backends import DOC, corpus, fw   # noqa: E402,F401  (corpus is a fixture)
from tests.test_overview_jurisdictions import _commit   # noqa: E402

from corpus_toolkit.config import load as load_config   # noqa: E402

STUB = """---
schema_version: 1
corpus: test-corpus
id: {id}
title: "{title}"
doc_type: statute
citation: "{citation}"
issuing_body: "Test Body"
source_url: "https://example.invalid/{id}"
source_format: html
status: current
jurisdiction: {jurisdiction}
content_mode: stub
stub_reason: "{reason}"
last_verified: "2026-11-10"
verified_by: "@test"
maintainer: "@test"
tags: [{tags}]
---

NON-AUTHORITATIVE. This instrument exists but is not public; no text is mirrored.

Reason: {reason}
"""


def _add_stub(root: Path, doc_id="wa-stub-1", title="Secret security standard",
              citation="WA SEC-1", jurisdiction="us-wa", tags='"ai"',
              reason="Not published by the issuing agency.", commit=True) -> str:
    (root / "statutes" / f"{doc_id}.md").write_text(STUB.format(
        id=doc_id, title=title, citation=citation, jurisdiction=jurisdiction, tags=tags,
        reason=reason))
    if commit:
        _commit(root)
    return doc_id


def _provenance(root: Path, doc_id: str):
    from corpus_toolkit.validate import provenance as prov
    prov._CONFIG = load_config(str(root / "_meta" / "corpus.yml"))
    prov._SLICE_FN = lambda d, s, t: t
    return prov.check_file(root / "statutes" / f"{doc_id}.md")


def test_provenance_accepts_a_stub_with_no_snapshot(corpus):
    _add_stub(corpus)
    rel, findings, checked, fulltexts, quotes = _provenance(corpus, "wa-stub-1")
    assert findings == []
    assert (checked, fulltexts, quotes) == (0, 0, 0)      # nothing was verified: nothing to


def test_provenance_still_demands_a_snapshot_of_a_non_stub(corpus):
    """The stub exemption is by content_mode alone; it must not leak to other documents."""
    p = corpus / "statutes" / "ors-1.010.md"
    p.write_text(p.read_text().replace("content_mode: verbatim", "content_mode: summary"))
    _, findings, _, _, _ = _provenance(corpus, "ors-1.010")
    assert any(lvl == "error" for lvl, _ in findings)
    # and a verbatim document with no snapshot is refused as before
    q = corpus / "statutes" / "ors-2.020.md"
    _, findings, _, _, _ = _provenance(corpus, "ors-2.020")
    assert any("missing source snapshot" in m for _, m in findings)


def test_provenance_refuses_text_smuggled_into_a_stub(corpus):
    _add_stub(corpus, doc_id="s-full", commit=False)
    p = corpus / "statutes" / "s-full.md"
    p.write_text(p.read_text() + "\n## Full text\n\nSome text nobody verified.\n")
    _, findings, _, _, _ = _provenance(corpus, "s-full")
    assert any(lvl == "error" and "stub" in m for lvl, m in findings)

    _add_stub(corpus, doc_id="s-quote", commit=False)
    p = corpus / "statutes" / "s-quote.md"
    p.write_text(p.read_text() + '\n**[VERBATIM]** "A claimed quotation."\n')
    _, findings, _, _, _ = _provenance(corpus, "s-quote")
    assert any(lvl == "error" and "stub" in m for lvl, m in findings)

    _add_stub(corpus, doc_id="s-long", commit=False)
    p = corpus / "statutes" / "s-long.md"
    p.write_text(p.read_text() + "\n" + "A paraphrase of the instrument. " * 100)
    _, findings, _, _, _ = _provenance(corpus, "s-long")
    assert any(lvl == "error" and "stub" in m for lvl, m in findings)


def test_frontmatter_gate_accepts_a_stub_file_end_to_end(corpus):
    from corpus_toolkit.validate import frontmatter as fmv
    _add_stub(corpus)
    cfg = load_config(str(corpus / "_meta" / "corpus.yml"))
    fmv._init_worker(fmv.bundled_schema(), cfg, None)
    _, findings, doc_id = fmv.check_file(corpus / "statutes" / "wa-stub-1.md")
    assert doc_id == "wa-stub-1" and findings == []


# ---------- a stub is never a search passage ----------

def test_a_stub_is_never_returned_by_keyword_search(corpus):
    _add_stub(corpus, title="Permit security standard", citation="WA SEC permit")
    f = fw(corpus)
    for mode in ("keyword", "hybrid"):
        ids = [h["id"] for h in f.search_corpus("permit security standard", mode=mode)]
        assert "wa-stub-1" not in ids, mode
    # even a query that matches nothing else
    assert all(h["id"] != "wa-stub-1" for h in f.search_corpus("Reason issuing agency"))
    assert all(h["id"] != "wa-stub-1" for h in f.search_corpus("WA SEC"))


def test_a_stub_is_not_returned_by_compare(corpus):
    _add_stub(corpus, title="Permit security standard")
    out = fw(corpus).compare("security standard", ["us-wa"], mode="keyword")
    assert out["groups"][0]["hits"] == []


def test_a_semantic_candidate_that_is_a_stub_is_dropped(corpus):
    _add_stub(corpus)

    class Sem:
        def available(self): return True
        def rank(self, q, n): return ["wa-stub-1", "ors-1.010"]

    f = fw(corpus)
    f.backend._semantic = Sem()
    ids = [h["id"] for h in f.search_corpus("anything", mode="semantic")]
    assert ids == ["ors-1.010"]


def test_stubs_are_not_embedded(corpus):
    from corpus_toolkit.semantic.build import doc_units
    _add_stub(corpus)
    _, _, units = doc_units(corpus / "statutes" / "wa-stub-1.md")
    assert units == []


# ---------- exists, not public: get_document and resolve_citation ----------

def test_get_document_answers_for_a_stub_and_says_it_is_one(corpus):
    _add_stub(corpus, reason="Not published by the issuing agency.")
    out = fw(corpus).get_document("wa-stub-1")
    assert "error" not in out
    assert out["content_mode"] == "stub" and out["stub"] is True
    assert out["stub_reason"] == "Not published by the issuing agency."
    assert out["citation"] == "WA SEC-1" and out["source_url"].startswith("https://")
    assert "NON-AUTHORITATIVE" in out["disclaimer"]
    assert "not public" in out["note"].lower()


def test_a_normal_document_response_gains_no_stub_fields(corpus):
    out = fw(corpus).get_document("ors-1.010")
    assert "stub" not in out and "stub_reason" not in out and "note" not in out


def test_resolve_citation_finds_a_stub_and_flags_it(corpus):
    from corpus_toolkit.mcp.framework import clear_schemes, register_scheme
    _add_stub(corpus)
    clear_schemes()
    register_scheme("wasec", r"WA SEC-(?P<n>\d+)", "wa-stub-{n}")
    try:
        out = fw(corpus).resolve_citation("WA SEC-1")
    finally:
        clear_schemes()
    assert [m["id"] for m in out["matches"]] == ["wa-stub-1"]
    assert out["matches"][0]["stub"] is True
    assert "unresolved" not in out
    assert "not public" in out["note"].lower()


def test_resolve_citation_does_not_flag_a_normal_document(corpus):
    f = fw(corpus)
    out = f.resolve_citation("ors-1.010")
    for x in out["matches"]:
        assert "stub" not in x


def test_health_does_not_warn_that_a_stub_only_doc_type_has_no_text(corpus):
    """Stubs are bodiless by definition; a doc_type holding only stubs is not a
    misconfigured index_headings."""
    for n in ("ors-1.010", "ors-2.020"):
        (corpus / "statutes" / f"{n}.md").unlink()
    _add_stub(corpus)
    yml = corpus / "_meta" / "corpus.yml"
    yml.write_text(yml.read_text() + "index_headings:\n  statute: [\"Full text\"]\n")
    _commit(corpus)
    h = fw(corpus).backend.health()
    assert h["reachable"] is True
    assert "WARNING" not in h["detail"] and h["empty_body_by_doc_type"] == {}


def test_a_stub_does_not_dilute_the_empty_body_count_of_real_documents(corpus):
    """A doc_type of stubs plus real documents is judged on the real ones: the one real
    document's headings match nothing, so the warning must still fire."""
    (corpus / "statutes" / "ors-2.020.md").unlink()
    _add_stub(corpus)
    yml = corpus / "_meta" / "corpus.yml"
    yml.write_text(yml.read_text() + "index_headings:\n  statute: [\"No such heading\"]\n")
    _commit(corpus)
    h = fw(corpus).backend.health()
    assert "WARNING" in h["detail"] and h["empty_body_by_doc_type"] == {"statute": 1}


# ---------- coverage ----------

def _wa_ca(root: Path):
    """us-wa: one document, two stubs. us-or: one document. One document with no jurisdiction
    anywhere is not added (the corpus declares one); see the unspecified test."""
    p = root / "statutes" / "ors-1.010.md"
    p.write_text(p.read_text().replace("status: current\n", "status: current\njurisdiction: us-or\n", 1))
    q = root / "statutes" / "ors-2.020.md"
    q.write_text(q.read_text().replace("status: current\n", "status: current\njurisdiction: us-wa\n", 1)
                 .replace('tags: ["ors"]', 'tags: ["ors", "ai"]'))
    _add_stub(root, "wa-stub-1", tags='"ai"', commit=False)
    _add_stub(root, "wa-stub-2", citation="WA SEC-2", tags='"ai", "privacy"', commit=False)
    _commit(root)


def test_coverage_counts_documents_and_stubs_apart_per_jurisdiction(corpus):
    _wa_ca(corpus)
    out = fw(corpus).coverage()
    wa = out["jurisdictions"]["us-wa"]
    assert (wa["documents"], wa["stubs"], wa["total"]) == (1, 2, 3)
    assert wa["stub_share"] == 2 / 3
    orr = out["jurisdictions"]["us-or"]
    assert (orr["documents"], orr["stubs"], orr["total"], orr["stub_share"]) == (1, 0, 1, 0.0)
    assert out["totals"] == {"documents": 2, "stubs": 2, "total": 4}
    assert "NON-AUTHORITATIVE" in out["disclaimer"]


def test_coverage_breaks_each_jurisdiction_down_by_tag(corpus):
    _wa_ca(corpus)
    tags = fw(corpus).coverage()["jurisdictions"]["us-wa"]["tags"]
    assert tags["ai"] == {"documents": 1, "stubs": 2}
    assert tags["privacy"] == {"documents": 0, "stubs": 1}
    assert tags["ors"] == {"documents": 1, "stubs": 0}


def test_coverage_for_one_jurisdiction_not_held_says_so(corpus):
    _wa_ca(corpus)
    out = fw(corpus).coverage(jurisdiction="us-co")
    assert list(out["jurisdictions"]) == ["us-co"]
    co = out["jurisdictions"]["us-co"]
    assert co["in_corpus"] is False and co["total"] == 0 and co["stub_share"] is None
    held = fw(corpus).coverage(jurisdiction="us-wa")["jurisdictions"]["us-wa"]
    assert held["in_corpus"] is True and held["stubs"] == 2


def test_coverage_with_no_jurisdiction_stated_is_unspecified_not_invented(corpus):
    yml = corpus / "_meta" / "corpus.yml"
    yml.write_text(yml.read_text().replace("  jurisdiction: oregon\n", ""))
    _commit(corpus)
    out = fw(corpus).coverage()
    assert out["jurisdictions"] == {}
    assert out["unspecified"]["documents"] == 2


def test_a_single_jurisdiction_corpus_with_no_stubs_reports_zero_stubs(corpus):
    out = fw(corpus).coverage()
    assert out["jurisdictions"]["oregon"]["stubs"] == 0
    assert out["jurisdictions"]["oregon"]["documents"] == 2
