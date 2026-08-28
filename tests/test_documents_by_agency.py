"""A corpus answers "your documents for registry slug X" (corpus-toolkit#46).

The platform's flagship question — dollars in, outcomes out, per agency — spans budget,
KPM, audits and ERF. The M3 crosswalk work made it POSSIBLE and nothing made it AVAILABLE:
`corpus-gateway` is deployed and fronting all eight corpora, but `agency_profile(slug)` is
the one tool it could not build, because the crosswalks are deliberately per-consumer —
"the table lives in the consumer, correctness belongs to the registry" — so a gateway
assembling a profile would have to duplicate every corpus's crosswalk and re-centralise
what was deliberately distributed.

ASKING IS BETTER THAN DUPLICATING. Each corpus already resolves its own documents to
registry slugs (`config.registry_slug_for`, indexed into `docs.issuing_body_slug`), so it
can answer for itself and the gateway stays stateless.

NOT A CROSSWALK LOADER. Measured 2026-08-19: oregon-kpm materialises the crosswalk into
frontmatter as `agency_registry_slug` on 785 of 785 documents, oregon-audits on 223 of 242.
The mapping is applied at ingest, by the corpus that owns it. The toolkit reads a resolved
slug; it does not learn to read a crosswalk.
"""
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp.backends import FileBackend

REPORT = """---
schema_version: 1
id: {id}
title: "{title}"
doc_type: performance_report
citation: "{citation}"
authority_level: agency_report
issuing_body: "{body}"
{slug_line}source_url: "https://example.invalid/{id}"
source_format: html
retrieved: "2026-08-19"
source_sha256: "{sha}"
status: current
content_mode: full_text
last_verified: "2026-08-19"
verified_by: "@test"
tags: ["t"]
---

## At a glance

A performance report.

## Full text

{body_text}
"""

DOGAMI = "department-of-geology-and-mineral-industries"
DAS = "department-of-administrative-services"


def _report(i: int, slug: str | None, body="Department of Geology and Mineral Industries"):
    return REPORT.format(
        id=f"appr-{i}", title=f"Annual Performance Progress Report {i}",
        citation=f"APPR {i}", body=body, sha=str(i) * 64,
        slug_line=f'agency_registry_slug: "{slug}"\n' if slug else "",
        body_text=f"The text of report {i}.")


CONFIG = """
    schema_version: 1
    corpus:
      id: test-kpm
      name: Test KPM
      jurisdiction: oregon
      archetype: document
    content_roots:
      - path: "reports"
        doc_type: "performance_report"
    plugins:
      issuing_body_slug_field: "agency_registry_slug"
    """


def _corpus(tmp_path: Path, docs: dict[str, str], config_yaml=CONFIG) -> Path:
    for rel, text in docs.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent(config_yaml).strip() + "\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def _backend(root: Path) -> FileBackend:
    return FileBackend(load_config(root / "_meta" / "corpus.yml"))


def test_a_corpus_returns_its_documents_for_a_registry_slug(tmp_path):
    """THE CAPABILITY, at the backend seam. Two agencies in one corpus; asking for one
    returns that one's documents and not the other's."""
    root = _corpus(tmp_path, {
        "reports/a-1.md": _report(1, DOGAMI),
        "reports/a-2.md": _report(2, DOGAMI),
        "reports/b-1.md": _report(3, DAS, body="Department of Administrative Services"),
    })

    got = _backend(root).documents_for_slug(DOGAMI)

    assert [d["id"] for d in got["documents"]] == ["appr-1", "appr-2"]
    assert got["total"] == 2


def _fw(root: Path):
    from corpus_toolkit.mcp.framework import CorpusFramework
    return CorpusFramework(load_config(root / "_meta" / "corpus.yml"))


def test_zero_documents_with_unattributed_ones_present_is_not_none(tmp_path):
    """THE DISTINCTION THIS TOOL EXISTS TO PRESERVE, and the one a caller will get wrong.

    An empty list for a slug reads as "this agency has nothing here". That is only true if
    every document in the corpus was attributed to SOMETHING. Where some carry no slug at
    all, the agency may well have documents — they are simply counted for nobody, so the
    honest answer is "none found, and this corpus cannot see all of itself".

    Measured on the corpus this serves: oregon-audits attributes 223 of 242 documents; the
    other 19 are its crosswalk's `unmapped` DECISIONS and carry no slug. A gateway
    assembling `agency_profile` from a bare empty list would report an agency as having no
    audits when the corpus never claimed to know. "Could not check" is never "is not
    there" (CONTEXT.md)."""
    root = _corpus(tmp_path, {
        "reports/a-1.md": _report(1, DOGAMI),
        "reports/u-1.md": _report(2, None),          # the crosswalk looked and declined
    })

    got = _fw(root).documents_by_agency(DAS)

    assert got["documents"] == []
    assert got["attribution"]["complete"] is False, (
        "an empty answer from a corpus that cannot see all of itself read as complete")
    assert got["attribution"]["documents_with_no_issuing_body"] == 1
    # THE OTHER HALF OF corpus-toolkit#158: this corpus attributes ONE of its two documents
    # (DOGAMI), so it CAN answer -- `total: 0` for a slug it genuinely holds nothing for
    # (DAS) is a real finding and must stay one, distinguishable from a corpus that
    # attributes nothing at all reporting the same clean zero.
    assert got["attribution"]["can_answer"] is True, (
        "a corpus that attributes at least one document was reported unable to answer")
    assert got["total"] == 0, (
        "a genuine none-found answer from an answerable corpus was withheld as null")


# ---------- corpus-toolkit#158: a corpus that attributes nothing says so ----------

def test_a_corpus_that_attributes_nothing_reports_it_cannot_answer(tmp_path):
    """THE RED PROOF for corpus-toolkit#158. A corpus where every document carries no
    issuing-body slug at all answered `total: 0` for any agency asked -- indistinguishable
    from a corpus that attributes normally and genuinely holds nothing for that agency.
    Measured on the platform (ADR-0011): oregon-budget (1,762 documents),
    oregon-legislature (6,137) and federal-reference (43) all attribute zero documents to
    any issuing body and all three answered a clean, confident zero for every slug that
    exists.

    The discriminator is the corpus's own counts --
    `documents_with_no_issuing_body >= documents_in_corpus` -- never the `basis` prose,
    which two corpora can share while one can answer and the other cannot."""
    root = _corpus(tmp_path, {
        "reports/u-1.md": _report(1, None),
        "reports/u-2.md": _report(2, None),
    })

    got = _fw(root).documents_by_agency(DOGAMI)

    assert got["attribution"]["can_answer"] is False, (
        "a corpus attributing NO document to any issuing body did not say it cannot answer")
    assert got["total"] is None, (
        "a non-attributing corpus reported a numeric total instead of withholding it")


REGISTRY = {"entries": [{"slug": DOGAMI, "name": "Department of Geology and Mineral Industries"},
                        {"slug": DAS, "name": "Department of Administrative Services"}]}

WITH_REGISTRY = CONFIG.rstrip() + """
      issuing_body_registry: "_meta/registry.yml"
      issuing_body_registry_key: "entries"
"""


def _corpus_with_registry(tmp_path: Path, docs: dict[str, str]) -> Path:
    root = _corpus(tmp_path, docs, config_yaml=WITH_REGISTRY)
    (root / "_meta" / "registry.yml").write_text(json.dumps(REGISTRY))
    return root


def test_a_corpus_with_no_registry_says_the_slug_was_not_checked(tmp_path):
    """`slug_in_registry` FALSE and NULL are different answers. False means checked, and
    the registry does not contain it — a typo, or an agency that does not exist. Null means
    the question was never asked.

    oregon-kpm has its registry commented out and oregon-audits states it has none, so
    returning False there would tell a gateway its slug was wrong on both corpora it most
    needs to ask."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})

    got = _fw(root).documents_by_agency("not-a-real-agency")

    assert got["slug_in_registry"] is None
    assert got["documents"] == []


def test_a_corpus_with_a_registry_says_the_slug_names_nothing(tmp_path):
    """The other half. With a registry, "no documents" and "no such agency" are separable,
    and a caller chasing an empty result needs to know which it hit."""
    root = _corpus_with_registry(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})

    missing = _fw(root).documents_by_agency("not-a-real-agency")
    real_but_empty = _fw(root).documents_by_agency(DAS)

    assert missing["slug_in_registry"] is False
    assert real_but_empty["slug_in_registry"] is True
    assert missing["documents"] == real_but_empty["documents"] == []


def test_a_fully_attributed_corpus_reports_a_complete_answer(tmp_path):
    """The counterpart to the floor case — a measure that can only ever say "floor" or
    "unknown" tells a caller nothing. With a registry and every document attributed, the
    answer is the whole answer and says so."""
    root = _corpus_with_registry(tmp_path, {
        "reports/a-1.md": _report(1, DOGAMI),
        "reports/b-1.md": _report(2, DAS, body="Department of Administrative Services"),
    })

    got = _fw(root).documents_by_agency(DOGAMI)

    assert [d["id"] for d in got["documents"]] == ["appr-1"]
    assert got["attribution"]["complete"] is True
    assert got["attribution"]["can_answer"] is True
    assert got["total"] == 1


def test_every_response_carries_the_envelope(tmp_path):
    """Response convention 1, on every object-shaped response. `issuing_body_profile` was
    the surface's one violation and it went undocumented for two releases
    (corpus-toolkit#38); a new tool does not get to reintroduce it."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})

    got = _fw(root).documents_by_agency(DOGAMI)

    assert got["corpus"] == "test-kpm"
    assert got["archetype"] == "document"
    assert "authoritative_source" in got


def test_the_tool_is_registered_only_when_the_backend_can_answer(tmp_path):
    """Same gate as `issuing_body_profile`, minus the registry requirement: registering a
    tool that raises on every call is the configuration corpus-toolkit#38 found the release
    gate did not cover.

    NOT gated on a registry, deliberately. `issuing_body_profile` needs one because it
    reports registry identity; this reports "which of my documents carry this slug", which
    needs none — and the two corpora `agency_profile` must ask declare none."""
    from corpus_toolkit.mcp import server as server_mod
    from corpus_toolkit.mcp._sdk import tools_by_name

    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})
    config = load_config(root / "_meta" / "corpus.yml")

    assert "documents_by_agency" in tools_by_name(server_mod.build_server(config))

    # A REAL CUSTOM BACKEND through the documented seam (`plugins.retrieval_module`),
    # not a monkeypatch: the gate exists for exactly this configuration, and a backend
    # implementing only the required protocol is what a corpus writing its own looks like.
    other = _corpus(tmp_path / "other", {"reports/a-1.md": _report(1, DOGAMI)},
                    config_yaml=CONFIG.rstrip() + '\n      retrieval_module: '
                                                  '"minimal_backend:MinimalBackend"\n')
    (other / "minimal_backend.py").write_text(textwrap.dedent('''
        class MinimalBackend:
            """Implements REQUIRED_BACKEND_METHODS and nothing optional."""
            name = "minimal"
            def __init__(self, config, semantic=None): self.config = config
            def search(self, *a, **kw): return []
            def get(self, *a, **kw): return None
            def exists(self, *a, **kw): return False
            def overview(self, *a, **kw): return {}
            def health(self, *a, **kw): return {"reachable": True, "documents": 1}
        '''))

    names = tools_by_name(server_mod.build_server(
        load_config(other / "_meta" / "corpus.yml")))

    assert "documents_by_agency" not in names, (
        "a backend that cannot answer got a tool that raises on every call")


def test_paging_covers_every_document_exactly_once(tmp_path):
    """`total` IS THE MATCH COUNT, NOT THE PAGE SIZE, and the order is a contract.

    Both were documented and neither was pinned — mutation-checked, and `ORDER BY id`
    dropped or `total` set to `len(rows)` passed the whole suite.

    They fail together in the way that hurts: a caller paginating with `offset` against an
    unordered scan sees some documents twice and never sees others, and a `total` equal to
    the page size says the last page is the whole answer. A gateway assembling
    `agency_profile` would report a partial document set as an agency's complete holdings —
    the same "a partial answer read as complete" this tool's attribution block exists to
    prevent, arriving through the pagination instead."""
    # IDS DELIBERATELY OPPOSITE TO FILENAME ORDER. Documents are indexed in path order, so
    # a fixture whose ids happen to sort the same way cannot tell `ORDER BY id` from
    # SQLite's natural rowid order — the first version of this test could not, and dropping
    # the clause passed the whole suite.
    root = _corpus(tmp_path, {f"reports/f{i}.md": _report(8 - i, DOGAMI)
                              for i in range(1, 8)})
    backend = _backend(root)

    first = backend.documents_for_slug(DOGAMI, limit=3, offset=0)
    second = backend.documents_for_slug(DOGAMI, limit=3, offset=3)
    third = backend.documents_for_slug(DOGAMI, limit=3, offset=6)

    assert first["total"] == 7, "total reported the page size, not the number of matches"
    assert [d["id"] for d in first["documents"]] == ["appr-1", "appr-2", "appr-3"], (
        "documents came back in storage order, so paging by offset is not stable")
    seen = [d["id"] for page in (first, second, third) for d in page["documents"]]
    assert len(seen) == len(set(seen)) == 7, f"paging skipped or repeated documents: {seen}"


def test_the_response_says_how_much_of_the_total_it_returned(tmp_path):
    """A page that does not say it is a page is a complete answer as far as a caller can
    tell. `returned` beside `total` is what makes the difference visible without the caller
    having to count the list itself."""
    root = _corpus(tmp_path, {f"reports/f{i}.md": _report(8 - i, DOGAMI)
                              for i in range(1, 8)})

    got = _fw(root).documents_by_agency(DOGAMI, limit=3)

    assert (got["total"], got["returned"], got["limit"], got["offset"]) == (7, 3, 3, 0)


# ---------- found in review: the registry-less coverage path ----------

class _Partial:
    """A backend that measured SOME of the coverage question and says so honestly."""
    name = "partial"
    def __init__(self, config, semantic=None): self.config = config
    def search(self, *a, **kw): return []
    def get(self, *a, **kw): return None
    def exists(self, *a, **kw): return False
    def overview(self, *a, **kw): return {}
    def health(self, *a, **kw): return {"reachable": True, "documents": 10}
    def holdings_for(self, slug, **kw):
        return {"counts": {"full_text": 1},
                "coverage": {"documents": 10, "unattributed": 2, "basis": "our own join"}}


def test_a_partial_measurement_is_not_promoted_to_a_measurement(tmp_path):
    """THE BRANCH KEYED ON SHAPE, NOT ON CONFIG, and that is the whole defect.

    It asked "did the backend report documents+unattributed and NOT the registry pair" and
    never consulted the corpus. So a backend that measured 2 of the 4 buckets for a corpus
    that DOES declare a registry was served as a complete measurement, the diagnostic naming
    what it failed to report disappeared, and the note asserted a fact about the corpus's
    configuration that the branch never checked — and that was false.

    On main that backend correctly got `complete: null` plus "reported attribution coverage
    without in_registry, no_registry_entry". A half-measurement is not a measurement
    (CONTEXT.md), and this promoted one."""
    root = _corpus_with_registry(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})
    fw = _fw(root)
    fw.backend = _Partial(fw.config)

    _counts, attribution = fw._holdings(fw.backend.holdings_for(DOGAMI))

    assert attribution["complete"] is None, (
        "a backend that reported 2 of 4 buckets was served as a measurement")
    assert "in_registry" in attribution["note"], (
        "the diagnostic naming what the backend did not report was lost")
    assert "declares no issuing-body registry" not in attribution["note"], (
        "the note asserted a config fact the branch never checked, and it was false")
    # corpus-toolkit#158: `can_answer` needs `documents_in_corpus` and
    # `documents_with_no_issuing_body`, neither of which this half-measurement reports —
    # `unknown`, not `False`, or a half-measurement would be read as "attributes nothing".
    assert attribution["can_answer"] == "unknown"


class _NoCoverage:
    """A backend on the pre-coverage shape: `holdings_for` returns bare counts, no
    `coverage` key at all -- v1.25.0's shape, still accepted (corpus-toolkit#158)."""
    name = "no-coverage"
    def __init__(self, config, semantic=None): self.config = config
    def search(self, *a, **kw): return []
    def get(self, *a, **kw): return None
    def exists(self, *a, **kw): return False
    def overview(self, *a, **kw): return {}
    def health(self, *a, **kw): return {"reachable": True, "documents": 10}
    def holdings_for(self, slug, **kw):
        return {"full_text": 1}


def test_a_backend_with_no_coverage_at_all_is_unknown_not_false(tmp_path):
    """The OTHER absence `can_answer` must not fold into `False`: a backend that reports no
    `coverage` block whatsoever -- not even a partial one -- has not measured
    `documents_with_no_issuing_body` against `documents_in_corpus`, so there is nothing for
    the discriminator to read. `unknown`, per the acceptance criteria: coverage counts
    absent OR unreadable both land here, never on `False`."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})
    fw = _fw(root)
    fw.backend = _NoCoverage(fw.config)

    _counts, attribution = fw._holdings(fw.backend.holdings_for(DOGAMI))

    assert attribution["complete"] is None
    assert attribution["can_answer"] == "unknown"
    assert "documents_in_corpus" not in attribution


def test_a_declared_but_unreadable_registry_is_not_reported_as_no_registry(tmp_path):
    """`config.issuing_body_slugs` returns None for BOTH "none declared" and "declared, and
    the file is missing or will not parse". Collapsing those tells an operator their corpus
    is configured the way they meant when in fact its registry path is broken — the last
    signal that anything is wrong, absorbed into a positive statement about intent.

    Could not read is not declares none (CONTEXT.md)."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=WITH_REGISTRY)          # registry.yml deliberately absent

    got = _fw(root).documents_by_agency(DOGAMI)

    note = got["attribution"].get("note", "")
    assert "declares no issuing-body registry" not in note, note
    assert "could not" in note.lower() or "unreadable" in note.lower(), note


def test_an_empty_index_is_not_a_fully_attributed_corpus(tmp_path):
    """The four-bucket path says so explicitly; the new branch dropped it and claimed
    "every document carries a slug" about a corpus with no documents.

    On exactly the configuration this branch was written for — oregon-kpm and oregon-audits
    declare no registry — and exactly the configuration platform-deploy's MIN_DOCS abort
    exists because it otherwise serves green."""
    root = _corpus(tmp_path, {})

    got = _fw(root).documents_by_agency(DOGAMI)

    assert got["attribution"]["complete"] is None
    assert "no documents" in got["attribution"]["note"].lower(), got["attribution"]["note"]


def test_complete_true_is_impossible_without_a_registry(tmp_path):
    """THE BRANCH'S OWN ARGUMENT, AND THE STRICT PATH BROKE IT.

    Gating the registry-less branch on config was not enough: a backend reporting the
    four-bucket shape still reached the STRICT path, which trusts `in_registry` without
    checking that a registry exists — so a corpus with none served `complete: true` in the
    same response as `slug_in_registry: null`. Internally contradictory: "every document is
    matched to a registry entry" and "there is no registry to ask" cannot both hold.

    Newly reachable here, because `documents_by_agency` is deliberately not registry-gated
    while `issuing_body_profile` is; on main no tool reached `_holdings` without a registry.
    A backend claiming registry buckets against a config that declares no registry is a
    DISAGREEMENT, and the honest answer names it."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=CONFIG.rstrip()
                   + '\n      retrieval_module: "four_bucket:FourBucket"\n')
    (root / "four_bucket.py").write_text(textwrap.dedent('''
        class FourBucket:
            """Reports the four-bucket shape for a corpus that declares no registry."""
            name = "four-bucket"
            def __init__(self, config, semantic=None): self.config = config
            def search(self, *a, **kw): return []
            def get(self, *a, **kw): return None
            def exists(self, *a, **kw): return False
            def overview(self, *a, **kw): return {}
            def health(self, *a, **kw): return {"reachable": True, "documents": 10}
            def holdings_for(self, slug, **kw):
                return {"counts": {}, "coverage": {
                    "documents": 10, "in_registry": 10, "no_registry_entry": 0,
                    "unattributed": 0, "basis": "hardcoded"}}
            def documents_for_slug(self, slug, limit=50, offset=0):
                return {"documents": [], "total": 0,
                        "coverage": self.holdings_for(slug)["coverage"]}
        '''))

    got = _fw(root).documents_by_agency(DOGAMI)

    assert got["slug_in_registry"] is None
    assert got["attribution"]["complete"] is not True, (
        "a corpus with no registry reported every document matched to a registry entry")
    assert "registry" in got["attribution"]["note"].lower()


def test_a_declared_sentinel_is_not_served_as_an_agency(tmp_path):
    """A SENTINEL IS THE CORPUS SAYING "THIS BELONGS TO NO BODY" (corpus-toolkit#94), and
    asking for it by name got those documents back as that body's holdings.

    Worse, with `complete: true` — which the contract's own table reads as "the whole
    answer" — and `slug_in_registry: false`, whose comment defines it as "a typo, or an
    agency that does not exist". Neither is the condition that occurred.

    On ERF that is 37,991 documents servable under `statewide` as an agency's complete
    holdings: the conflation #94 closed, arriving through a new tool."""
    root = _corpus(tmp_path, {
        "reports/a-1.md": _report(1, DOGAMI),
        "reports/s-1.md": _report(2, "statewide"),
        "reports/s-2.md": _report(3, "statewide"),
    }, config_yaml=WITH_REGISTRY.rstrip()
       + '\n      issuing_body_slug_sentinels: ["statewide"]\n')
    (root / "_meta" / "registry.yml").write_text(json.dumps(REGISTRY))

    got = _fw(root).documents_by_agency("statewide")

    assert got["documents"] == [], "documents attributed to NO body were served as an agency's"
    assert "sentinel" in got["error"].lower(), got
    # NO `attribution` BLOCK. The four-answer table describes answers; this is not one, and
    # attaching a completeness claim to a refusal invites reading the refusal as an answer.
    # `can_answer` lives inside `attribution` (corpus-toolkit#158), so this one assertion
    # already covers it -- stated explicitly because the acceptance criterion is its own:
    # `can_answer` must never appear on a refusal.
    assert "attribution" not in got
    assert got["total"] == 0, "a refusal's total is a page-size zero, never a withheld null"


def test_an_empty_slug_does_not_return_the_unattributed_documents(tmp_path):
    """Unattributed documents are indexed with `issuing_body_slug = ''`, so an empty slug
    matched them — and the SAME response's coverage counted them under
    `documents_with_no_issuing_body`. One response, two contradictory claims about one
    document."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI),
                              "reports/u-1.md": _report(2, None)})

    got = _fw(root).documents_by_agency("")

    assert got["documents"] == []
    assert "error" in got or got["total"] == 0


@pytest.mark.parametrize("limit,offset", [(-1, 0), (-5, 2), (10**9, 0), (0, 0), (5, -3)])
def test_pagination_arguments_are_clamped_and_echoed_honestly(limit, offset, tmp_path):
    """SQLite reads a negative LIMIT as unbounded, so `limit=-1` returned EVERY match in one
    response — on ERF's Department of Environmental Quality that is 1,929 documents — while
    the response echoed `limit: -1`. `offset=-3` was silently taken as 0 while the response
    echoed -3, so a caller paging backwards silently re-read page 1. And `limit=0` returned
    an empty list with `complete: true`, which the contract's table reads as "this corpus
    genuinely holds nothing for that slug".

    Every sibling in this file clamps — `search` to `max(1, min(int(limit), 40))`. The
    echoed values must be the ones actually served, or the response is lying about the page
    it returned."""
    root = _corpus(tmp_path, {f"reports/f{i}.md": _report(i, DOGAMI) for i in range(1, 6)})

    got = _fw(root).documents_by_agency(DOGAMI, limit=limit, offset=offset)

    assert got["limit"] >= 1 and got["offset"] >= 0, (limit, offset, got["limit"], got["offset"])
    assert got["returned"] == len(got["documents"]) <= got["limit"]
    assert got["returned"] >= 1, "a clamped page must still serve documents that exist"


def test_a_backend_returning_the_wrong_shape_says_so_rather_than_raising_keyerror(tmp_path):
    """The capability gate asks "can you answer?" by checking the method EXISTS. A backend
    implementing it with a slightly different shape passes that gate and then raised
    `KeyError: 'documents'` on every call — the registered-landmine outcome the gate was
    added for (corpus-toolkit#38), rebuilt one layer in.

    `_holdings` is defensive about the backend's coverage throughout; this consumed
    `raw["documents"]` and `raw["total"]` directly."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=CONFIG.rstrip() +
                   '\n      retrieval_module: "bad_shape:BadShape"\n')
    (root / "bad_shape.py").write_text(textwrap.dedent('''
        class BadShape:
            """Implements the method, returns a plausible-but-wrong shape."""
            name = "bad-shape"
            def __init__(self, config, semantic=None): self.config = config
            def search(self, *a, **kw): return []
            def get(self, *a, **kw): return None
            def exists(self, *a, **kw): return False
            def overview(self, *a, **kw): return {}
            def health(self, *a, **kw): return {"reachable": True, "documents": 1}
            def documents_for_slug(self, slug, limit=50, offset=0):
                return {"results": [], "count": 0}      # not the declared shape
        '''))

    with pytest.raises(Exception) as e:
        _fw(root).documents_by_agency(DOGAMI)

    assert "bad-shape" in str(e.value), "the error did not name the backend at fault"
    assert "documents" in str(e.value)


def test_a_fully_slugged_corpus_with_no_registry_is_unknown_not_complete(tmp_path):
    """THE BRANCH'S CENTRAL CLAIM, and no test could see it fail: mutating
    `False if unattributed else None` to `... else True` passed the whole suite.

    Every document carries a slug, so nothing is invisible to a per-slug query — which is
    exactly the reasoning that makes `true` tempting and wrong. Without a registry a
    MISTYPED slug is undetectable, and a document filed under a typo is precisely a document
    this answer cannot see. Unknown, not yes."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI),
                              "reports/b-1.md": _report(2, DAS)})

    got = _fw(root).documents_by_agency(DOGAMI)

    assert [d["id"] for d in got["documents"]] == ["appr-1"]
    assert got["attribution"]["complete"] is None, (
        "a registry-less corpus claimed its answer was complete")
    assert got["attribution"]["documents_in_corpus"] == 2
    assert "mistyped" in got["attribution"]["note"]


SENTINEL_NO_REGISTRY = CONFIG.rstrip() + """
      issuing_body_slug_sentinels: ["statewide"]
"""


def test_sentinels_are_counted_without_a_registry(tmp_path):
    """The one configuration this branch added over main, and nothing exercised it: every
    other fixture declares no sentinels, so `declared = sum(...)` could be replaced with
    `declared = 0` and the suite stayed green.

    A declared sentinel is RESOLVED, not a gap (corpus-toolkit#94) — counting it as
    unattributed would make a corpus that has done everything right report its answers as
    floors forever, which is the error #94 exists to undo."""
    root = _corpus(tmp_path, {
        "reports/a-1.md": _report(1, DOGAMI),
        "reports/s-1.md": _report(2, "statewide"),
        "reports/u-1.md": _report(3, None),
    }, config_yaml=SENTINEL_NO_REGISTRY)

    got = _fw(root).documents_by_agency(DOGAMI)
    att = got["attribution"]

    assert att["documents_in_corpus"] == 3
    assert att["documents_declared_no_issuing_body"] == 1, (
        "a declared sentinel was not counted as resolved")
    assert att["documents_with_no_issuing_body"] == 1
    assert att["complete"] is False


class _SentinelBlind:
    """Declares sentinels, but its backend never reports `declared_no_body`."""
    name = "sentinel-blind"
    def __init__(self, config, semantic=None): self.config = config
    def search(self, *a, **kw): return []
    def get(self, *a, **kw): return None
    def exists(self, *a, **kw): return False
    def overview(self, *a, **kw): return {}
    def health(self, *a, **kw): return {"reachable": True, "documents": 3}
    def holdings_for(self, slug, **kw):
        return {"counts": {}, "coverage": {"documents": 3, "unattributed": 1,
                                           "basis": "partial"}}


def test_a_sentinel_corpus_whose_backend_omits_the_bucket_is_unknown(tmp_path):
    """Where sentinels ARE declared, `declared_no_body` is required and its absence is
    decisive: such a backend counted every sentinel document as something else, so its split
    is not merely incomplete but WRONG (corpus-toolkit#94). The sub-expression enforcing
    that in the new predicate was unobserved — deleting it left the suite green."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=SENTINEL_NO_REGISTRY)
    fw = _fw(root)
    fw.backend = _SentinelBlind(fw.config)

    _counts, att = fw._holdings(fw.backend.holdings_for(DOGAMI))

    assert att["complete"] is None
    assert "declared_no_body" in att["note"]
    assert att["can_answer"] == "unknown", (
        "a sentinel corpus whose backend omitted a required bucket was not marked unknown")


def test_the_registered_tool_passes_limit_and_offset_through_in_that_order(tmp_path):
    """No test crossed the MCP tool boundary with pagination arguments, so swapping the two
    in `server.py` — `limit=offset, offset=limit` — passed the whole suite. The framework
    method was covered; the wiring to it was not."""
    from corpus_toolkit.mcp import server as server_mod
    from corpus_toolkit.mcp._sdk import call_tool, tools_by_name
    import asyncio

    root = _corpus(tmp_path, {f"reports/f{i}.md": _report(8 - i, DOGAMI)
                              for i in range(1, 8)})
    mcp = server_mod.build_server(load_config(root / "_meta" / "corpus.yml"))
    assert "documents_by_agency" in tools_by_name(mcp)

    got = asyncio.run(call_tool(mcp, "documents_by_agency",
                                {"slug": DOGAMI, "limit": 2, "offset": 4}))

    assert (got["limit"], got["offset"], got["returned"]) == (2, 4, 2)
    assert [d["id"] for d in got["documents"]] == ["appr-5", "appr-6"]


def test_documents_and_their_coverage_come_from_one_index_read(tmp_path):
    """"DELEGATED RATHER THAN RE-DERIVED" WAS ONLY HALF TRUE. `documents_for_slug` opened
    the index, read its documents, then called `holdings_for`, which calls `ensure_index()`
    AGAIN — and that returns a fresh `sqlite3.connect` after re-checking `repo_state`, not a
    cached handle. So a rebuild landing between the two reads attaches coverage measured
    against one corpus to documents listed from another.

    `ensure_index`'s own docstring contemplates exactly that race ("a long-lived MCP server
    racing a CLI rebuild"). The response claims both halves are the same claim about the
    same corpus; they have to be read from the same connection for that to hold."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI),
                              "reports/u-1.md": _report(2, None)})
    backend = _backend(root)
    backend.ensure_index()                      # build once, outside the count

    calls = []
    real = backend.ensure_index
    backend.ensure_index = lambda *a, **kw: (calls.append(1), real(*a, **kw))[1]

    got = backend.documents_for_slug(DOGAMI)

    assert got["coverage"]["unattributed"] == 1
    assert len(calls) == 1, (
        f"documents and coverage were read through {len(calls)} separate index "
        f"connections, so a rebuild between them goes unnoticed")


# ---------- found in the third review ----------

def test_a_sentinel_refusal_does_not_claim_the_slug_was_checked(tmp_path):
    """The refusal hardcoded `slug_in_registry: False` — contradicting the comment three
    lines above it, this method's own docstring, the contract and the CHANGELOG, all of
    which say null-never-false where nothing was checked.

    And it fires on exactly the corpora this tool targets: oregon-kpm and oregon-audits
    declare no registry, so a gateway asking about a sentinel there was told "checked, and
    the registry does not contain it" by a corpus with nothing to check against."""
    root = _corpus(tmp_path, {"reports/s-1.md": _report(1, "statewide")},
                   config_yaml=SENTINEL_NO_REGISTRY)

    got = _fw(root).documents_by_agency("statewide")

    assert "sentinel" in got["error"].lower()
    assert got["slug_in_registry"] is None, (
        "a corpus with no registry reported the slug as checked and absent")


def test_a_padded_slug_is_not_reported_as_a_slug_the_registry_lacks(tmp_path):
    """The empty-slug guard strips for its emptiness test and nothing strips for the lookup,
    so `"  dogami  "` reached the backend verbatim: zero documents, and
    `slug_in_registry: False` — "a typo, or an agency that does not exist" — about a slug
    the registry does contain."""
    root = _corpus_with_registry(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)})

    got = _fw(root).documents_by_agency(f"  {DOGAMI}  ")

    assert got["slug_in_registry"] is True
    assert [d["id"] for d in got["documents"]] == ["appr-1"]


def test_a_page_is_capped_however_large_a_limit_is_asked_for(tmp_path):
    """The cap was asserted NOWHERE: deleting `min(..., _MAX_DOCUMENTS_PER_PAGE)` — the only
    reason that constant exists — passed the whole suite, because the largest fixture held 5
    documents and `10**9 >= 5`. The regression it guards is 1,929 ERF documents in one MCP
    response, and it would have shipped green."""
    from corpus_toolkit.mcp.framework import _MAX_DOCUMENTS_PER_PAGE

    n = _MAX_DOCUMENTS_PER_PAGE + 5
    root = _corpus(tmp_path, {f"reports/f{i:04d}.md": _report(i, DOGAMI)
                              for i in range(1, n + 1)})

    got = _fw(root).documents_by_agency(DOGAMI, limit=10**9)

    assert got["total"] == n
    assert got["limit"] == _MAX_DOCUMENTS_PER_PAGE
    assert got["returned"] == _MAX_DOCUMENTS_PER_PAGE, "the page cap did not apply"


def test_a_backend_config_disagreement_is_reported_as_itself(tmp_path):
    """The sub-clause routing a four-bucket backend on a registry-less corpus to the
    DISAGREEMENT path could be deleted and the suite stayed green: the previous test asserted
    only `complete is not True` and `"registry" in note`, which the registry-less path's own
    note also satisfies. Two paths, one indistinguishable assertion.

    They are different findings. The registry-less path says "this corpus has no registry, so
    the slug was not checked"; this says "your backend counted against a registry this corpus
    does not have" — a fault to fix, not a property to report."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=CONFIG.rstrip()
                   + '\n      retrieval_module: "four_bucket:FourBucket"\n')
    (root / "four_bucket.py").write_text(textwrap.dedent('''
        class FourBucket:
            name = "four-bucket"
            def __init__(self, config, semantic=None): self.config = config
            def search(self, *a, **kw): return []
            def get(self, *a, **kw): return None
            def exists(self, *a, **kw): return False
            def overview(self, *a, **kw): return {}
            def health(self, *a, **kw): return {"reachable": True, "documents": 10}
            def holdings_for(self, slug, **kw):
                return {"counts": {}, "coverage": {
                    "documents": 10, "in_registry": 10, "no_registry_entry": 0,
                    "unattributed": 0, "basis": "hardcoded"}}
            def documents_for_slug(self, slug, limit=50, offset=0):
                return {"documents": [], "total": 0,
                        "coverage": self.holdings_for(slug)["coverage"]}
        '''))

    got = _fw(root).documents_by_agency(DOGAMI)
    att = got["attribution"]

    assert att["complete"] is None
    assert "disagree" in att["note"], f"reported as a property, not as a fault: {att}"
    assert "documents_in_corpus" not in att, (
        "a disagreement was answered with counts measured against nothing known")
    assert att["can_answer"] == "unknown", (
        "corpus-toolkit#158: a fault reported as `False` would say this corpus attributes "
        "nothing, a claim this disagreement has no counts to support")
    assert got["total"] == 0, (
        "`unknown` must not null `total` the way `False` does — only a corpus KNOWN to "
        "attribute nothing withholds it")


def test_a_backend_returning_the_right_keys_with_wrong_types_names_the_backend(tmp_path):
    """The shape check tested key PRESENCE only, so `{"documents": 5, "total": 0}` passed it
    and died on `len(5)` — a TypeError naming no backend, which is the unattributable failure
    the check exists to replace."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=CONFIG.rstrip() +
                   '\n      retrieval_module: "wrong_types:WrongTypes"\n')
    (root / "wrong_types.py").write_text(textwrap.dedent('''
        class WrongTypes:
            name = "wrong-types"
            def __init__(self, config, semantic=None): self.config = config
            def search(self, *a, **kw): return []
            def get(self, *a, **kw): return None
            def exists(self, *a, **kw): return False
            def overview(self, *a, **kw): return {}
            def health(self, *a, **kw): return {"reachable": True, "documents": 1}
            def documents_for_slug(self, slug, limit=50, offset=0):
                return {"documents": 5, "total": "many"}
        '''))

    with pytest.raises(Exception) as e:
        _fw(root).documents_by_agency(DOGAMI)

    assert "wrong-types" in str(e.value)
    assert "documents" in str(e.value) and "total" in str(e.value)


def test_a_subclass_overriding_holdings_for_still_works(tmp_path):
    """`holdings_for` gained a `con` parameter, and every backend already in service was
    written against the signature without it. Such a backend inherits `documents_for_slug`,
    which passes `con=`, and died on every call with `unexpected keyword argument 'con'`.

    Declaring `con` on the protocol fixes the next author; it does nothing for the ones
    already deployed. Sharing the connection is an optimisation and a race fix — it is not
    worth breaking a live corpus over, so the caller degrades to a second read rather than
    demanding the parameter. Every fake backend in this file writes `**kw`, which hid this
    entirely."""
    root = _corpus(tmp_path, {"reports/a-1.md": _report(1, DOGAMI)},
                   config_yaml=CONFIG.rstrip() +
                   '\n      retrieval_module: "sub_backend:SubBackend"\n')
    (root / "sub_backend.py").write_text(textwrap.dedent('''
        from corpus_toolkit.mcp.backends import FileBackend

        class SubBackend(FileBackend):
            """The signature the protocol declared BEFORE `con` existed — which is what any
            custom backend already in service was written against."""
            name = "sub"
            def holdings_for(self, slug):
                return super().holdings_for(slug)
        '''))

    got = _fw(root).documents_by_agency(DOGAMI)

    assert [d["id"] for d in got["documents"]] == ["appr-1"]


def test_an_empty_corpus_is_unknown_not_a_corpus_that_attributes_nothing(tmp_path):
    """`unattributed >= in_corpus` is true of an empty index (0 >= 0), and reading that as
    `False` states a measurement nobody took.

    `complete` already answers an empty index with `None`, and the contract already calls
    that "unknown, not none". Two fields in the same block reading identical evidence
    opposite ways -- one declining to measure, the other asserting a definite negative --
    is the collapse ADR-0011 closes, reappearing one field over. It also withheld `total`
    on a corpus that has nothing to withhold.
    """
    root = _corpus(tmp_path, {})

    got = _fw(root).documents_by_agency(DOGAMI)

    assert got["attribution"]["can_answer"] == "unknown"
    assert got["attribution"]["complete"] is None, (
        "the two fields must read the same evidence the same way")
    assert got["total"] == 0, "an empty corpus withholds nothing; it holds nothing"


def test_a_slug_absent_from_the_registry_still_reports_it_cannot_answer(tmp_path):
    """Acceptance criterion: `can_answer: false` and `total: null` hold "for every slug
    including ones absent from the registry". A slug nobody has heard of takes a different
    path through `slug_in_registry`, and the criterion had no test."""
    root = _corpus(tmp_path, {"reports/u-1.md": _report(1, None)})

    got = _fw(root).documents_by_agency("no-such-agency-anywhere")

    assert got["attribution"]["can_answer"] is False
    assert got["total"] is None
