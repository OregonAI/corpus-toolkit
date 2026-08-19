"""`issuing_body_profile` asks the backend, and had no test at all before this file.

TWO THINGS ARE BEING FIXED HERE. The tool used to run raw SQL against FileBackend's `docs`
table through `ensure_index()`, so it was unavailable to any other backend at any price,
and three separate pieces of code existed to keep that from surfacing as a crash: a shim on
CorpusFramework, a `hasattr(backend, "ensure_index")` gate in server.py, and a stderr
warning for the tool that gate silently dropped (corpus-toolkit#75). It now goes through
`RetrievalBackend.holdings_for(slug)`.

And it had NO coverage — `issuing_body_registry` appeared nowhere in tests/, so the only
tool with a config-gated registration, two error shapes and a scoped-path join was never
executed by the suite. Changing its implementation with nothing watching is how the
convention-1 violation of corpus-toolkit#38 survived undocumented for as long as it did.
"""
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp.framework import CorpusFramework

DOC = """---
schema_version: 1
id: {id}
title: "{title}"
doc_type: policy
citation: "{citation}"
authority_level: policy
issuing_body: "Enterprise Information Strategy and Policy Division"
source_url: "https://example.invalid/{id}"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: {mode}
last_verified: "2026-07-26"
verified_by: "@test"
tags: ["t"]
---

## At a glance

A policy.

## Full text

{body}
"""

REGISTRY = {
    "entries": [
        {"slug": "department-of-administrative-services",
         "name": "Department of Administrative Services"},
        {"slug": "employment-department", "name": "Employment Department"},
    ]
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """An ISSUING-BODY-SCOPED corpus: agencies/<slug>/<subdir>/doc.md.

    The scoped layout is the point — `issuing_body_slug` is derived from the PATH
    (config.scope_slug_for), deliberately not from the document's own free-text
    `issuing_body` field, which here is a sub-unit name that matches no registry slug.
    """
    base = tmp_path / "agencies" / "department-of-administrative-services" / "policies"
    base.mkdir(parents=True)
    for i, mode in enumerate(("verbatim", "verbatim", "summary")):
        (base / f"policy-{i}.md").write_text(DOC.format(
            id=f"policy-{i}", title=f"Policy {i}", citation=f"POL {i}",
            sha=str(i) * 64, mode=mode, body=f"The text of policy {i}."))
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "registry.yml").write_text(json.dumps(REGISTRY))
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: test-corpus
          name: Test Corpus
          jurisdiction: oregon
          archetype: document
          authoritative_source: "https://example.invalid/official"
        content_roots:
          - path: agencies
            scoped: true
            subdirs:
              policies: policy
        plugins:
          issuing_body_registry: _meta/registry.yml
    """).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def fw(corpus: Path) -> CorpusFramework:
    return CorpusFramework(load_config(str(corpus / "_meta" / "corpus.yml")))


def test_counts_holdings_by_content_mode(corpus):
    out = fw(corpus).issuing_body_profile("department-of-administrative-services")

    assert out["slug"] == "department-of-administrative-services"
    assert out["registry"]["name"] == "Department of Administrative Services"
    assert out["in_repo"] == {"verbatim": 2, "summary": 1}


def test_a_body_with_nothing_ingested_says_so(corpus):
    """An empty mapping must not read as a count of zero of nothing — the string is the
    documented answer and is what the caller renders."""
    out = fw(corpus).issuing_body_profile("employment-department")

    assert out["in_repo"] == "no documents ingested for this issuing body yet"


def test_free_text_resolves_to_a_unique_slug(corpus):
    out = fw(corpus).issuing_body_profile("Administrative Services")

    assert out["slug"] == "department-of-administrative-services"


def test_an_unmatched_query_errors_with_candidates(corpus):
    out = fw(corpus).issuing_body_profile("no such body")

    assert "error" in out
    assert out["candidates"] == []


def test_every_return_path_carries_the_envelope(corpus):
    """Response convention 1. This tool was the one violation on the surface, and all
    three of its shapes omitted corpus/archetype/authoritative_source
    (corpus-toolkit#38) — with no test, so nothing would have caught a regression."""
    f = fw(corpus)
    for out in (f.issuing_body_profile("department-of-administrative-services"),
                f.issuing_body_profile("no such body")):
        assert out["corpus"] == "test-corpus"
        assert out["archetype"] == "document"
        assert out["authoritative_source"] == "https://example.invalid/official"


def test_no_registry_configured_is_an_explicit_error(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: bare
          name: Bare
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: docs
            doc_type: statute
    """).strip() + "\n")
    out = fw(tmp_path)

    assert "no issuing-body registry" in out.issuing_body_profile("anything")["error"]


# ---------- #71: the count, and whether the count is the whole answer ----------
#
# `in_repo` came from an index column populated ONLY for documents under a `scoped: true`
# content root. Measured on executive-regulatory-frameworks 2026-08-18: 960 of 75,905
# documents, 1.3%. Its Department of Environmental Quality reported 53 documents against
# the 1,929 that carry `agency: department-of-environmental-quality` — a 97% under-report
# served as a confident number, because the field was populated and the call succeeded.
#
# Two things are wrong there and both are fixed below. The JOIN is too narrow, which a
# corpus fixes by declaring which frontmatter field carries its registry slugs. And the
# COUNT DID NOT SAY WHAT IT COULD NOT SEE, which is the part no join fixes: attribution is
# per-document, a corpus may hold documents attributed to nobody, and a bare number cannot
# be told apart from a complete one.

RULE = """---
schema_version: 1
id: {id}
title: "{title}"
doc_type: rule
citation: "{citation}"
authority_level: rule
issuing_body: "A sub-unit name that matches no registry slug"
{agency}source_url: "https://example.invalid/{id}"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: {mode}
last_verified: "2026-07-26"
verified_by: "@test"
tags: ["t"]
---

## At a glance

A rule, filed under its chapter — NOT under the agency that issued it. That is the whole
defect: a rule belongs to a chapter, so no agency directory can ever contain it.

## Full text

{body}
"""

DAS = "department-of-administrative-services"


def _corpus(tmp_path: Path, config_yaml: str, docs: dict[str, str]) -> Path:
    for rel, text in docs.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "registry.yml").write_text(json.dumps(REGISTRY))
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent(config_yaml).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def _policy(i: int, mode: str) -> str:
    return DOC.format(id=f"policy-{i}", title=f"Policy {i}", citation=f"POL {i}",
                      sha=str(i) * 64, mode=mode, body=f"The text of policy {i}.")


def _rule(i: int, mode: str, agency: str | None) -> str:
    return RULE.format(id=f"rule-{i}", title=f"Rule {i}", citation=f"OAR {i}",
                       sha=str(i) * 64, mode=mode, body=f"The text of rule {i}.",
                       agency=f'agency: "{agency}"\n' if agency else "")


DECLARING_FIELD = """
    corpus:
      id: test-corpus
      name: Test Corpus
      jurisdiction: oregon
      archetype: document
      authoritative_source: "https://example.invalid/official"
    content_roots:
      - path: agencies
        scoped: true
        subdirs:
          policies: policy
      - path: rules
        doc_type: rule
    plugins:
      issuing_body_registry: _meta/registry.yml
      issuing_body_slug_field: agency
"""

DECLARING_NOTHING = DECLARING_FIELD.replace("      issuing_body_slug_field: agency\n", "")


@pytest.fixture
def chapter_organised(tmp_path: Path) -> Path:
    """ERF's shape, in miniature, for a corpus that declares its slug field.

    Three DAS policies under `agencies/<slug>/policies/` — the 1.3% a path-derived slug
    reaches — plus five rules under `rules/`, four of them DAS's, which no agency
    directory can contain. One rule is `agency: statewide`: a value naming no registry
    entry, which is an attribution the corpus made deliberately and not a coverage gap.
    """
    docs = {f"agencies/{DAS}/policies/policy-{i}.md": _policy(i, mode)
            for i, mode in enumerate(("verbatim", "verbatim", "summary"))}
    docs.update({f"rules/rule-{i}.md": _rule(i, mode, DAS) for i, mode in
                 enumerate(("verbatim", "verbatim", "verbatim", "summary"))})
    docs["rules/rule-4.md"] = _rule(4, "verbatim", "statewide")
    return _corpus(tmp_path, DECLARING_FIELD, docs)


@pytest.fixture
def every_document_registered(tmp_path: Path) -> Path:
    """Every document resolves to a registry entry: three DAS policies and one DAS rule."""
    docs = {f"agencies/{DAS}/policies/policy-{i}.md": _policy(i, "verbatim")
            for i in range(3)}
    docs["rules/rule-0.md"] = _rule(0, "verbatim", DAS)
    return _corpus(tmp_path, DECLARING_FIELD, docs)


@pytest.fixture
def unattributed_documents(tmp_path: Path) -> Path:
    """The same corpus BEFORE it declares anything: two rules carry no attribution at all,
    and no directory attributes them either. Their documents are counted for no body."""
    docs = {f"agencies/{DAS}/policies/policy-{i}.md": _policy(i, mode)
            for i, mode in enumerate(("verbatim", "verbatim", "summary"))}
    docs.update({f"rules/rule-{i}.md": _rule(i, "verbatim", None) for i in (0, 1)})
    return _corpus(tmp_path, DECLARING_NOTHING, docs)


def test_frontmatter_attributes_the_documents_no_directory_can_reach(chapter_organised):
    """The join half of #71. Four of DAS's five rules live under `rules/`, because a rule
    is filed by chapter; the path-derived slug cannot see any of them, and before this the
    tool reported only the three policies."""
    out = fw(chapter_organised).issuing_body_profile(DAS)

    assert out["in_repo"] == {"verbatim": 5, "summary": 2}


def test_the_declared_field_wins_over_the_directory(tmp_path):
    """Which mechanism wins, stated as a test rather than left to whichever ran last.

    The declared field is the corpus's explicit assertion for a document; the path slug is
    a structural inference from where the file sits. So a policy filed in the Employment
    Department's directory but declaring `agency: department-of-administrative-services`
    counts for DAS, and the directory does not override it."""
    docs = {"agencies/employment-department/policies/policy-0.md":
            _policy(0, "verbatim").replace("doc_type: policy",
                                           f'doc_type: policy\nagency: "{DAS}"')}
    corpus = _corpus(tmp_path, DECLARING_FIELD, docs)

    assert fw(corpus).issuing_body_profile(DAS)["in_repo"] == {"verbatim": 1}
    assert fw(corpus).issuing_body_profile("employment-department")["in_repo"] == (
        "no documents ingested for this issuing body yet")


def test_an_UNDECLARED_out_of_registry_value_is_not_a_complete_count(chapter_organised):
    """`agency: statewide` is a value the registry does not contain, and where the corpus
    has NOT declared it a sentinel, it is indistinguishable from a typo: both are counted
    for NO body. Reporting them as attributed rebuilds #71 one level up — on ERF that is
    37,992 documents, 50.05%, so the corpus would call itself fully attributed while half
    of it reaches no count.

    `complete` therefore means "every document is accounted for", not "every document has a
    non-empty string in a column".

    KEPT IN FORCE THROUGH corpus-toolkit#94, which added the sentinel declaration. This
    fixture declares none, so nothing here changes: the value is still unexplained and the
    count is still a floor. The paired case — the same value, DECLARED — is
    `test_a_declared_sentinel_is_its_own_bucket_and_completes_the_count` in
    tests/test_issuing_body_sentinels.py, and the two together are what stop the
    declaration becoming a way to silence the warning rather than answer it.

    The note used to cite corpus-toolkit#94 as the reason nothing checked these values.
    That issue is closed and the note now names the remedy instead — declare the value as a
    sentinel if it is deliberate — so the assertion moved to the remedy rather than being
    dropped."""
    out = fw(chapter_organised).issuing_body_profile(DAS)

    assert out["attribution"]["complete"] is False
    assert out["attribution"]["documents_in_corpus"] == 8
    assert out["attribution"]["documents_matched_to_a_registry_entry"] == 7
    assert out["attribution"]["documents_naming_no_registry_entry"] == 1
    assert out["attribution"]["documents_declared_no_issuing_body"] == 0
    assert out["attribution"]["documents_with_no_issuing_body"] == 0
    assert "issuing_body_slug_sentinels" in out["attribution"]["note"]
    assert "LOWER BOUND" in out["attribution"]["note"]


def test_a_complete_count_says_it_is_complete(every_document_registered):
    """The other side of the same rule: where every document resolves to a registry entry,
    the count IS the whole answer and the response says so without hedging."""
    out = fw(every_document_registered).issuing_body_profile(DAS)

    assert out["in_repo"] == {"verbatim": 4}
    assert out["attribution"]["complete"] is True
    assert out["attribution"]["documents_in_corpus"] == 4
    assert out["attribution"]["documents_matched_to_a_registry_entry"] == 4
    assert "this count is complete" in out["attribution"]["note"]


def test_a_partial_count_says_it_is_a_lower_bound(unattributed_documents):
    """The half no join fixes. Counts here are exactly what this corpus reported before —
    three policies, nothing else — but two of its five documents are attributed to
    nobody, so the number is a floor and the response now says so in the same breath."""
    out = fw(unattributed_documents).issuing_body_profile(DAS)

    assert out["in_repo"] == {"verbatim": 2, "summary": 1}
    assert out["attribution"]["complete"] is False
    assert out["attribution"]["documents_with_no_issuing_body"] == 2
    assert out["attribution"]["documents_matched_to_a_registry_entry"] == 3
    # The note LEADS WITH THE GAP rather than the matched percentage (corpus-toolkit#94).
    # Phrasing it the other way round read "3 of 5 (60%) are attributed" — fine here, and
    # actively misleading once sentinels exist, where it became "50% attributed" for a
    # corpus whose real gap was one document in 75,905.
    assert "2 of this corpus's 5 documents (40.0%) are unaccounted for" in (
        out["attribution"]["note"])
    assert "The other 3: 3 attributed to a registry body" in out["attribution"]["note"]
    assert "LOWER BOUND" in out["attribution"]["note"]


def test_zero_holdings_in_a_partial_corpus_is_not_none_ingested(unattributed_documents):
    """"Nothing ingested for this body" is a claim about the corpus, and it is only true
    when every document carries an attribution. Where some carry none, the honest answer
    is "nothing I could attribute" — the `no_graph` / `not_in_graph` distinction."""
    out = fw(unattributed_documents).issuing_body_profile("employment-department")

    assert out["in_repo"] != "no documents ingested for this issuing body yet"
    assert "attribution" in out["in_repo"]
    assert out["attribution"]["complete"] is False


def test_a_backend_that_never_measured_coverage_reports_unknown(corpus):
    """v1.25.0's `holdings_for` returned a bare {content_mode: count}. A corpus-supplied
    backend still on that shape keeps working — and gets `complete: null`, not `true`. It
    did not measure coverage, and finding none is a different answer from not looking."""
    f = fw(corpus)

    class LegacyBackend:
        name = "legacy-stub"

        def holdings_for(self, slug):
            return {"verbatim": 3} if slug == DAS else {}

    f.backend = LegacyBackend()
    out = f.issuing_body_profile(DAS)
    empty = f.issuing_body_profile("employment-department")

    assert out["in_repo"] == {"verbatim": 3}
    assert out["attribution"]["complete"] is None
    assert out["attribution"]["basis"] == "unknown"
    assert "UNKNOWN" in out["attribution"]["note"]
    assert empty["in_repo"] != "no documents ingested for this issuing body yet"


def test_a_directory_scoped_corpus_counts_exactly_as_before(corpus):
    """The fixture at the top of this file declares no slug field and keeps every document
    under its scoped root. Its counts must not move, and its coverage is genuinely
    complete — the fallback is a supported way to attribute a corpus, not a legacy path."""
    out = fw(corpus).issuing_body_profile(DAS)

    assert out["in_repo"] == {"verbatim": 2, "summary": 1}
    assert out["attribution"]["complete"] is True
    assert "path-derived" in out["attribution"]["basis"]


# ---------- what the count must never do, and what it must not claim ----------

def test_an_unregistered_declared_value_never_removes_a_document_from_a_count(tmp_path):
    """A COUNT MUST NOT GO DOWN because of an unchecked field.

    `validate/frontmatter.py` fails CI when a scoped path's slug is not in the registry, so
    the path mechanism is checked; nothing checks the declared field (corpus-toolkit#94).
    An earlier draft of this fix let the field win unconditionally, so one typo in `agency:`
    dropped a correctly-filed, CI-validated document out of a count that was previously
    right — and the response still called itself complete. The declared field wins only
    where its value names a registry entry."""
    docs = {f"agencies/{DAS}/policies/policy-0.md":
            _policy(0, "verbatim").replace(
                "doc_type: policy", 'doc_type: policy\nagency: "departmnt-of-admin-srvcs"')}
    corpus = _corpus(tmp_path, DECLARING_FIELD, docs)

    out = fw(corpus).issuing_body_profile(DAS)

    assert out["in_repo"] == {"verbatim": 1}
    assert out["attribution"]["complete"] is True


def test_an_unregistered_declared_value_with_no_directory_is_counted_and_named(tmp_path):
    """Where no directory can rescue it, the same typo must not vanish silently: it lands
    in the "names no registry entry" bucket with a number beside it, and the count that
    lost it says it is a lower bound."""
    docs = {"rules/rule-0.md": _rule(0, "verbatim", "departmnt-of-admin-srvcs")}
    corpus = _corpus(tmp_path, DECLARING_FIELD, docs)

    out = fw(corpus).issuing_body_profile(DAS)

    assert out["attribution"]["complete"] is False
    assert out["attribution"]["documents_naming_no_registry_entry"] == 1
    assert out["attribution"]["documents_matched_to_a_registry_entry"] == 0
    assert "LOWER BOUND" in out["attribution"]["note"]


def test_coverage_reported_without_its_counts_is_unknown_not_complete(corpus):
    """Half-measured is not measured. A backend that returns a `coverage` dict carrying
    only a basis has answered none of the question, and defaulting its missing counts to
    zero would make "nobody measured" arrive as "measured, and nothing is missing"."""
    f = fw(corpus)

    class HalfMeasuringBackend:
        name = "half-measuring-stub"

        def holdings_for(self, slug):
            return {"counts": {"verbatim": 3}, "coverage": {"basis": "our own join"}}

    f.backend = HalfMeasuringBackend()
    out = f.issuing_body_profile(DAS)

    assert out["in_repo"] == {"verbatim": 3}
    assert out["attribution"]["complete"] is None
    assert out["attribution"]["basis"] == "our own join"
    assert "documents" in out["attribution"]["note"]      # names what it did not report
    assert "UNKNOWN" in out["attribution"]["note"]


def test_an_empty_index_is_not_a_corpus_where_everything_is_attributed(tmp_path):
    """Zero documents, zero unattributed, and therefore "complete" — the arithmetic is
    right and the answer is nonsense. An empty index measures nothing, which is why
    platform-deploy's deploy.sh carries a MIN_DOCS abort: an empty index otherwise serves
    green."""
    corpus = _corpus(tmp_path, DECLARING_FIELD, {"rules/.keep": ""})

    out = fw(corpus).issuing_body_profile(DAS)

    assert out["attribution"]["complete"] is None
    assert out["attribution"]["documents_in_corpus"] == 0
    assert "NO documents" in out["attribution"]["note"]
    assert out["in_repo"] != "no documents ingested for this issuing body yet"
