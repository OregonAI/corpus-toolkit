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
import re
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


def test_file_backend_cites_the_document_not_the_corpus(corpus):
    """The built-in path's side of corpus-toolkit#90's precedence, which nothing asserted.

    `FileBackend` derives `source_url` and `authoritative_source` from the SAME document
    column, so it lands on step 1 of the precedence and can never observe steps 2 or 3 —
    which is exactly why the missing middle step stayed invisible for as long as it did.
    Pinned here so a future change to the precedence has to keep the built-in path citing
    the document rather than the corpus."""
    d = fw(corpus).get_document("ors-2.020")

    assert d["authoritative_source"] == "https://example.invalid/ors-2.020"
    assert d["authoritative_source"] == d["source_url"]


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
        # `source_url` and NO `authoritative_source`, which is exactly what the documented
        # `RetrievalBackend.get()` contract permits ("Record metadata + body"). This stub
        # used to omit both, which is why corpus-toolkit#90 was invisible to the suite: the
        # only tests of this fallback went through FileBackend, which happens to populate
        # both keys from one column.
        return {"id": doc_id, "title": "Stub", "body": "live",
                "source_url": "https://api.invalid/records/42",
                "executed_at": "2026-07-26T00:00:00Z"}

    def exists(self, doc_id):
        return {"id": doc_id, "title": "Stub", "doc_type": "record"}

    def overview(self):
        return {"documents_by_type": {"record": 1}, "commit": ""}

    def health(self):
        return {"reachable": True, "checked_at": "2026-07-26T00:00:00Z", "detail": "stub"}


class _BrokenBackend(_StubBackend):
    """Missing health() — the kind of adapter bug that must fail at startup."""
    health = None


class _EnvelopeClobberingBackend(_StubBackend):
    """A backend that returns the three envelope keys from the two methods whose return
    value CorpusFramework merges into a response (corpus-toolkit#102, #104).

    Neither documented contract forbids this. `get()` is "Record metadata + body", and
    `overview()` is "Backend-shaped facts ... counts, commit/source stamp" — and a source
    stamp is exactly what a proxy backend would reach for a key like `corpus` to express.
    So the toolkit has to enforce the envelope rather than trust every backend author to
    remember it.
    """
    name = "clobbering"
    CLOBBER = {"corpus": "upstream-odata-feed",
               "archetype": "api",
               "authoritative_source": "https://upstream.invalid/feed"}

    def get(self, doc_id, *, part="auto"):
        # No "id" key, so this takes get_document's NOT-FOUND branch.
        return {"error": f"no document with id {doc_id!r}", **self.CLOBBER}

    def overview(self):
        # `disclaimer` and `jurisdiction` are clobbered HERE rather than in CLOBBER because
        # only `corpus_overview` assembles them; the not-found branch has neither.
        return {"documents_by_type": {"record": 1}, "commit": "",
                "disclaimer": "UPSTREAM TERMS OF USE APPLY",
                "jurisdiction": "elsewhere",
                **self.CLOBBER}

    # Clobbered too, so the sweep below catches a FUTURE site that starts splatting one of
    # these. Neither is merged today — `exists` is read as a truthiness check in one place
    # and picked apart by name in the other, and `holdings_for` goes through `_holdings`.
    def exists(self, doc_id):
        return {"id": doc_id, "title": "Stub", "doc_type": "record", **self.CLOBBER}

    def holdings_for(self, slug):
        return {"counts": {"verbatim": 1},
                "coverage": {"documents": 1, "in_registry": 1, "no_registry_entry": 0,
                             "unattributed": 0, "basis": "stub", **self.CLOBBER}}


class _RecordWithItsOwnSourceBackend(_StubBackend):
    """A backend whose get() SUCCEEDS and supplies the document's own authoritative_source.

    This is the one place a backend is meant to win, and it is the case FileBackend hits on
    every call — so it needs pinning separately from the two branches being fixed.
    """
    name = "own-source"

    def get(self, doc_id, *, part="auto"):
        # DELIBERATELY DIFFERENT from source_url. FileBackend sets both from one column, so
        # equal values cannot tell "authoritative_source won" from "source_url won" — which
        # is the distinction corpus-toolkit#90's precedence turns on.
        return {"id": doc_id, "title": "Stub", "body": "live",
                "source_url": "https://api.invalid/records/42",
                "authoritative_source": "https://official.invalid/canonical/42"}


class _RecordWithNoSourceBackend(_StubBackend):
    """A record offering nothing more precise than the corpus: no `authoritative_source`,
    and an EMPTY `source_url`. Empty and absent are both "not supplied"."""
    name = "no-source"

    def get(self, doc_id, *, part="auto"):
        return {"id": doc_id, "title": "Stub", "body": "live", "source_url": ""}


class _RecordOmittingSourceBackend(_StubBackend):
    """The same, with the key absent rather than empty."""
    name = "omits-source"

    def get(self, doc_id, *, part="auto"):
        return {"id": doc_id, "title": "Stub", "body": "live"}


class _RecordWithNonStringSourceUrlBackend(_StubBackend):
    """`source_url` as a list of mirrors — plausible for a proxy backend, and permitted:
    the protocol never types this key. It is not a candidate for a field declared
    `str | None`."""
    name = "list-source"

    def get(self, doc_id, *, part="auto"):
        return {"id": doc_id, "title": "Stub", "body": "live",
                "source_url": ["https://a.invalid/42", "https://b.invalid/42"]}


class _NonStringEnvelopeBackend(_EnvelopeClobberingBackend):
    """The same, with values the declared ResponseEnvelope cannot accept.

    Since corpus-toolkit#103 the envelope types `corpus` and `archetype` as `str`, so a
    None here is a hard ValidationError at serialization — a tool error on a path a corpus
    author has no reason to expect one. The framework must never let the value get that
    far.
    """
    name = "non-string"
    CLOBBER = {"corpus": None, "archetype": None, "authoritative_source": []}


def _with_backend(corpus: Path, factory_path: str) -> CorpusFramework:
    cfg_path = corpus / "_meta" / "corpus.yml"
    cfg_path.write_text(cfg_path.read_text() + textwrap.dedent(f"""
        plugins:
          retrieval_module: "{factory_path}"
    """))
    (corpus / "backend_mod.py").write_text(
        "from tests.test_backends import (_StubBackend, _BrokenBackend, "
        "_EnvelopeClobberingBackend, _NonStringEnvelopeBackend, "
        "_RecordWithItsOwnSourceBackend, _RecordWithNoSourceBackend, "
        "_RecordOmittingSourceBackend, _RecordWithNonStringSourceUrlBackend)\n")
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


def test_not_found_response_names_this_corpus_even_when_the_record_names_another(corpus):
    """corpus-toolkit#102. The not-found branch merged the backend's error record OVER the
    envelope, so a record carrying `corpus` renamed the corpus on the one response an agent
    gets when it guesses an id wrong — the response it is most likely to misread, and the
    exact failure #38 fixed, re-openable from the backend side.

    The success branch immediately below it already re-asserts these two after the record;
    this branch did not."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_EnvelopeClobberingBackend")

    d = f.get_document("no-such-doc")

    assert "error" in d                      # still the not-found branch
    assert d["corpus"] == "test-corpus"      # config's value, not the record's
    assert d["archetype"] == "document"


def test_not_found_response_keeps_the_corpus_level_authoritative_source(corpus):
    """corpus-toolkit#102, the third field. There is no document on this branch, so there
    is no `source_url` to be more precise with — the corpus-level value is the only correct
    answer, and the record cannot supply a better one.

    This fixture declares no `corpus.authoritative_source`, so the right answer is `null`:
    a documented value meaning "this corpus declares no front door", which convention 1
    requires be distinguishable from a key nobody answered. A backend URL arriving in that
    slot is the collapse this platform never makes."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_EnvelopeClobberingBackend")

    d = f.get_document("no-such-doc")

    assert "authoritative_source" in d       # present, per convention 1
    assert d["authoritative_source"] is None


def test_corpus_overview_names_this_corpus_even_when_the_backend_names_another(corpus):
    """corpus-toolkit#104 — the same defect as #102 at the second and only other site where
    the framework merges a backend-supplied mapping into an enveloped response.

    `corpus_overview` is the tool the server's own instructions tell a client to call
    FIRST, so a corpus misreporting its own identity here misinforms every session that
    starts correctly. `overview()`'s documented contract is "counts, commit/source stamp" —
    a source stamp is exactly what a proxy backend would express under a key like `corpus`,
    which makes this a plausible mistake rather than a contrived one."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_EnvelopeClobberingBackend")

    o = f.corpus_overview()

    assert o["corpus"] == "test-corpus"
    assert o["archetype"] == "document"
    assert o["authoritative_source"] is None
    # and the backend's own facts still arrive — this displaces three keys, drops nothing
    assert o["documents_by_type"] == {"record": 1}


def test_a_non_string_from_the_backend_never_reaches_the_declared_envelope(corpus):
    """The loud half of corpus-toolkit#102/#104, and the reason the fix belongs in the
    framework rather than in each backend's memory.

    Since #103 the declared ResponseEnvelope types `corpus` and `archetype` as `str`, so a
    None arriving in either slot is a hard ValidationError at serialization — a tool error
    on a path a corpus author has no reason to expect one. Because the framework now
    re-asserts the envelope, the offending value never gets that far and the response is
    simply CORRECT rather than loudly broken.

    Asserted through `ResponseEnvelope.model_validate` because that is what the SDK does to
    these responses on the way out; a framework-level assertion alone would not prove the
    wire path is safe."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from corpus_toolkit.mcp.responses import ResponseEnvelope
    f = _with_backend(corpus, "backend_mod:_NonStringEnvelopeBackend")

    for response in (f.get_document("no-such-doc"), f.corpus_overview()):
        assert response["corpus"] == "test-corpus"
        assert response["archetype"] == "document"
        # serializes rather than raising — this is the assertion that matters
        assert ResponseEnvelope.model_validate(response).corpus == "test-corpus"


def _corpus_declaring_a_front_door(corpus: Path, factory_path: str,
                                   front_door: str | None = "https://front.invalid/door"):
    """The corpus fixture with a declared `corpus.authoritative_source`, plus a backend.

    The plain fixture declares none, so it cannot show the front door being stamped OVER a
    per-document URL — the whole of corpus-toolkit#90. `front_door=None` gives back the
    undeclared case, for the `null` assertions.
    """
    # dedent BEFORE interpolating: an interpolated line carries its own indentation and
    # would otherwise redefine the common prefix dedent strips from every other line.
    config = textwrap.dedent("""
        corpus:
          id: test-corpus
          name: Test Corpus
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: statutes
            doc_type: statute
        graph_path: _meta/graph.json
        plugins:
          retrieval_module: "{factory_path}"
    """).strip().format(factory_path=factory_path)
    if front_door:
        config = config.replace("  archetype: document",
                                f'  archetype: document\n  authoritative_source: "{front_door}"')
    (corpus / "_meta" / "corpus.yml").write_text(config + "\n")
    (corpus / "backend_mod.py").write_text(
        "from tests.test_backends import (_StubBackend, _BrokenBackend, "
        "_EnvelopeClobberingBackend, _NonStringEnvelopeBackend, "
        "_RecordWithItsOwnSourceBackend, _RecordWithNoSourceBackend, "
        "_RecordOmittingSourceBackend, _RecordWithNonStringSourceUrlBackend)\n")
    return CorpusFramework(load_config(str(corpus / "_meta" / "corpus.yml")))


def _sweep_corpus(corpus: Path, factory_path: str) -> CorpusFramework:
    """The corpus fixture with a REAL graph and a REAL issuing-body registry.

    The plain fixture has an empty `graph.json` and no registry, so `graph_neighbors`,
    `authority_chain` and `issuing_body_profile` all return their early error envelopes and
    never reach the code that consumes a backend mapping. A sweep run against it asserts
    only that four error shapes carry an envelope — which they do by construction — and
    would go green over a future site that splatted `holdings_for()`'s mapping. Reaching the
    success path is the entire point of the sweep.
    """
    (corpus / "_meta" / "registry.yml").write_text(json.dumps(
        {"entries": [{"slug": "water-department", "name": "Water Department"}]}))
    (corpus / "_meta" / "graph.json").write_text(json.dumps({
        "nodes": [{"id": "ors-1.010", "title": "Definitions", "doc_type": "statute"},
                  {"id": "ors-2.020", "title": "Fees", "doc_type": "statute"}],
        "edges": [{"from": "ors-2.020", "to": "ors-1.010", "type": "implements"}]}))
    (corpus / "_meta" / "corpus.yml").write_text(textwrap.dedent(f"""
        corpus:
          id: test-corpus
          name: Test Corpus
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: statutes
            doc_type: statute
        graph_path: _meta/graph.json
        plugins:
          retrieval_module: "{factory_path}"
          issuing_body_registry: _meta/registry.yml
    """).strip() + "\n")
    (corpus / "backend_mod.py").write_text(
        "from tests.test_backends import (_StubBackend, _BrokenBackend, "
        "_EnvelopeClobberingBackend, _NonStringEnvelopeBackend, "
        "_RecordWithItsOwnSourceBackend, _RecordWithNoSourceBackend, "
        "_RecordOmittingSourceBackend, _RecordWithNonStringSourceUrlBackend)\n")
    return CorpusFramework(load_config(str(corpus / "_meta" / "corpus.yml")))


def test_no_enveloped_response_anywhere_lets_the_backend_displace_the_envelope(corpus):
    """The guard that makes corpus-toolkit#102/#104 stay fixed.

    #102 listed the sites it believed were safe and cleared `corpus_overview`, which was
    the second instance — found only because someone re-audited the list by hand rather
    than trusting it. An inventory written once in prose goes stale; this sweeps every
    enveloped tool through a backend that clobbers every method it can, so a THIRD site
    added later fails here instead of shipping.

    `search_corpus` is absent deliberately: it is list-shaped and carries no envelope, per
    the documented exemption in response convention 1."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _sweep_corpus(corpus, "backend_mod:_EnvelopeClobberingBackend")

    responses = {
        "get_document": f.get_document("no-such-doc"),
        "corpus_overview": f.corpus_overview(),
        "resolve_citation": f.resolve_citation("ORS 1.010"),
        "graph_neighbors": f.graph_neighbors("ors-2.020"),
        "authority_chain": f.authority_chain("ors-2.020"),
        "issuing_body_profile": f.issuing_body_profile("water-department"),
    }

    # EACH TOOL REACHED ITS REAL PATH, asserted rather than assumed. An earlier version of
    # this sweep ran against a fixture with an empty graph and no registry, so four of the
    # six tools returned early error envelopes and the sweep proved nothing about the code
    # that consumes a backend mapping — it would have gone green over a future site
    # splatting `holdings_for()`. A sweep that cannot fail is worse than no sweep.
    assert "did_you_mean" in responses["get_document"]          # not-found branch proper
    assert responses["corpus_overview"]["documents_by_type"] == {"record": 1}
    assert responses["graph_neighbors"]["implements"], "graph tools hit the empty-graph error"
    assert responses["authority_chain"]["title"] == "Fees"
    assert responses["issuing_body_profile"]["in_repo"] == {"verbatim": 1}, (
        "issuing_body_profile never reached backend.holdings_for()")

    enveloped = {name: r for name, r in responses.items()
                 if isinstance(r, dict) and "corpus" in r}
    # If this trips, a tool stopped carrying the envelope — a convention 1 regression that
    # would otherwise make the assertions below vacuously pass.
    assert set(enveloped) == set(responses), (
        f"not carrying an envelope: {sorted(set(responses) - set(enveloped))}")

    for name, r in enveloped.items():
        assert r["corpus"] == "test-corpus", f"{name} let the backend rename the corpus"
        assert r["archetype"] == "document", f"{name} let the backend change the archetype"
        assert r["authoritative_source"] is None, f"{name} took the backend's source URL"


def test_a_backend_cannot_replace_the_disclaimer_on_corpus_overview(corpus):
    """The non-authoritative disclaimer is the whole platform's load-bearing claim, and
    `corpus_overview` is the tool response convention 4 names by name as carrying it.

    A first pass at corpus-toolkit#104 moved the envelope past the backend's mapping but
    left `disclaimer` and `jurisdiction` in front of it, so a proxy backend stamping its
    upstream's terms of use replaced the NON-AUTHORITATIVE warning on the tool clients call
    first — while `get_document`'s docstring goes on claiming "a new backend cannot forget
    the disclaimer"."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_EnvelopeClobberingBackend")

    o = f.corpus_overview()

    assert "NON-AUTHORITATIVE" in o["disclaimer"]
    assert o["jurisdiction"] == "oregon"


def test_re_asserting_the_envelope_displaces_three_keys_and_drops_nothing(corpus):
    """Envelope-last must not become payload-last. The fix moves `**self._envelope()` after
    the backend's mapping, and the failure mode of getting that wrong is silent deletion —
    which is exactly how the v1.24.0 TypedDict incident destroyed `get_document`'s body at
    serialization while the call still reported success (corpus-toolkit#61)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_EnvelopeClobberingBackend")

    d = f.get_document("no-such-doc")
    assert d["error"] == "no document with id 'no-such-doc'"   # the record's own text
    assert "did_you_mean" in d

    o = f.corpus_overview()
    assert o["documents_by_type"] == {"record": 1}             # the backend's own facts
    assert o["commit"] == ""


def test_a_records_source_url_beats_the_corpus_front_door(corpus):
    """corpus-toolkit#90. The fallback tested the assembled response's slot rather than the
    record's `source_url`, so a backend honouring the documented `get()` contract — which
    nowhere requires `authoritative_source` — had the corpus's front door stamped over a
    per-document URL it had supplied in the same payload.

    That is a WRONG answer rather than a missing one: the response tells an agent to verify
    at the front door while carrying the exact URL the record came from. It bites hardest on
    the `api` and `hybrid` archetypes, which are the ones that ship a `retrieval_module`."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _corpus_declaring_a_front_door(corpus, "backend_mod:_StubBackend")

    d = f.get_document("stub:1")

    assert d["source_url"] == "https://api.invalid/records/42"
    assert d["authoritative_source"] == "https://api.invalid/records/42"


@pytest.mark.parametrize("factory", ["_RecordWithNoSourceBackend",
                                     "_RecordOmittingSourceBackend"])
def test_a_record_offering_nothing_more_precise_falls_back_to_the_front_door(corpus, factory):
    """Step 3 of the precedence, for both spellings of "not supplied".

    Falling THROUGH source_url must not become falling PAST the front door: a record with
    nothing more precise to say still gets the corpus's entry point, which is what
    convention 1 promises for every object-shaped response."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _corpus_declaring_a_front_door(corpus, f"backend_mod:{factory}")

    d = f.get_document("stub:1")

    assert d["authoritative_source"] == "https://front.invalid/door"


@pytest.mark.parametrize("factory", ["_RecordWithNoSourceBackend",
                                     "_RecordOmittingSourceBackend"])
def test_a_corpus_with_no_front_door_still_emits_the_documented_null(corpus, factory):
    """The end of the precedence, where every step declines.

    `null` is a documented value — "this corpus declares no front door" — and an ABSENT key
    means "nobody answered the question". CONTEXT.md's rule that outranks the vocabulary is
    that those two never collapse, and `ResponseEnvelope` types the field
    required-and-nullable precisely so a default cannot manufacture the first answer for a
    tool that gave neither."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from corpus_toolkit.mcp.responses import ResponseEnvelope
    f = _corpus_declaring_a_front_door(corpus, f"backend_mod:{factory}", front_door=None)

    d = f.get_document("stub:1")

    assert "authoritative_source" in d          # present, not omitted
    assert d["authoritative_source"] is None
    assert ResponseEnvelope.model_validate(d).authoritative_source is None


def test_a_non_string_source_url_is_not_promoted_into_the_envelope(corpus):
    """Falling through `source_url` must not turn a harmless record key into a tool error.

    `authoritative_source` is declared `str | None`, and the protocol never types
    `source_url` at all — a proxy backend may reasonably put a list of mirrors there.
    Before the precedence change such a value was an undeclared extra key that rode along
    while the slot took the front door, and the call succeeded. Promoting it unchecked would
    make every `get_document` on that corpus a hard ValidationError: exactly the class the
    parent commit closed for the not-found branch and `corpus_overview`, reopened on the
    success branch by the fix for it.

    So step 2 DECLINES a non-string and falls through. The asymmetry with step 1 is
    deliberate — see the framework comment."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from corpus_toolkit.mcp.responses import ResponseEnvelope
    f = _corpus_declaring_a_front_door(
        corpus, "backend_mod:_RecordWithNonStringSourceUrlBackend")

    d = f.get_document("stub:1")

    assert d["authoritative_source"] == "https://front.invalid/door"
    assert d["source_url"] == ["https://a.invalid/42", "https://b.invalid/42"]  # rides along
    assert ResponseEnvelope.model_validate(d).authoritative_source == (
        "https://front.invalid/door")


def test_the_success_branch_still_lets_a_record_supply_its_own_source(corpus):
    """The one place a backend is SUPPOSED to win, pinned so this fix cannot over-reach.

    A document's own `source_url` is the more precise answer to "where is the official
    text" than the corpus's front door, so `get_document`'s success branch leaves
    `authoritative_source` overridable on purpose — and that is what FileBackend relies on
    for every call. Nothing here touches that branch; this asserts it rather than trusting
    the diff, because the next change in this area (corpus-toolkit#90) edits exactly this
    precedence and should find a guard already in place."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _corpus_declaring_a_front_door(corpus,
                                       "backend_mod:_RecordWithItsOwnSourceBackend")

    d = f.get_document("rec:42")

    # Step 1 of the precedence beats both step 2 (the record's own source_url) and step 3
    # (the corpus front door) — three distinct URLs, so this cannot pass by coincidence.
    assert d["authoritative_source"] == "https://official.invalid/canonical/42"
    assert d["source_url"] == "https://api.invalid/records/42"
    assert d["corpus"] == "test-corpus"        # ...but never over these two
    assert d["archetype"] == "document"


def test_incomplete_backend_fails_at_startup(corpus):
    """Better a TypeError when the server boots than an AttributeError on the first
    query — for a search tool the latter looks like an empty corpus."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    with pytest.raises(TypeError, match="RetrievalBackend"):
        _with_backend(corpus, "backend_mod:_BrokenBackend")


def test_holdings_is_a_capability_a_backend_can_lack(corpus):
    """`issuing_body_profile` asks the backend rather than reaching through an FTS
    connection, so the question a caller cares about is whether the backend can answer at
    all — which server.py checks before registering the tool (corpus-toolkit#75).

    THIS TEST USED TO ASSERT `not hasattr(f, "ensure_index")`, pinning a deletion that was
    itself the bug: ERF's Dockerfile bakes its index by calling exactly that method, so
    every ERF image build failed and the reconcile loop rebuilt it every ten minutes for
    hours (platform-deploy#28). A test can enshrine a breaking change as firmly as it can
    prevent one — this one made the regression look deliberate to anyone reading the suite.
    The assertion is gone; `ensure_index` is restored and covered below."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_StubBackend")

    assert not callable(getattr(f.backend, "holdings_for", None))


def test_ensure_index_is_reachable_from_the_framework(corpus):
    """The call ERF's Dockerfile makes, in the form it makes it.

    A search of `corpus_toolkit/` and `tests/` found no caller before #75 deleted this,
    which is why it read as dead code. The callers are in the corpus repos that pin this
    one, and nothing in this repo's CI sees them — so the guard has to live here."""
    f = fw(corpus)

    con = f.ensure_index()
    try:
        assert con.execute("SELECT COUNT(*) FROM docs").fetchone()[0] > 0
    finally:
        con.close()


def test_a_backend_without_an_index_says_so_rather_than_AttributeError(corpus):
    """An API-archetype corpus legitimately has no FTS index. It should be told which
    backend and why, not handed the bare AttributeError that broke ERF."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _with_backend(corpus, "backend_mod:_StubBackend")

    with pytest.raises(AttributeError, match="not file-backed"):
        f.ensure_index()


def test_file_backend_counts_holdings_through_the_seam(corpus):
    """The query issuing_body_profile used to run itself, now behind the interface.

    The answer carries its own denominator (corpus-toolkit#71): a slug with nothing held
    reports empty COUNTS, and separately how much of the corpus carries any issuing-body
    attribution at all. Without that, "nothing for this body" and "nothing I can see for
    any body" are the same response."""
    f = fw(corpus)

    empty = f.backend.holdings_for("nobody-has-this-slug")
    assert empty["counts"] == {}
    assert empty["coverage"]["documents"] > 0
    # This fixture declares no registry, so "does this slug name a body?" has no answer
    # here and the three buckets are OMITTED rather than guessed at zero.
    assert "in_registry" not in empty["coverage"]
    assert isinstance(f.backend.holdings_for("")["counts"], dict)


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
    # Against the module's own constant, not a literal: this asserts "the rebuild stamped
    # the current schema", and a literal makes every bump look like a test failure.
    from corpus_toolkit.mcp.backends import SCHEMA_VERSION

    assert con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0] == str(SCHEMA_VERSION)


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


# ---------- M4: source_data_file provenance + corpus-verify stamping ----------

def test_hash_only_with_source_data_file_verifies_the_artifact(corpus, tmp_path):
    import hashlib
    data = corpus / "data"
    data.mkdir()
    (data / "d.parquet").write_bytes(b"PARQUET-ISH BYTES")
    good = hashlib.sha256(b"PARQUET-ISH BYTES").hexdigest()
    doc = DOC.format(id="ds-1", title="Dataset doc", doc_type="statute",
                     citation="DS 1", sha=good, tag="ors",
                     glance="A dataset summary.", body="ignored")
    doc = doc.replace("content_mode: verbatim", "content_mode: summary\n"
                      "snapshot_policy: hash-only\n"
                      "source_data_file: data/d.parquet")
    # strip the Full text section: dataset docs are summaries
    doc = doc.split("## Full text")[0]
    (corpus / "statutes" / "ds-1.md").write_text(doc)
    subprocess.run(["git", "add", "-A"], cwd=corpus, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "ds"], cwd=corpus, check=True)
    from corpus_toolkit.validate import provenance as prov
    prov._CONFIG = load_config(str(corpus / "_meta" / "corpus.yml"))
    prov._SLICE_FN = lambda d, s, t: t
    rel, findings, checked, _, _ = prov.check_file(corpus / "statutes" / "ds-1.md")
    assert checked == 1 and findings == []
    # and the gate FIRES on corruption
    (data / "d.parquet").write_bytes(b"DIFFERENT BYTES")
    rel, findings, checked, _, _ = prov.check_file(corpus / "statutes" / "ds-1.md")
    assert any("mismatch" in f[1] for f in findings)


def test_corpus_verify_stamps_only_with_attestation(corpus):
    import subprocess as sp
    r = sp.run(["python3", "-m", "corpus_toolkit.verify", "--config",
                str(corpus / "_meta" / "corpus.yml"), "--doc", "ors-1.010"],
               capture_output=True, text=True)
    assert r.returncode != 0 and "--attest" in (r.stderr + r.stdout)
    r = sp.run(["python3", "-m", "corpus_toolkit.verify", "--config",
                str(corpus / "_meta" / "corpus.yml"), "--doc", "ors-1.010",
                "--by", "@tester", "--attest", "read against source"],
               capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (corpus / "statutes" / "ors-1.010.md").read_text()
    assert 'verified_by: "@tester"' in text


def test_big_doc_bytes_has_exactly_one_definition():
    """corpus-toolkit#52 — the constant was defined twice, 1,200 bytes apart.

    `framework.py` imported it from `backends` and then immediately reassigned it to
    50_000, shadowing the import, while the only code that branches on it (backends'
    `part == "auto"` check) used 50 * 1024. A document between the two was "big" to one
    module and not the other, decided by which module a caller imported from, and nothing
    errored. Corpora also re-derive this threshold to decide which documents need `### `
    anchors, so the two values could disagree across repo boundaries too.
    """
    from corpus_toolkit.mcp import backends, framework
    assert framework.BIG_DOC_BYTES is backends.BIG_DOC_BYTES
    assert backends.BIG_DOC_BYTES == 50 * 1024

    # The shadowing assignment must not come back. An import is fine; a rebind is the bug.
    src = Path(framework.__file__).read_text()
    assert not re.search(r"^BIG_DOC_BYTES\s*=", src, re.M), (
        "framework.py must not redefine BIG_DOC_BYTES — import it from backends")


@pytest.mark.parametrize("reserved", ["corpus", "archetype", "authoritative_source",
                                      "id", "title"])
def test_no_graph_relation_can_displace_a_response_key_either(corpus, reserved):
    """The sweep above, for the input it does NOT cover (corpus-toolkit#105).

    It drives every enveloped tool through a clobbering BACKEND, which is why the third
    site of this class went unnoticed: `graph_neighbors` writes one response key per graph
    relation name, and graph data reaches the response by a path no backend stub touches.

    So the guard is completed here rather than left implied by the other one's name. A
    relation named for a reserved key is refused when the graph is parsed, and refusing is
    the point: for a BACKEND mapping the framework quietly wins, because the backend had no
    business setting those keys; a graph relation is the corpus's own declared edge, and
    dropping it silently would be data loss."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    f = _sweep_corpus(corpus, "backend_mod:_StubBackend")
    (corpus / "_meta" / "graph.json").write_text(json.dumps({
        "nodes": [{"id": "ors-1.010", "title": "Definitions", "doc_type": "statute"},
                  {"id": "ors-2.020", "title": "Fees", "doc_type": "statute"}],
        "edges": [{"from": "ors-2.020", "to": "ors-1.010", "type": reserved}]}))
    f._graph_cache = None                       # re-read the graph we just rewrote

    out = f.graph_neighbors("ors-2.020")

    assert reserved in out["error"]
    assert "graph.json" in out["note"]
    assert out["corpus"] == "test-corpus"       # refusing still carries the envelope
    # ...and ONLY this tool refuses. Raising from the shared graph loader took down
    # corpus_overview and resolve_citation too, neither of which can have a key displaced
    # by a relation name.
    assert "error" not in f.corpus_overview()
