"""The document frontmatter schema's `status` enum (corpus-toolkit#159).

`suspended` means IN FORCE UNTIL RECENTLY, OUT OF FORCE NOW, EXPECTED TO RETURN -- a
temporary, usually dated loss of force, distinct from `repealed`'s permanent one. Oregon
suspends administrative rules with an end date the rule's own text prints; before this
value existed, a corpus holding such a rule had to pick `repealed` (claims a permanence
its own text disproves), `current` (claims force the publisher just withdrew), or
`superseded` (claims a replacement that does not exist) -- three wrong answers.

SEAM: `corpus_toolkit.validate.frontmatter.bundled_schema()`, driven directly through
`jsonschema.Draft202012Validator` -- the same validator class `check_file` builds
(`_init_worker`) and runs every corpus document through in production
(`_VALIDATOR.iter_errors(fm)`). The enum is a property of the schema file itself, not of
any one corpus's config or content layout, so this is the smallest seam that actually
exercises the shipped contract rather than a copy of it.
"""
import jsonschema

from corpus_toolkit.validate.frontmatter import bundled_schema


def _document(**overrides):
    """The minimal document satisfying every REQUIRED top-level field.
    doc_type=entity_doc sidesteps the archetype `allOf` conditionals (retrieved,
    source_sha256, content_mode, authority_level) that apply to document-bearing
    doc_types -- irrelevant to what `status` accepts."""
    doc = {
        "schema_version": 1,
        "corpus": "test-corpus",
        "jurisdiction": "oregon",
        "id": "test-doc",
        "title": "Test Document",
        "doc_type": "entity_doc",
        "citation": "Test Citation",
        "issuing_body": "Test Body",
        "source_url": "https://example.org/test",
        "source_format": "json",
        "status": "current",
        "last_verified": "2026-08-27",
        "verified_by": "@test",
        "maintainer": "@test",
    }
    doc.update(overrides)
    return doc


def _errors(doc):
    validator = jsonschema.Draft202012Validator(bundled_schema())
    return list(validator.iter_errors(doc))


def test_status_suspended_validates():
    errors = _errors(_document(status="suspended"))
    assert errors == [], [e.message for e in errors]


def test_status_outside_the_enum_still_fails():
    """The enum stays closed: a value nobody declared (`vacated`) is not quietly
    admitted alongside `suspended`."""
    errors = _errors(_document(status="vacated"))
    assert any(err.path and err.path[0] == "status" for err in errors), \
        [e.message for e in errors]


def test_status_description_distinguishes_suspended_from_repealed():
    """Acceptance criterion: the schema's own description states what `suspended`
    means and how it differs from `repealed` -- a temporary, usually dated loss of
    force, versus a permanent one."""
    description = bundled_schema()["properties"]["status"].get("description", "")
    assert "suspended" in description
    assert "repealed" in description
    assert "temporary" in description
    assert "permanent" in description
