"""`search_corpus(issuing_body=...)` takes a registry slug OR the free-text frontmatter
string, and says which one it matched (corpus-toolkit#131).

THE FAILURE THIS CLOSES. Every other tool on this platform that takes a body takes a
REGISTRY SLUG: `issuing_body_profile(slug)` resolves one, `documents_by_agency(slug)`
requires one. This filter matched only `docs.issuing_body` -- the free-text frontmatter
field, which is a sub-unit name ("DAS Enterprise Information Strategy and Policy
Division") and not a slug. A caller holding a slug from either of those tools got `[]`,
which is indistinguishable from "this corpus holds nothing for that body".

It is worse for an agent than for a person: an agent that just resolved a slug has every
reason to reuse it, no signal that this one parameter wants a different kind of string,
and an empty list that reads as a finding. "Could not check" is never "is not there"
(CONTEXT.md) -- and neither is "you named the wrong column".
"""
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp.backends import FileBackend
from corpus_toolkit.mcp.framework import CorpusFramework

DAS = "department-of-administrative-services"
EMPLOYMENT = "employment-department"
# The free-text frontmatter value: a SUB-UNIT of the body the slug names. This is the
# shape real corpora carry, and the reason the two columns cannot be conflated.
DAS_SUBUNIT = "DAS Enterprise Information Strategy and Policy Division"

DOC = """---
schema_version: 1
id: {id}
title: "{title}"
doc_type: policy
citation: "{citation}"
authority_level: agency_policy
issuing_body: "{body}"
{slug_line}source_url: "https://example.invalid/{id}"
source_format: html
retrieved: "2026-08-20"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: "2026-08-20"
verified_by: "@test"
tags: ["t"]
---

## At a glance

A policy about {topic}.

## Full text

The department shall issue a {topic} permit on application.
"""


def _doc(i: int, *, body: str, slug: str | None = None, topic="water") -> str:
    return DOC.format(id=f"pol-{i}", title=f"Policy {i}", citation=f"POL {i}",
                      body=body, sha=str(i) * 64, topic=topic,
                      slug_line=f'agency_registry_slug: "{slug}"\n' if slug else "")


CONFIG = """
    schema_version: 1
    corpus:
      id: test-bodies
      name: Test Bodies
      jurisdiction: oregon
      archetype: document
    content_roots:
      - path: "policies"
        doc_type: "policy"
    plugins:
      issuing_body_slug_field: "agency_registry_slug"
      issuing_body_registry: "_meta/registry.yml"
      issuing_body_registry_key: "entries"
"""

NO_REGISTRY = CONFIG.replace(
    '      issuing_body_registry: "_meta/registry.yml"\n', "").replace(
    '      issuing_body_registry_key: "entries"\n', "")

# In the registry, holding nothing in this corpus. The case a bare `[]` cannot tell apart
# from a caller who named the wrong kind of string.
WATER = "water-resources-department"

REGISTRY = {"entries": [{"slug": DAS, "name": "Department of Administrative Services"},
                        {"slug": EMPLOYMENT, "name": "Employment Department"},
                        {"slug": WATER, "name": "Water Resources Department"}]}


def _corpus(tmp_path: Path, docs: dict[str, str], config_yaml=CONFIG,
            registry=REGISTRY) -> Path:
    for rel, text in docs.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(
        textwrap.dedent(config_yaml).strip() + "\n")
    if registry is not None:
        (tmp_path / "_meta" / "registry.yml").write_text(json.dumps(registry))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def _fw(root: Path) -> CorpusFramework:
    return CorpusFramework(load_config(str(root / "_meta" / "corpus.yml")))


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Two bodies. DAS's documents carry a SUB-UNIT name in `issuing_body` and the
    registry slug in the declared slug field -- so the two columns disagree, which is the
    only arrangement in which this bug is visible at all."""
    return _corpus(tmp_path, {
        "policies/p-1.md": _doc(1, body=DAS_SUBUNIT, slug=DAS),
        "policies/p-2.md": _doc(2, body=DAS_SUBUNIT, slug=DAS),
        "policies/p-3.md": _doc(3, body="Employment Department", slug=EMPLOYMENT),
    })


def test_a_registry_slug_finds_the_documents_attributed_to_that_body(corpus):
    """THE BUG. A caller holding a slug -- from `issuing_body_profile`, from
    `documents_by_agency`, from the registry itself -- passed it here and got nothing."""
    hits = _fw(corpus).search_corpus("permit", issuing_body=DAS)

    assert [h["id"] for h in hits] == ["pol-1", "pol-2"]


def test_the_frontmatter_string_still_filters_exactly_as_it_did(corpus):
    """THE CALLER WHO WAS ALREADY RIGHT. `issuing_body` in frontmatter is the only thing
    this parameter has ever matched, and a corpus repo pinning a new toolkit tag must not
    find its searches answering differently. Anything reachable from a corpus repo is
    public surface (AGENTS.md)."""
    hits = _fw(corpus).search_corpus("permit", issuing_body=DAS_SUBUNIT)

    assert [h["id"] for h in hits] == ["pol-1", "pol-2"]


def test_a_hit_says_which_of_the_two_the_filter_matched(corpus):
    """ACCEPTING BOTH IS ONLY HALF AN ANSWER. Two different questions now reach one
    parameter, and a hit list that does not say which was asked leaves the caller to infer
    it from the data -- which is exactly the inference that produced #131. Each hit names
    the column that matched, so a slug search whose hits carry an unfamiliar
    `issuing_body` string is self-explaining rather than suspicious."""
    by_slug = _fw(corpus).search_corpus("permit", issuing_body=DAS)
    by_string = _fw(corpus).search_corpus("permit", issuing_body=DAS_SUBUNIT)

    assert by_slug[0]["issuing_body_filter"]["value"] == DAS
    assert by_slug[0]["issuing_body_filter"]["matched"] == "registry_slug"
    assert by_string[0]["issuing_body_filter"]["value"] == DAS_SUBUNIT
    assert by_string[0]["issuing_body_filter"]["matched"] == "issuing_body"


def test_an_unfiltered_search_is_untouched(corpus):
    """The blast radius is the parameter under repair and nothing else. A caller that
    passes no `issuing_body` gets the hit shape it always got, key for key."""
    hits = _fw(corpus).search_corpus("permit")

    assert len(hits) == 3
    assert set(hits[0]) == {"id", "title", "citation", "doc_type",
                            "issuing_body", "path", "snippet"}


def test_an_empty_answer_says_which_of_the_two_it_looked_for(corpus):
    """THE PART WORTH FIXING REGARDLESS OF THE REST. `[]` means "no document's <something>
    equalled what you passed", and the caller cannot see which <something> — so a slug that
    names a real body holding nothing here, and a string that is nobody's frontmatter
    value, arrive as the same answer. Two findings, one response.

    A search filtered by issuing body therefore never answers with a bare empty list: it
    answers with ONE record that is not a hit (no `id`, no `path`, no `snippet`) and names
    the column it filtered on."""
    real_body_no_documents = _fw(corpus).search_corpus("permit", issuing_body=WATER)
    string_nobody_carries = _fw(corpus).search_corpus("permit",
                                                      issuing_body="Bureau of Nothing")

    assert len(real_body_no_documents) == len(string_nobody_carries) == 1
    said = real_body_no_documents[0]
    assert said["no_hits"] is True
    assert not {"id", "path", "snippet", "title"} & set(said), (
        "the no-hits record must not be readable as a document hit")
    assert said["issuing_body_filter"]["matched"] == "registry_slug"
    assert string_nobody_carries[0]["issuing_body_filter"]["matched"] == "issuing_body"


def test_a_corpus_with_no_registry_says_the_slug_question_was_never_asked(tmp_path):
    """"COULD NOT CHECK" IS NEVER "IS NOT THERE" (CONTEXT.md, response convention 5).

    Resolution is registry membership, so a corpus that declares no registry cannot decide
    whether the value is a slug -- it filters on the frontmatter field, which is what it
    has always done, and says the other question went unasked. Reporting `matched:
    issuing_body` alone would read as "checked, and it is not a slug", which is a claim
    nobody made. oregon-kpm has its registry commented out and oregon-audits declares
    none, so this is the live case, not the edge one."""
    root = _corpus(tmp_path, {"policies/p-1.md": _doc(1, body=DAS_SUBUNIT, slug=DAS)},
                   config_yaml=NO_REGISTRY, registry=None)

    out = _fw(root).search_corpus("permit", issuing_body=DAS)

    filt = out[0]["issuing_body_filter"]
    assert filt["matched"] == "issuing_body"
    assert filt["registry_checked"] is False
    assert "no issuing-body registry" in filt["note"]


def test_a_corpus_with_a_registry_says_the_value_was_checked_and_is_not_a_slug(corpus):
    """The other half of the same distinction. Checked-and-no is an answer; not-checked is
    not, and a caller chasing an empty result needs to know which it got."""
    out = _fw(corpus).search_corpus("permit", issuing_body="Bureau of Nothing")

    assert out[0]["issuing_body_filter"]["registry_checked"] is True


class _LegacySignatureBackend:
    """A corpus-supplied backend written against `RetrievalBackend.search` AS IT WAS —
    no `issuing_body_slug` keyword, because there was none to write.

    The retrieval seam is public surface and the corpora that supply their own adapter
    (`oregon-legislature`, `oregon-budget`) pin toolkit tags; handing this a keyword its
    signature does not name is `TypeError` on every filtered search."""
    name = "legacy"

    def __init__(self, config, semantic=None):
        self.config = config

    def search(self, query, *, doc_type=None, issuing_body=None, limit=10,
               mode="hybrid"):
        return [{"id": "legacy:1", "title": "Legacy", "citation": "L 1",
                 "doc_type": "policy", "issuing_body": issuing_body or "",
                 "path": "legacy.md", "snippet": "..."}]

    def get(self, doc_id, *, part="auto"):
        return {"id": doc_id, "title": "Legacy", "body": "text"}

    def exists(self, doc_id):
        return {"id": doc_id, "title": "Legacy", "doc_type": "policy"}

    def overview(self):
        return {"documents": 1}

    def health(self):
        return {"reachable": True, "checked_at": None, "detail": "1 document"}


def _with_legacy_backend(root: Path, cls="_LegacySignatureBackend") -> CorpusFramework:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    cfg = root / "_meta" / "corpus.yml"
    # A module name of its OWN: `import` caches by name, and tests/test_backends.py
    # already installs a `backend_mod` from a different tmp corpus. Sharing the name means
    # whichever test ran first decides what the other one loads.
    cfg.write_text(cfg.read_text()
                   + f'  retrieval_module: "slug_backend_mod:{cls}"\n')
    (root / "slug_backend_mod.py").write_text(
        "from tests.test_search_issuing_body import (_LegacySignatureBackend, "
        "_SwallowsUnknownKeywords)\n")
    return CorpusFramework(load_config(str(cfg)))


def test_a_backend_that_cannot_filter_by_slug_is_not_reported_as_having_done_so(corpus):
    """THE GUARD, AND THE CONDITION IT FIRES ON. A backend is asked for a slug filter only
    if its own signature names the parameter — so an older adapter keeps answering
    (additive, per the seam's rules) and, more importantly, its frontmatter answer is
    never dressed up as a slug answer. Claiming `matched: registry_slug` from a backend
    that filtered on something else is a wrong condition reported confidently, which is
    the defect this repo files issues about."""
    fw = _with_legacy_backend(corpus)

    hits = fw.search_corpus("permit", issuing_body=DAS)      # DAS *is* a registry slug

    assert hits[0]["issuing_body_filter"]["matched"] == "issuing_body"
    assert "legacy" in hits[0]["issuing_body_filter"]["note"]
    assert "registry slug" in hits[0]["issuing_body_filter"]["note"]


class _RanksEverything:
    """A semantic module that returns every document, another body's first."""
    def available(self):
        return True

    def rank(self, query, want):
        return ["pol-3", "pol-1", "pol-2"]


def test_the_slug_filter_holds_on_the_semantic_path_too(corpus):
    """A FILTER THAT ONLY ONE RANKER HONOURS IS NOT A FILTER. The semantic branch re-reads
    each candidate's metadata and drops the ones the filters exclude; a slug filter it did
    not know about would let another body's documents in through `hybrid` (the default
    mode) on every corpus that has semantic search configured — a wrong answer, not an
    empty one."""
    cfg = load_config(str(corpus / "_meta" / "corpus.yml"))
    hits = FileBackend(cfg, _RanksEverything()).search(
        "permit", issuing_body_slug=DAS, mode="semantic")

    assert [h["id"] for h in hits] == ["pol-1", "pol-2"]


def test_a_value_that_is_both_a_slug_and_a_frontmatter_string_resolves_to_the_slug(tmp_path):
    """THE DECISION, PINNED. One string, two readings, and this is the one taken.

    The registry slug wins, because it is the identity every OTHER tool on this platform
    takes -- a caller holding a value that IS a slug got it from a slug-shaped tool. The
    choice is made by identity, so it is the same on every call and on every corpus, and
    it is not silent: the hit says `matched: registry_slug`, which is what turns "these are
    not the documents I expected" into a readable answer instead of a mystery. There is
    deliberately no parameter to force the other reading -- a frontmatter `issuing_body` is
    a human-written descriptor and a slug is lower-hyphen-case, so the overlap is
    pathological, and inventing a mode for it would put a second decision in front of every
    caller to spare one who does not exist yet."""
    root = _corpus(tmp_path, {
        "policies/p-1.md": _doc(1, body=DAS_SUBUNIT, slug=DAS),
        # A document whose FREE-TEXT field happens to be spelled like the DAS slug, while
        # it is attributed to a different body.
        "policies/p-9.md": _doc(9, body=DAS, slug=EMPLOYMENT),
    })

    hits = _fw(root).search_corpus("permit", issuing_body=DAS)

    assert [h["id"] for h in hits] == ["pol-1"]
    assert hits[0]["issuing_body_filter"]["matched"] == "registry_slug"


def test_the_no_hits_note_names_every_filter_that_was_applied(corpus):
    """The note must not invite the caller to blame the body filter for an exclusion some
    other filter made. It reports what was searched for -- all of it."""
    out = _fw(corpus).search_corpus("permit", issuing_body=DAS, doc_type="statute")

    assert out[0]["no_hits"] is True
    assert "statute" in out[0]["note"] and DAS in out[0]["note"]


class _SwallowsUnknownKeywords(_LegacySignatureBackend):
    """The worse half of the same problem: a `search` that ACCEPTS every keyword and acts
    on none of them. It cannot raise, so a capability check that trusts `**kwargs` sees a
    slug-filtering backend and gets an unfiltered result back."""
    name = "swallows"

    def search(self, query, *, doc_type=None, issuing_body=None, limit=10,
               mode="hybrid", **kwargs):
        return super().search(query, doc_type=doc_type, issuing_body=issuing_body,
                              limit=limit, mode=mode)


def test_a_backend_that_swallows_the_keyword_is_not_credited_with_filtering(corpus):
    """AN UNFILTERED RESULT LABELLED "FILTERED BY SLUG" IS A WRONG ANSWER, WHERE THE
    DEGRADATION IS ONLY A NARROWER ONE. `**kwargs` is therefore not accepted as evidence
    that a backend can filter by slug — only a signature that NAMES the parameter is, since
    naming it is the one thing a backend cannot do by accident."""
    fw = _with_legacy_backend(corpus, cls="_SwallowsUnknownKeywords")

    hits = fw.search_corpus("permit", issuing_body=DAS)

    assert hits[0]["issuing_body_filter"]["matched"] == "issuing_body", (
        "a backend that ignored the keyword was reported as having filtered on the slug")
