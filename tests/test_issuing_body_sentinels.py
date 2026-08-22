"""A corpus can say "this document is attributed to no body, on purpose".

corpus-toolkit#94. `plugins.issuing_body_slug_field` let a corpus name the frontmatter key
carrying its registry slug, and `issuing_body_profile` counted on that value — but nothing
checked the value against the registry, and nothing let a corpus declare which non-registry
values were deliberate. Two consequences, and either fix alone leaves the other in place.

A WRONG value is counted for no body. `agency: departmnt-of-revenue` attributes a document
to a body that does not exist, so it lands in no per-agency count. The path-derived half of
the same join IS checked — `validate/frontmatter.py` fails CI when a document under
`agencies/<slug>/` names an unregistered slug — so this was a hole on one side of one join.

A DELIBERATE value is indistinguishable from a typo. ERF's 37,991 `agency: statewide`
documents are right: they carry no agency by design. From the toolkit they looked exactly
like misspellings, so `attribution.complete` reported False and every per-agency count was
labelled a lower bound — permanently, for a reason that was 99.997% legitimate.

Sentinels get their own coverage bucket rather than being folded into `in_registry`.
"counted for a registry body" and "deliberately counted for no body" are different answers
to different questions, and CONTEXT.md's rule that outranks the vocabulary is that a new
mechanism may not collapse two distinct answers.
"""
import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus_toolkit import config as config_mod

REGISTRY = {"entries": [{"slug": "water-department", "name": "Water Department"},
                        {"slug": "employment-department", "name": "Employment Department"}]}


def _write(tmp_path: pathlib.Path, *, field="agency", sentinels=None, registry=True):
    """A corpus declaring a slug field and optionally a sentinel list."""
    (tmp_path / "_meta").mkdir(exist_ok=True)
    if registry:
        (tmp_path / "_meta" / "registry.yml").write_text(json.dumps(REGISTRY))
    plugins = []
    if registry:
        plugins.append("  issuing_body_registry: _meta/registry.yml")
    if field is not None:
        plugins.append(f"  issuing_body_slug_field: {field}")
    if sentinels is not None:
        plugins.append(f"  issuing_body_slug_sentinels: {sentinels}")
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: test-corpus
          name: Test Corpus
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: agencies
            scoped: true
            subdirs:
              policies: policy
          - path: rules
            doc_type: rule
        plugins:
    """).strip() + "\n" + "\n".join(plugins) + "\n")
    return config_mod.load(tmp_path / "_meta" / "corpus.yml")


def test_a_corpus_declaring_no_sentinels_is_unchanged(tmp_path):
    """The baseline, and the guard that stops the assertions below passing for the wrong
    reason: a malformed fixture raises too, and would look like a working check."""
    cfg = _write(tmp_path)

    assert cfg.issuing_body_slug_field == "agency"
    assert cfg.issuing_body_slug_sentinels == frozenset()


def test_declared_sentinels_are_parsed(tmp_path):
    cfg = _write(tmp_path, sentinels='["statewide", "multi-agency"]')

    assert cfg.issuing_body_slug_sentinels == frozenset({"statewide", "multi-agency"})


def test_sentinels_without_a_slug_field_is_a_config_error(tmp_path):
    """There is no field for them to apply to, so the declaration cannot mean anything.
    Failing at load beats silently ignoring it — a corpus author who declared sentinels
    believes documents carrying them are attributed, and would read the resulting coverage
    as a measurement rather than as their declaration being dropped on the floor."""
    with pytest.raises(ValueError) as e:
        _write(tmp_path, field=None, sentinels='["statewide"]')

    assert "issuing_body_slug_sentinels" in str(e.value)
    assert "issuing_body_slug_field" in str(e.value)


@pytest.mark.parametrize("bad, why", [
    ('"statewide"', "a bare string is not a list"),
    ('[statewide, 7]', "every entry must be a string"),
    ('[]', "an empty list declares nothing and is a mistake, not a no-op"),
])
def test_a_malformed_sentinel_declaration_fails_at_load(tmp_path, bad, why):
    with pytest.raises(ValueError) as e:
        _write(tmp_path, sentinels=bad)

    assert "issuing_body_slug_sentinels" in str(e.value), why


def test_a_sentinel_that_names_a_registry_entry_is_a_config_error(tmp_path):
    """A value cannot mean both "this body" and "no body". Declaring one that the registry
    also contains would make the two mechanisms disagree about the same document, silently
    — the class of contradiction `registry_slug_for`'s precedence exists to prevent."""
    with pytest.raises(ValueError) as e:
        _write(tmp_path, sentinels='["water-department"]')

    assert "water-department" in str(e.value)


# ---------- resolution: a sentinel is an assertion, not a gap ----------

def test_a_sentinel_does_not_fall_back_to_the_path_slug(tmp_path):
    """THE CASE THAT CHANGES INDEXED VALUES, and so forces the FTS SCHEMA_VERSION bump.

    A document under `agencies/water-department/policies/` declaring `agency: statewide`
    used to resolve to `water-department`: the declared value did not name a registry
    entry, so the precedence fell through to the CI-validated path slug. That fallback is
    right for a TYPO — an unchecked value must never displace a checked one — and wrong for
    a SENTINEL, which is the corpus positively asserting "no body". Re-attributing it by
    directory contradicts the corpus about its own document.

    Same column, different values: exactly the reasoning that bumped SCHEMA_VERSION to 3."""
    cfg = _write(tmp_path, sentinels='["statewide"]')
    parts = ("agencies", "water-department", "policies", "p.md")

    assert cfg.registry_slug_for({"agency": "statewide"}, parts) == "statewide"


def test_a_typo_still_falls_back_to_the_validated_path_slug(tmp_path):
    """The other side, unchanged. RULE 2 BEFORE RULE 3 IS THE WHOLE POINT: letting an
    unchecked value override a checked one means one typo silently REMOVES a document from
    a count that was previously right. Declaring sentinels must not weaken that."""
    cfg = _write(tmp_path, sentinels='["statewide"]')
    parts = ("agencies", "water-department", "policies", "p.md")

    assert cfg.registry_slug_for({"agency": "departmnt-of-revenue"}, parts) == (
        "water-department")


def test_a_registry_slug_still_wins_over_the_directory(tmp_path):
    """And the top of the precedence, unchanged."""
    cfg = _write(tmp_path, sentinels='["statewide"]')
    parts = ("agencies", "water-department", "policies", "p.md")

    assert cfg.registry_slug_for({"agency": "employment-department"}, parts) == (
        "employment-department")


def test_without_a_sentinel_declaration_the_old_precedence_is_untouched(tmp_path):
    """A corpus declaring no sentinels indexes byte-identically to before."""
    cfg = _write(tmp_path)
    parts = ("agencies", "water-department", "policies", "p.md")

    assert cfg.registry_slug_for({"agency": "statewide"}, parts) == "water-department"


# ---------- coverage: a fourth bucket, not a fold ----------

DOC = """---
schema_version: 1
id: {id}
title: "{id}"
doc_type: {doc_type}
citation: "C {id}"
authority_level: statute
issuing_body: "Body"
{agency}source_url: "https://example.invalid/{id}"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: "2026-07-26"
verified_by: "@test"
tags: ["t"]
---

## Full text

Text of {id}.
"""


def _corpus_with_docs(tmp_path, docs, *, sentinels=None):
    """A real corpus on disk (git repo + index) declaring `agency` and optional sentinels."""
    cfg = _write(tmp_path, sentinels=sentinels)
    for i, (rel, agency, doc_type) in enumerate(docs):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DOC.format(
            id=path.stem, doc_type=doc_type, sha=str(i) * 64,
            agency=f"agency: {agency}\n" if agency else ""))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "c"], cwd=tmp_path, check=True)
    from corpus_toolkit.mcp.framework import CorpusFramework
    return CorpusFramework(config_mod.load(tmp_path / "_meta" / "corpus.yml"))


# Two registry-attributed policies, plus one rule carrying a sentinel. The rule sits under
# `rules/`, which no directory attributes — ERF's shape in miniature.
_DOCS = [("agencies/water-department/policies/p-0.md", "water-department", "policy"),
         ("agencies/water-department/policies/p-1.md", "water-department", "policy"),
         ("rules/r-0.md", "statewide", "rule")]


def test_a_declared_sentinel_is_its_own_bucket_and_completes_the_count(tmp_path):
    """The payoff. A corpus where every document either names a registry entry or carries a
    declared sentinel has NOTHING unaccounted for, so its per-body counts are the whole
    answer rather than a floor.

    The sentinel documents are reported in their OWN bucket, not added to
    `documents_matched_to_a_registry_entry`: they are counted for no body, and saying
    otherwise would rebuild corpus-toolkit#71 one level up."""
    f = _corpus_with_docs(tmp_path, _DOCS, sentinels='["statewide"]')

    att = f.issuing_body_profile("water-department")["attribution"]

    assert att["documents_in_corpus"] == 3
    assert att["documents_matched_to_a_registry_entry"] == 2
    assert att["documents_declared_no_issuing_body"] == 1
    assert att["documents_naming_no_registry_entry"] == 0
    assert att["documents_with_no_issuing_body"] == 0
    assert att["complete"] is True


def test_the_same_corpus_without_the_declaration_is_still_a_lower_bound(tmp_path):
    """The control, and the reason this is a DECLARATION rather than a heuristic. Identical
    documents, no sentinel list — `statewide` is then indistinguishable from a typo and the
    count must still say so."""
    f = _corpus_with_docs(tmp_path, _DOCS)

    att = f.issuing_body_profile("water-department")["attribution"]

    assert att["documents_naming_no_registry_entry"] == 1
    assert att["documents_declared_no_issuing_body"] == 0
    assert att["complete"] is False


def test_an_undeclared_value_still_makes_the_count_a_lower_bound(tmp_path):
    """Declaring SOME sentinels must not excuse the others. A corpus that declares
    `statewide` and then misspells an agency is still incomplete, and the typo lands in the
    naming-no-registry-entry bucket where it is visible."""
    docs = _DOCS + [("rules/r-1.md", "departmnt-of-revenue", "rule")]
    f = _corpus_with_docs(tmp_path, docs, sentinels='["statewide"]')

    att = f.issuing_body_profile("water-department")["attribution"]

    assert att["documents_declared_no_issuing_body"] == 1
    assert att["documents_naming_no_registry_entry"] == 1
    assert att["complete"] is False


# ---------- validation: what makes the declaration safe ----------

def _validate(tmp_path, docs, *, sentinels=None):
    """Run corpus-validate-frontmatter's registry checks, returning the findings."""
    cfg = _write(tmp_path, sentinels=sentinels)
    for i, (rel, agency, doc_type) in enumerate(docs):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DOC.format(
            id=path.stem, doc_type=doc_type, sha=str(i) * 64,
            agency=f"agency: {agency}\n" if agency else ""))
    from corpus_toolkit.validate import frontmatter as fmv
    registry = fmv._read_registry(cfg).slugs
    findings = []
    for rel, agency, doc_type in docs:
        path = tmp_path / rel
        fmv._init_worker(fmv.bundled_schema(), cfg, registry)
        _, f, _ = fmv.check_file(path)
        findings += [msg for level, msg in f if "issuing-body" in msg or "sentinel" in msg]
    return findings


def test_an_undeclared_out_of_registry_value_fails_validation(tmp_path):
    """The half that makes the sentinel declaration SAFE rather than a mute button.

    Without this check, `issuing_body_slug_sentinels` would be a way to silence the coverage
    warning instead of answering it, and a genuine typo would stay invisible — the declared
    field would remain the one half of the join nothing validates."""
    findings = _validate(tmp_path, [("rules/r-0.md", "departmnt-of-revenue", "rule")])

    assert any("departmnt-of-revenue" in m for m in findings), findings


def test_a_declared_sentinel_passes_validation(tmp_path):
    findings = _validate(tmp_path, [("rules/r-0.md", "statewide", "rule")],
                         sentinels='["statewide"]')

    assert findings == []


def test_the_same_value_undeclared_fails_validation(tmp_path):
    """The control for the test above — the check is keyed on the DECLARATION, not on the
    value happening to look sentinel-ish."""
    findings = _validate(tmp_path, [("rules/r-0.md", "statewide", "rule")])

    assert any("statewide" in m for m in findings), findings


def test_a_registry_slug_passes_validation(tmp_path):
    findings = _validate(tmp_path, [("rules/r-0.md", "water-department", "rule")])

    assert findings == []


def test_a_corpus_declaring_no_slug_field_is_not_checked(tmp_path):
    """A corpus that attributes by directory only has no declared values to check, and must
    not acquire findings it cannot act on."""
    cfg = _write(tmp_path, field=None)
    assert cfg.issuing_body_slug_field is None


# ---------- backends that predate the fourth bucket ----------

class _OldShapeBackend:
    """A corpus-supplied backend reporting the THREE-bucket coverage shape."""
    name = "old-shape"

    def __init__(self, config, semantic=None):
        self.config = config

    def search(self, q, *, doc_type=None, issuing_body=None, limit=10, mode="hybrid"):
        return []

    def get(self, doc_id, *, part="auto"):
        return {"error": "no"}

    def exists(self, doc_id):
        return None

    def overview(self):
        return {"documents_by_type": {}, "commit": ""}

    def health(self):
        return {"reachable": True, "checked_at": "x", "detail": "old"}

    def holdings_for(self, slug):
        return {"counts": {"verbatim": 2},
                "coverage": {"documents": 3, "in_registry": 2,
                             "no_registry_entry": 1, "unattributed": 0,
                             "basis": "three-bucket"}}


def _framework_with_old_backend(tmp_path, *, sentinels=None):
    _write(tmp_path, sentinels=sentinels)
    cfg_path = tmp_path / "_meta" / "corpus.yml"
    cfg_path.write_text(cfg_path.read_text()
                        + '  retrieval_module: "sentinel_backend_mod:_OldShapeBackend"\n')
    (tmp_path / "sentinel_backend_mod.py").write_text(
        "from tests.test_issuing_body_sentinels import _OldShapeBackend\n")
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    sys.path.insert(0, str(tmp_path))
    from corpus_toolkit.mcp.framework import CorpusFramework
    return CorpusFramework(config_mod.load(cfg_path))


def test_an_old_shape_backend_is_still_a_measurement_when_no_sentinels_are_declared(tmp_path):
    """A three-bucket backend is reporting a COMPLETE measurement for a corpus with no
    sentinels: every document it saw fell into one of the three buckets and the fourth would
    be zero. Demanding the new key anyway would degrade every existing custom backend to
    `complete: None` for no gain."""
    f = _framework_with_old_backend(tmp_path)

    att = f.issuing_body_profile("water-department")["attribution"]

    assert att["complete"] is False          # 1 of 3 names no registry entry — a real answer
    assert att["documents_declared_no_issuing_body"] == 0


def test_an_old_shape_backend_degrades_to_unknown_once_sentinels_are_declared(tmp_path):
    """And the case where the missing key is decisive.

    Such a backend counted every sentinel document as `no_registry_entry`, because it does
    not know sentinels exist. Its split is not merely incomplete, it is WRONG — `complete`
    would read False for a corpus that is in fact fully attributed. Reporting UNKNOWN is the
    only honest answer: "could not check" is never "is not there"."""
    f = _framework_with_old_backend(tmp_path, sentinels='["statewide"]')

    att = f.issuing_body_profile("water-department")["attribution"]

    assert att["complete"] is None
    assert "declared_no_body" in att["note"]
    assert "UNKNOWN" in att["note"]


def test_the_lower_bound_note_counts_sentinels_as_accounted_for(tmp_path):
    """SIBLING INSTANCE of the bug this issue exists to fix, in the branch that fires
    whenever a single typo survives.

    The `complete` branch was updated to name the sentinel count; the `else` branch was not,
    so a corpus that declares sentinels AND has one unexplained value got prose reading
    "37913 of 75905 documents (50%) are attributed to a registry body" — the 37,991
    deliberate ones absent entirely. A caller concludes half the corpus is unaccounted for
    when the real gap is one document.

    That is the "lower bound for a 99.997% legitimate reason" message #94 exists to remove,
    surviving in the branch nobody looked at. The note must lead with what is actually
    UNACCOUNTED FOR, not with the registry-matched percentage."""
    docs = _DOCS + [("rules/r-1.md", "departmnt-of-revenue", "rule")]
    f = _corpus_with_docs(tmp_path, docs, sentinels='["statewide"]')

    note = f.issuing_body_profile("water-department")["attribution"]["note"]

    assert "declared to belong to no issuing body" in note
    assert "1 of this corpus's 4" in note, note      # the real gap, stated as the gap
    assert "LOWER BOUND" in note


# ---------- hardening found in review ----------

def test_a_null_sentinel_declaration_is_an_error_not_a_silent_no_op(tmp_path):
    """`issuing_body_slug_sentinels:` with nothing under it is PRESENT-BUT-EMPTY, not
    absent. Returning frozenset() there let an author who commented out their only entry hit
    exactly the failure the empty-list branch calls out — the key looks declared, declares
    nothing, and its documents fall back into `no_registry_entry` — with no error."""
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: t
          name: T
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: rules
            doc_type: rule
        plugins:
          issuing_body_slug_field: agency
          issuing_body_slug_sentinels:
    """).strip() + "\n")

    with pytest.raises(ValueError) as e:
        config_mod.load(tmp_path / "_meta" / "corpus.yml")

    assert "issuing_body_slug_sentinels" in str(e.value)


@pytest.mark.parametrize("bad", ["[agency]", "3"])
def test_a_non_string_slug_field_names_the_key(tmp_path, bad):
    """The adjacent unchecked site. `(plugins.get(...) or "").strip()` raised a bare
    `AttributeError: 'list' object has no attribute 'strip'` naming no config key — the
    exact defect class the preceding commit in this stack eliminated for the `corpus.*`
    fields, one level down and on a line this change already rewrote."""
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent(f"""
        corpus:
          id: t
          name: T
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: rules
            doc_type: rule
        plugins:
          issuing_body_slug_field: {bad}
    """).strip() + "\n")

    with pytest.raises(ValueError) as e:
        config_mod.load(tmp_path / "_meta" / "corpus.yml")

    assert "issuing_body_slug_field" in str(e.value)


def test_the_basis_string_describes_what_actually_happened_to_sentinels(tmp_path):
    """`basis` is served verbatim as "how attribution was derived". It claimed "else
    path-derived scope slug" unconditionally, which for a sentinel document under a scoped
    root tells a caller it was attributed by its DIRECTORY — the re-attribution the sentinel
    rule exists to prevent, described to the caller as though it had happened."""
    f = _corpus_with_docs(tmp_path, _DOCS, sentinels='["statewide"]')

    basis = f.issuing_body_profile("water-department")["attribution"]["basis"]

    assert "statewide" in basis
    assert "no issuing body" in basis
