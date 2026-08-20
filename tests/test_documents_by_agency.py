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
