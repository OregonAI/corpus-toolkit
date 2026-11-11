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
