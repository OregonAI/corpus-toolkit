"""Relationship resolution must be corpus-wide even when validation is scoped.

`--changed` deliberately validates only the files in a diff. The bug this guards against
is scoping the *resolution universe* the same way: a relationship pointing at an unchanged
sibling then looks unresolvable, and a one-file PR fails on references that are perfectly
valid.

It only bites a corpus with no `graph.json` — a legitimate state, and the documented one
for a corpus that has not built a graph yet. Corpora that have a graph got a corpus-wide
universe from it and never saw this, which is exactly why it stayed hidden until the first
graph-less corpus edited a relationship-bearing file in a PR.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

from corpus_toolkit import config as config_mod
from corpus_toolkit.validate.frontmatter import _resolution_universe, _all_content_ids

CORPUS_YML = """\
corpus:
  id: "t"
  name: "T"
  jurisdiction: "oregon"
  archetype: "document"
  schema_version: 1
  contract_version: 1
content_roots:
  - path: "docs"
    doc_type: doc
graph_path: "_meta/graph.json"
disclaimer_marker: "NON-AUTHORITATIVE"
"""

DOC = """\
---
schema_version: 1
corpus: t
id: {id}
title: {id}
doc_type: doc
relationships:
{rels}
---

NON-AUTHORITATIVE test document.
"""


def _doc(id_, rels=()):
    body = ("  related:\n" + "\n".join(f"    - {r}" for r in rels)) if rels else "  related: []"
    return DOC.format(id=id_, rels=body)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta/corpus.yml").write_text(CORPUS_YML)
    (tmp_path / "docs").mkdir()
    # alpha points at beta; beta and gamma are untouched by the "diff"
    (tmp_path / "docs/alpha.md").write_text(_doc("alpha", ["beta", "gamma"]))
    (tmp_path / "docs/beta.md").write_text(_doc("beta"))
    (tmp_path / "docs/gamma.md").write_text(_doc("gamma"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_universe_is_corpus_wide_when_only_one_file_changed(corpus):
    """The regression. `docs` carries only the changed file; beta and gamma must still
    resolve, because they exist in the corpus."""
    config = config_mod.load(str(corpus / "_meta/corpus.yml"))
    assert not config.graph_path.is_file(), "fixture must have no graph — that is the case under test"

    universe = _resolution_universe(config, {"alpha"})     # as if only alpha changed
    assert {"beta", "gamma"} <= universe, (
        "relationship targets in unchanged files fell out of the universe — a scoped "
        "validation run would report them as unresolvable")


def test_all_content_ids_finds_every_document(corpus):
    config = config_mod.load(str(corpus / "_meta/corpus.yml"))
    assert _all_content_ids(config) == {"alpha", "beta", "gamma"}


def test_graph_is_preferred_when_present(corpus):
    """The graph stays the fast path; the frontmatter scan is only a fallback."""
    (corpus / "_meta/graph.json").write_text('{"nodes":[{"id":"from-graph"}]}')
    config = config_mod.load(str(corpus / "_meta/corpus.yml"))
    universe = _resolution_universe(config, set())
    assert "from-graph" in universe
    assert "beta" not in universe, "graph present — should not have paid for a full scan"


def test_bundled_schema_is_importable_without_a_toolkit_checkout():
    """The schema must come from the INSTALLED package.

    It used to exist only at the repo root, reachable at `.toolkit/schemas/...` — a path
    created solely by the reusable workflows' second checkout. Every local validation
    command in every corpus's docs pointed there, so none of them could be run by an actual
    contributor, and `pip install corpus-toolkit` could not supply it either: packages.find
    included only `corpus_toolkit*`, so `schemas/` shipped in neither the wheel nor as
    package data.
    """
    from corpus_toolkit.validate.frontmatter import bundled_schema
    s = bundled_schema()
    assert s["properties"]["doc_type"]["enum"], s
    assert "id" in s["properties"]
