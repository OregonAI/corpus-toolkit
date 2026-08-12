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
