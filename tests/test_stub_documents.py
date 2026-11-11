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
