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
import os
import subprocess
import sys
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


# A REAL front door rather than the `.invalid` fixtures elsewhere in this file: this
# config is fed to `corpus-validate-frontmatter` below, and since corpus-toolkit#11 an
# RFC 2606 reserved host in THIS field is an error (it is the template's unfilled
# placeholder). The value is never fetched — nothing in the toolkit follows it.
DECLARING_FIELD = """
    corpus:
      id: test-corpus
      name: Test Corpus
      jurisdiction: oregon
      archetype: document
      authoritative_source: "https://sos.oregon.gov/archives/"
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


def test_an_empty_query_is_refused_on_a_ONE_ENTRY_registry(tmp_path):
    """corpus-toolkit#122. `issuing_body_profile("")` answered with a full profile.

    The tool takes a slug OR a free-text name fragment, and the fallback is a substring
    match: `"" in name` is true for every entry. On a registry holding ONE entry that is
    exactly one hit, the uniqueness test passes, and the tool serves registry identity,
    curated notes and holdings for an agency nobody named.

    ONE ENTRY IS THE WHOLE POINT OF THIS FIXTURE. On a multi-entry registry every entry
    matches, `len(hits) != 1`, and the error path already fires — so the failure is INVERTED
    with corpus size, silent exactly where one match looks like a deliberate answer. A test
    written against the existing two-entry registry would pass without exercising anything.

    Sibling of the empty-slug case corpus-toolkit#123 closed in `documents_by_agency`: an
    empty argument is not a wildcard and not a name fragment, it is a missing one."""
    one = {"entries": [{"slug": "employment-department", "name": "Employment Department"}]}
    root = _corpus(tmp_path, DECLARING_FIELD, {"docs/a.md": _policy(1, "verbatim")})
    (root / "_meta" / "registry.yml").write_text(json.dumps(one))

    for query in ("", "   ", "\t"):
        got = CorpusFramework(load_config(root / "_meta" / "corpus.yml")
                              ).issuing_body_profile(query)
        assert "error" in got, f"{query!r} was answered: {got.get('slug')}"
        assert "slug" not in got or not got.get("registry")


def test_a_padded_slug_still_resolves(tmp_path):
    """The other half, and the trap #123's third review found in the sibling fix: stripping
    for the emptiness test and then looking up the UNSTRIPPED value reports a real slug as
    one the registry does not contain. Strip once, use the stripped value."""
    root = _corpus(tmp_path, DECLARING_FIELD, {"docs/a.md": _policy(1, "verbatim")})

    got = CorpusFramework(load_config(root / "_meta" / "corpus.yml")
                          ).issuing_body_profile(f"  {DAS}  ")

    assert got.get("slug") == DAS, got


# ---------- #128: which registry fields carry a NAME is the corpus's declaration ----------
#
# The free-text fallback matched exactly one field, `name`, which is right only while `name`
# holds the name readers know. `executive-regulatory-frameworks` is mid-migration under its
# ADR 0003: `name` currently holds the OAR chapter title, that title has been copied to
# `oar_name`, and ERF#168 makes `name` the STATUTORY name. Those differ in practice —
# "Business Development Department, Oregon (DBA: Business Oregon)" is one string in the
# financial register, another in the rules index, and a third in statute.
#
# Measured against ERF's committed 189-row registry with that promotion simulated (`name`
# replaced, `oar_name` untouched): matching `name` alone leaves 189 of 189 bodies unfindable
# by the name printed on every OAR citation; `name` + `oar_name` + `aliases` leaves 0.
#
# `oar_name` is ERF-specific and this toolkit is generic, so the field list is CONFIG
# (AGENTS.md: all corpus specifics come from config), defaulting to ["name"].

PROMOTED = {
    "entries": [
        {"slug": DAS,
         # What ERF#168 makes `name`: the name the body's enabling authority gives it.
         "name": "Administrative Services, Department of",
         # What every OAR citation prints, and what a reader is holding when they ask.
         "oar_name": "Oregon Department of Administrative Services",
         "aliases": ["DAS", "State Services Division"]},
        {"slug": "employment-department",
         "name": "Employment Department",
         "oar_name": "Employment Department, Oregon",
         "aliases": []},
    ]
}

DECLARING_NAME_FIELDS = DECLARING_FIELD + (
    '      issuing_body_name_fields: ["name", "oar_name", "aliases"]\n')


def _promoted(tmp_path: Path, config_yaml: str) -> Path:
    """ERF after its `name` promotion, in miniature: the OAR name lives in `oar_name`."""
    root = _corpus(tmp_path, config_yaml, {f"agencies/{DAS}/policies/policy-0.md":
                                           _policy(0, "verbatim")})
    (root / "_meta" / "registry.yml").write_text(json.dumps(PROMOTED))
    return root


def test_a_declared_name_field_finds_a_body_by_the_name_a_reader_holds(tmp_path):
    """The defect, stated as the reader's experience: they query by the name printed on the
    OAR citation, and today's matcher — `name` only — cannot find the body at all."""
    root = _promoted(tmp_path, DECLARING_NAME_FIELDS)

    out = fw(root).issuing_body_profile("Oregon Department of Administrative Services")

    assert out.get("slug") == DAS, out


def test_a_corpus_declaring_nothing_matches_the_name_field_and_nothing_else(tmp_path):
    """THE BACKWARD-COMPATIBILITY BASELINE, and the reason the default is ("name",).

    The same promoted registry, read by a corpus that declares no name fields: the OAR name
    reaches nothing, exactly as on v1.28.0. A corpus adopting a toolkit release must not
    find its queries suddenly resolving to bodies it never declared findable — a wider net
    is the corpus's decision to make, not the toolkit's to make for it.

    Also pins that this test cannot pass by accident: `Administrative Services` still
    resolves through `name`, so the fallback is running and the registry is loadable."""
    root = _promoted(tmp_path, DECLARING_FIELD)

    unmatched = fw(root).issuing_body_profile("Oregon Department of Administrative Services")

    assert "error" in unmatched, unmatched
    assert unmatched["candidates"] == []
    assert fw(root).issuing_body_profile("Administrative Services, Department of")["slug"] == DAS


def test_a_curated_alias_list_is_matched_element_wise(tmp_path):
    """An alias list needs no config key of its own. A declared field whose value is a LIST
    is matched per element, so ERF's `aliases` — a curated, reviewed assertion that two
    strings denote the same body, a stronger signal than any fuzzy match — is declared in
    the same list as `oar_name`."""
    root = _promoted(tmp_path, DECLARING_NAME_FIELDS)

    out = fw(root).issuing_body_profile("State Services Division")

    assert out.get("slug") == DAS, out


def test_a_body_matching_in_several_declared_fields_is_still_ONE_hit(tmp_path):
    """A WIDER NET MUST NOT MANUFACTURE AMBIGUITY. Uniqueness is per BODY, not per name: a
    query hitting a body's `name` and its `oar_name` and two of its aliases is one body
    found, and counting names instead would turn every good match into `no unique issuing
    body match` — the tool refusing to answer precisely where it now knows more."""
    root = _promoted(tmp_path, DECLARING_NAME_FIELDS)

    out = fw(root).issuing_body_profile("Administrative Services")

    assert out.get("slug") == DAS, out


def test_candidates_name_the_field_and_the_name_that_matched(tmp_path):
    """WIDENING THE NET PRODUCES A QUESTION, so the question has to be readable.

    `issuing_body_profile` is a disambiguation surface: it demands a unique hit and
    otherwise hands back candidates for a human or agent to choose between, which is why a
    wider net here is safe where a wider JOIN would not be — it can only ever produce a
    question, never a silent misattribution.

    But `{slug, name}` alone answers a reader who searched by the OAR name with a list of
    STATUTORY names they may never have seen. The candidate now carries the string that
    actually matched and the field it came from, so the reader recognises what they are
    being asked to choose between. `name` keeps its existing meaning and value."""
    root = _promoted(tmp_path, DECLARING_NAME_FIELDS)

    out = fw(root).issuing_body_profile("Oregon")

    assert "error" in out
    assert out["candidates"] == [
        {"slug": DAS,
         "name": "Administrative Services, Department of",
         "matched_field": "oar_name",
         "matched_name": "Oregon Department of Administrative Services"},
        {"slug": "employment-department",
         "name": "Employment Department",
         "matched_field": "oar_name",
         "matched_name": "Employment Department, Oregon"},
    ]


def test_candidates_carry_the_same_keys_for_a_corpus_declaring_nothing(corpus):
    """The shape is UNIFORM, not conditional. A caller that renders candidates must not
    have to branch on whether the corpus declared anything — the same reasoning that keeps
    `attribution` present on every success. Where nothing is declared the two new keys
    always read `name` and the entry's name, which is the truth about that match."""
    out = fw(corpus).issuing_body_profile("Department")

    assert out["candidates"] == [
        {"slug": DAS, "name": "Department of Administrative Services",
         "matched_field": "name", "matched_name": "Department of Administrative Services"},
        {"slug": "employment-department", "name": "Employment Department",
         "matched_field": "name", "matched_name": "Employment Department"},
    ]


# ---------- the declaration is CHECKED at load, because every way of getting it wrong is
# silent otherwise: a name field that reaches no registry cell matches nothing, and
# "matches nothing" is exactly what an unfindable body looks like from the outside.

@pytest.mark.parametrize("declared, phrase, why", [
    ("[]", "empty list", "an empty list declares nothing"),
    ("", "declared with no value", "the key present with no value declares nothing"),
    ("oar_name", "got str", "a bare string would iterate as its characters"),
    ('{name: oar_name}', "got dict", "a mapping is not a list of field names"),
    ('["name", ""]', "non-empty", "an empty field name reaches no registry cell"),
    ('["name", 7]', "got 7", "a non-string is not a field name"),
])
def test_a_malformed_name_field_declaration_fails_at_load(tmp_path, declared, phrase, why):
    """EACH CASE PINS THE MESSAGE ITS OWN GUARD PRODUCES. Asserting only the config key name
    is a check that passes without checking anything — every branch names the key, so the
    wrong guard firing (or one guard swallowing every case) reads as six working checks."""
    root = _promoted(tmp_path, DECLARING_FIELD + f"      issuing_body_name_fields: {declared}\n")

    with pytest.raises(ValueError) as e:
        load_config(root / "_meta" / "corpus.yml")

    assert "issuing_body_name_fields" in str(e.value), why
    assert phrase in str(e.value), f"{why}: wrong guard fired — {e.value}"


def test_name_fields_without_a_registry_is_a_config_error(tmp_path):
    """Sibling of the sentinels-without-a-slug-field check. The fields name columns of the
    ISSUING-BODY REGISTRY, so with no registry declared there is nothing for them to name:
    the corpus has widened a matcher that never runs, and would read its unfindable bodies
    as a data problem."""
    config = DECLARING_FIELD.replace("      issuing_body_registry: _meta/registry.yml\n", "")
    root = _promoted(tmp_path, config + '      issuing_body_name_fields: ["name", "oar_name"]\n')

    with pytest.raises(ValueError) as e:
        load_config(root / "_meta" / "corpus.yml")

    assert "issuing_body_name_fields" in str(e.value)
    assert "issuing_body_registry" in str(e.value)


def test_a_corpus_declaring_nothing_gets_the_documented_default(corpus):
    """The default is a real value a corpus can read back, not an implicit branch."""
    assert load_config(corpus / "_meta" / "corpus.yml").issuing_body_name_fields == ("name",)


def test_a_registry_cell_that_is_not_a_string_is_skipped_not_coerced(tmp_path):
    """A registry is hand-maintained YAML, so a declared field can hold anything. Coercing
    with `str()` invents names nobody wrote — `aliases: [~]` would make the query "none"
    match a body — and calling `.lower()` on it takes the whole tool down instead."""
    root = _promoted(tmp_path, DECLARING_NAME_FIELDS)
    registry = json.loads((root / "_meta" / "registry.yml").read_text())
    registry["entries"][0]["oar_name"] = 1988
    registry["entries"][0]["aliases"] = [None, {"was": "DAS"}, "State Services Division"]
    (root / "_meta" / "registry.yml").write_text(json.dumps(registry))
    f = fw(root)

    assert "error" in f.issuing_body_profile("none")
    assert "error" in f.issuing_body_profile("1988")
    assert f.issuing_body_profile("State Services Division").get("slug") == DAS


def test_a_field_declared_twice_is_scanned_once(tmp_path):
    """Order decides which field a candidate reports as the one that matched, so the list
    is order-preserving; a repeat cannot match anything the first scan missed."""
    root = _promoted(
        tmp_path,
        DECLARING_FIELD + '      issuing_body_name_fields: ["oar_name", "name", "oar_name"]\n')

    cfg = load_config(root / "_meta" / "corpus.yml")

    assert cfg.issuing_body_name_fields == ("oar_name", "name")
    assert fw(root).issuing_body_profile("Oregon Department of Administrative")[
        "slug"] == DAS


def test_a_malformed_registry_cell_no_longer_takes_the_tool_down(tmp_path):
    """THE ONE RESPECT IN WHICH A CORPUS DECLARING NOTHING IS NOT BYTE-IDENTICAL, pinned
    here rather than left for a corpus to discover.

    v1.28.0 matched `q in o.get("name", "").lower()`, so a registry entry whose `name` is
    null, numeric or a list raised `AttributeError: 'NoneType' object has no attribute
    'lower'` — taking down EVERY free-text query against that registry, not just one naming
    the bad entry. A hand-maintained YAML file makes that a data typo away.

    Skipping is the fix, and `str()` is not: coercing would make the query "none" match a
    body. A LIST-valued `name` is the case where the two differ visibly — it crashed before
    and is matched element-wise now, so this corpus finds a body v1.28.0 could not. An
    exception is not behaviour worth preserving, but it IS a difference, so it is a test and
    a CHANGELOG line rather than a footnote."""
    root = _promoted(tmp_path, DECLARING_FIELD)          # declares NO name fields
    registry = {"entries": [{"slug": DAS, "name": None},
                            {"slug": "employment-department",
                             "name": ["Employment Department", "Employment Board"]}]}
    (root / "_meta" / "registry.yml").write_text(json.dumps(registry))
    f = fw(root)

    assert f.config.issuing_body_name_fields == ("name",)
    assert "error" in f.issuing_body_profile("Administrative")     # was AttributeError
    assert f.issuing_body_profile("Employment Board")["slug"] == "employment-department"


# ---------- #129: a declared name field the registry does not carry ----------
#
# The declaration is checked at LOAD for shape (empty list, bare string, non-string entry,
# no registry to name columns of) and not against the registry it names columns of, so
# `oar_nmae` loads clean, serves clean, and matches nothing — indistinguishable from a body
# that is not there.
#
# REPORTED, NOT FATAL, and the operator decision is the reason: a mid-migration corpus
# legitimately declares a field its registry is about to grow (ERF declared `oar_name`
# between #166 and #168), so refusing the load would break a config that is correct and
# merely early.
#
# IT SURFACES IN `corpus-validate-frontmatter`, alongside `corpus.authoritative_source` —
# the corpus-level config channel that already exists, that every corpus runs on every PR
# through the validate-frontmatter reusable workflow, and that a maintainer already reads
# for exactly this class of finding. Not a new channel, and not the MCP response: an agent
# calling `corpus_overview` cannot fix a registry column, and `config_warning` is spent on
# the one finding that changes how an agent should read the answer it is holding.

def _config_only(tmp_path: Path, declared: str, registry=PROMOTED) -> Path:
    """A corpus with a registry, a name-field declaration and NO content files.

    The seam under test is the corpus-level config check, which runs whether or not the
    corpus holds documents; keeping the content out means an unrelated frontmatter error
    cannot be mistaken for this finding, or mask it.
    """
    root = tmp_path
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "agencies").mkdir(exist_ok=True)
    (root / "rules").mkdir(exist_ok=True)
    (root / "_meta" / "registry.yml").write_text(json.dumps(registry))
    (root / "_meta" / "corpus.yml").write_text(
        textwrap.dedent(DECLARING_FIELD + declared).strip() + "\n")
    return root


def _validate(root: Path) -> subprocess.CompletedProcess:
    """Run the validator the way a corpus's CI does, from the corpus root.

    THIS interpreter and THIS checkout, pinned: `python3` plus a bare cwd would run
    whichever `corpus_toolkit` happened to be importable there, so the guard could pass
    against an install that does not contain the code under test.
    """
    return subprocess.run(
        [sys.executable, "-m", "corpus_toolkit.validate.frontmatter",
         "--config", "_meta/corpus.yml"],
        cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})


def test_a_name_field_no_registry_entry_carries_is_reported_by_the_validator(tmp_path):
    """The typo case. `oar_nmae` reaches no cell, so every query against it matches
    nothing — and nothing anywhere says so today."""
    root = _config_only(tmp_path, '      issuing_body_name_fields: ["name", "oar_nmae"]\n')

    out = _validate(root)

    assert "oar_nmae" in out.stdout, out.stdout
    assert "issuing_body_name_fields" in out.stdout, out.stdout
    assert "warning" in out.stdout, f"reported, not fatal — {out.stdout}"


def test_the_finding_is_reported_and_does_not_fail_the_run(tmp_path):
    """A corpus mid-migration declares the field its registry is about to grow. That corpus
    must still validate — the finding is a report, and making it fatal would refuse a config
    that is correct and merely early."""
    root = _config_only(tmp_path, '      issuing_body_name_fields: ["name", "oar_nmae"]\n')

    out = _validate(root)

    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAILED" not in out.stdout, out.stdout


def test_a_name_field_the_registry_carries_is_not_reported(tmp_path):
    """THE GUARD MUST NOT FIRE ON THE RIGHT CONFIG. `name`, `oar_name` and `aliases` are all
    carried by the promoted registry; a check that reports them too is a check that reports
    everything, which is the same as reporting nothing."""
    root = _config_only(
        tmp_path, '      issuing_body_name_fields: ["name", "oar_name", "aliases"]\n')

    out = _validate(root)

    assert "issuing_body_name_fields" not in out.stdout, out.stdout
    assert out.returncode == 0, out.stdout


def test_a_field_only_some_entries_carry_is_not_reported(tmp_path):
    """A registry mid-migration carries the field on some rows and not others. That is a
    partially-populated column, not a field name that reaches nothing, and the two are
    different findings — only the second one makes every query against it fail."""
    registry = json.loads(json.dumps(PROMOTED))
    del registry["entries"][1]["oar_name"]
    root = _config_only(tmp_path,
                        '      issuing_body_name_fields: ["name", "oar_name", "aliases"]\n',
                        registry=registry)

    out = _validate(root)

    assert "oar_name" not in out.stdout, out.stdout


def test_a_field_whose_every_cell_is_unmatchable_is_reported(tmp_path):
    """`oar_name: null` on every row reaches no NAME, which is the same silent outcome as a
    misspelled field: `_name_match` skips a cell that is not a string. Checking only for the
    KEY would pass here while every query against the field still matched nothing."""
    registry = json.loads(json.dumps(PROMOTED))
    for entry in registry["entries"]:
        entry["oar_name"] = None
    root = _config_only(tmp_path,
                        '      issuing_body_name_fields: ["name", "oar_name", "aliases"]\n',
                        registry=registry)

    out = _validate(root)

    assert "oar_name" in out.stdout, out.stdout
    assert "issuing_body_name_fields" in out.stdout, out.stdout


def test_an_unreadable_registry_is_not_reported_as_an_unmatched_field(tmp_path):
    """"COULD NOT CHECK" IS NOT "IS NOT THERE". A registry that cannot be read says nothing
    about which fields it carries, and reporting the declared fields as unmatched would be a
    finding invented from a failure to look — the collapse response convention 5 and
    `_registry_slugs_at_load` both exist to prevent."""
    root = _config_only(tmp_path,
                        '      issuing_body_name_fields: ["name", "oar_name", "aliases"]\n')
    (root / "_meta" / "registry.yml").write_text("entries: [ this is not valid yaml\n")

    out = _validate(root)

    claim = "claimed a field is carried by no entry of a registry it could not read"
    assert "no entry in" not in out.stdout, f"{claim} — {out.stdout}"
    assert "could not be read" in out.stdout, out.stdout
    assert "could not be checked" in out.stdout, out.stdout
    assert "Traceback" not in out.stderr, out.stderr


def test_a_registry_entry_with_no_slug_is_reported(tmp_path):
    """A registry row with no `slug` is a broken row: nothing can be attributed to it, and
    a document naming that body is reported as unregistered. It used to raise `KeyError`
    out of the validator's registry load — a traceback naming neither the file nor the row,
    but a loud failure. Skipping it silently would trade a bad message for no message."""
    registry = {"entries": [{"slug": DAS, "name": "Administrative Services, Department of"},
                            {"name": "A body nobody gave a slug"}]}
    root = _config_only(tmp_path, "", registry=registry)

    out = _validate(root)

    assert "no slug" in out.stdout, out.stdout
    assert "registry.yml" in out.stdout, out.stdout
    assert out.returncode == 1, "a broken registry row must still fail the run"
    assert "Traceback" not in out.stderr, out.stderr


def test_the_default_name_field_is_reported_as_the_default(tmp_path):
    """A corpus that declared nothing must not be told off for a key it never wrote. The
    finding is the same — `name` reaches no cell, so every free-text query fails — but it
    is the DEFAULT that reaches nothing, and the fix is to declare the field the registry
    actually carries."""
    registry = {"entries": [{"slug": DAS, "oar_name": "Oregon Department of Administrative "
                                                      "Services"}]}
    root = _config_only(tmp_path, "", registry=registry)

    out = _validate(root)

    assert "default" in out.stdout, out.stdout
    assert "'name'" in out.stdout, out.stdout
    assert out.returncode == 0, out.stdout
