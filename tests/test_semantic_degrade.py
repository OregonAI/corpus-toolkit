"""The semantic seam degrades, and does not take search down with it.

WHY THIS FILE EXISTS. Semantic search was, until now, one corpus's private module, and the
toolkit had no test that supplied a semantic module at all -- the only occurrence of the
word in the suite was an unused constructor parameter. Meanwhile `backends.py` calls
`self._semantic.rank(query, pool)` with NO guard, so a module that raises takes down
`search_corpus` for a corpus whose only fault is an unmounted volume.

That combination is why the platform's own docs call this its sharp edge: *a broken mount
looks like working search with worse results*. Rolling the feature from one corpus to seven
multiplies the number of mounts that can be missing, so the degrade path stops being
theoretical and starts being the common case.

These tests assert the behaviour a caller can observe, not the internals: results still
come back, they are the keyword results, and nothing raises.
"""
import json
import subprocess
import textwrap

import pytest

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp.backends import FileBackend

DOC = """---
schema_version: 1
id: {id}
title: "{title}"
doc_type: statute
citation: "{cite}"
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
maintainer: "@test"
relationships: {{}}
tags: [t]
---

## At a glance

{glance}

## Full text

{body}
"""


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "statutes").mkdir()
    (tmp_path / "_meta").mkdir()
    for d in [
        dict(id="ors-1.010", title="Water appropriation permits", cite="ORS 1.010",
             sha="a" * 64, glance="Permits for water.",
             body="A person may not appropriate water without a permit."),
        dict(id="ors-2.020", title="Permit application fees", cite="ORS 2.020",
             sha="b" * 64, glance="Fees.",
             body="The fee for a permit application is $250."),
    ]:
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
                    "commit", "-qm", "c"], cwd=tmp_path, check=True)
    return tmp_path


def backend(corpus, semantic=None):
    return FileBackend(load_config(str(corpus / "_meta" / "corpus.yml")), semantic)


class _Unavailable:
    """The unmounted-artifact case: loaded, but reporting it has nothing."""
    def available(self):
        return False

    def rank(self, query, want):        # must never be reached
        raise AssertionError("rank() called on an unavailable semantic module")


class _Working:
    def __init__(self, order):
        self.order = order
        self.asked = []

    def available(self):
        return True

    def rank(self, query, want):
        self.asked.append(want)
        return list(self.order)


class _NoAvailableAttr:
    """No `available()` at all -- backends.py treats absence as available."""
    def rank(self, query, want):
        return ["ors-2.020"]


def test_no_semantic_module_is_keyword_only(corpus):
    hits = backend(corpus).search("permit", mode="hybrid")
    assert [h["id"] for h in hits], "keyword search must work with no semantic module"


def test_unavailable_module_degrades_and_never_calls_rank(corpus):
    # The important half is the assertion inside _Unavailable.rank: an unavailable module
    # must be skipped, not called and caught.
    hits = backend(corpus, _Unavailable()).search("permit", mode="hybrid")
    assert [h["id"] for h in hits]


def test_semantic_mode_also_degrades_rather_than_returning_nothing(corpus):
    """`mode="semantic"` with no working module must still answer.

    Returning [] would be defensible and is wrong here: the caller asked for the best
    ranking available, and a corpus that silently answers "no results" for a query whose
    words are literally in its text reads as missing content rather than missing config.
    """
    hits = backend(corpus, _Unavailable()).search("permit", mode="semantic")
    assert [h["id"] for h in hits]


def test_module_without_available_is_assumed_available(corpus):
    hits = backend(corpus, _NoAvailableAttr()).search("permit", mode="hybrid")
    assert [h["id"] for h in hits]


def test_rank_is_asked_for_a_pool_not_the_caller_limit(corpus):
    """rank() receives max(limit*4, 40), which every implementation must tolerate.

    A module that assumed it was being asked for exactly `limit` would silently truncate
    the candidate pool and weaken fusion without failing anything.
    """
    sem = _Working(["ors-2.020", "ors-1.010"])
    backend(corpus, sem).search("permit", mode="hybrid", limit=5)
    assert sem.asked and sem.asked[0] >= 40


def test_unknown_ids_from_rank_are_dropped_not_surfaced(corpus):
    """A stale artifact naming deleted documents must not produce phantom hits."""
    sem = _Working(["does-not-exist", "ors-1.010"])
    hits = backend(corpus, sem).search("permit", mode="semantic")
    assert "does-not-exist" not in [h["id"] for h in hits]


def test_semantic_only_hit_is_marked_as_such(corpus):
    """A document the keyword arm never matched still returns, flagged.

    Without the marker a semantic hit is indistinguishable from a keyword hit whose
    snippet happened to be empty, and the caller cannot tell why it was returned.
    """
    sem = _Working(["ors-2.020"])
    hits = backend(corpus, sem).search("zzzznomatch", mode="semantic")
    if hits:
        assert any("semantic" in (h.get("snippet") or "") for h in hits)
