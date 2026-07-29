#!/usr/bin/env python3
"""Graph-backed MCP tools: external edge targets, the no-graph condition, and the
response-convention-1 envelope.

Every test here exists because the tool answered CONFIDENTLY AND WRONGLY, not because
it was slow or missing a field — so each one is written to fail loudly against the
pre-fix behaviour rather than to pin the current shape:

  * corpus-toolkit#4 — graph_neighbors did `nodes[t]` on every edge target. On
    oregon-records-retention (n_edges 440, n_edges_external 440) that raised KeyError
    for EVERY document, and the caller saw a tool error whose whole message was an OAR
    citation string. authority_chain had the identical unguarded lookup and is covered
    here too; the issue named only graph_neighbors.
  * corpus-toolkit#5 — with no _meta/graph.json, graph() degrades to ({}, {}) and both
    tools reported "no document with id X" for documents the same server was serving.
  * corpus-toolkit#6 — corpus_overview never carried authoritative_source.

Stdlib unittest, matching tests/test_cross_corpus.py.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit import config as config_mod                  # noqa: E402
from corpus_toolkit.mcp.framework import (                       # noqa: E402
    CorpusFramework, clear_schemes, register_scheme,
)

CORPUS_YML = """\
corpus:
  id: records-retention
  name: Records Retention
  jurisdiction: oregon
  archetype: document
  schema_version: 1
  contract_version: 1
content_roots:
  - path: schedules
    doc_type: schedule
"""

DOC = """\
---
id: {id}
title: {title}
doc_type: schedule
citation: Schedule {num}
status: current
source_url: https://sos.oregon.gov/{num}
retrieved: 2026-01-01
content_mode: summary
---

## At a glance

NON-AUTHORITATIVE. Records retention rules, implementing OAR 166-300-0015.
"""

# The real shape from oregon-records-retention/_meta/graph.json: `to` is an OAR
# CITATION STRING that resolves in executive-regulatory-frameworks, not a node here.
SIBLING_INDEX = {
    "corpus": "executive-regulatory-frameworks",
    "contract_version": 1,
    "n_documents": 1,
    "documents": {
        "oar-166-300-0015": ["OAR 166-300-0015 — General Schedule Rules",
                             "administrative-rule",
                             "rules/oar-166/oar-166-300-0015.md"],
    },
}


class GraphToolTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="corpus-graph-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        clear_schemes()
        self.addCleanup(clear_schemes)

    def corpus(self, *, edges=None, graph=True, extra_docs=(), graph_nodes=None,
               siblings="", authoritative_source=None) -> Path:
        root = self.tmp / "repo"
        (root / "_meta").mkdir(parents=True, exist_ok=True)
        (root / "schedules").mkdir(parents=True, exist_ok=True)
        docs = [("schedule-employment", "Employment Records Schedule", "166-300"),
                *extra_docs]
        for doc_id, title, num in docs:
            (root / "schedules" / f"{doc_id}.md").write_text(
                DOC.format(id=doc_id, title=title, num=num))

        text = CORPUS_YML
        if authoritative_source:
            text = text.replace("  schema_version: 1",
                                f"  authoritative_source: {authoritative_source}\n"
                                f"  schema_version: 1")
        text += siblings
        (root / "_meta" / "corpus.yml").write_text(text)

        if graph:
            nodes = graph_nodes if graph_nodes is not None else [
                {"id": d, "title": t, "doc_type": "schedule",
                 "path": f"schedules/{d}.md"} for d, t, _ in docs]
            (root / "_meta" / "graph.json").write_text(json.dumps(
                {"corpus": "records-retention", "nodes": nodes, "edges": edges or []}))
        return root / "_meta" / "corpus.yml"

    def framework(self, cfg: Path) -> CorpusFramework:
        return CorpusFramework(config_mod.load(cfg))

    def sibling_block(self, *, index_path=None, index_url=None) -> str:
        out = "siblings:\n  - id: executive-regulatory-frameworks\n"
        if index_path:
            out += f"    index_path: {index_path}\n"
        if index_url:
            out += f"    index_url: {index_url}\n"
        out += ("    web_base: https://github.com/OregonAI/"
                "executive-regulatory-frameworks/blob/main/\n")
        return out

    def write_sibling_index(self) -> Path:
        idx = self.tmp / "sibling-index.json"
        idx.write_text(json.dumps(SIBLING_INDEX))
        return idx


class TestExternalEdgeTargets(GraphToolTestCase):
    """corpus-toolkit#4."""

    EXTERNAL_EDGE = [{"from": "schedule-employment", "type": "related",
                      "to": "OAR 166-300-0015"}]

    def test_graph_neighbors_does_not_raise_on_an_external_target(self):
        cfg = self.corpus(edges=self.EXTERNAL_EDGE)
        # Call it OUTSIDE the assertion: pre-fix this raises KeyError, and an
        # uncaught raise here would abort the whole test run before any of the
        # assertions below print — a regression would then read as a crash, not a
        # failure. Turn it into a value first, then assert on it.
        try:
            out = self.framework(cfg).graph_neighbors("schedule-employment")
        except Exception as e:                                   # noqa: BLE001
            self.fail(f"graph_neighbors raised {type(e).__name__}: {e} — an edge target "
                      f"that is not a local node must be reported, not raised")
        self.assertNotIn("error", out)
        self.assertEqual(out["related"], [{"citation": "OAR 166-300-0015",
                                           "external": True}])

    def test_authority_chain_does_not_raise_on_an_external_target(self):
        cfg = self.corpus(edges=[{"from": "schedule-employment", "type": "implements",
                                  "to": "OAR 166-300-0015"}])
        try:
            out = self.framework(cfg).authority_chain("schedule-employment")
        except Exception as e:                                   # noqa: BLE001
            self.fail(f"authority_chain raised {type(e).__name__}: {e}")
        self.assertEqual(out["up_implements"],
                         [[{"citation": "OAR 166-300-0015", "external": True,
                            "via": "schedule-employment"}]])
        self.assertEqual(out["down_implemented_by"], [])

    def test_external_target_resolves_through_the_sibling_index(self):
        """The contract calls remote `corpus:id` edges a feature of this tool, so an
        external neighbour must come back as {corpus, id, url}, not merely not-raise."""
        cfg = self.corpus(edges=self.EXTERNAL_EDGE,
                          siblings=self.sibling_block(index_path=self.write_sibling_index()))
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        got = self.framework(cfg).graph_neighbors("schedule-employment")["related"][0]

        self.assertTrue(got["external"])
        self.assertEqual(got["id"], "oar-166-300-0015")
        self.assertEqual(got["corpus"], "executive-regulatory-frameworks")
        self.assertEqual(got["doc_type"], "administrative-rule")
        self.assertEqual(got["title"], "OAR 166-300-0015 — General Schedule Rules")
        self.assertEqual(got["url"], "https://github.com/OregonAI/executive-regulatory-"
                                     "frameworks/blob/main/rules/oar-166/oar-166-300-0015.md")
        self.assertEqual(got["resolved_via"], "sibling:executive-regulatory-frameworks")

    def test_unreachable_sibling_says_could_not_check_and_fabricates_nothing(self):
        cfg = self.corpus(edges=self.EXTERNAL_EDGE, siblings=self.sibling_block(
            index_url="https://nonexistent.invalid/corpus-index.json"))
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        got = self.framework(cfg).graph_neighbors("schedule-employment")["related"][0]

        self.assertTrue(got["external"])
        self.assertNotIn("id", got)                  # nothing invented
        self.assertNotIn("url", got)
        self.assertEqual(got["sibling_unavailable"], "executive-regulatory-frameworks")
        self.assertIn("NOT evidence it is absent", got["note"])

    def test_local_targets_keep_their_historical_shape(self):
        cfg = self.corpus(
            extra_docs=[("schedule-parks", "Parks Records Schedule", "166-400")],
            edges=[{"from": "schedule-employment", "type": "related",
                    "to": "schedule-parks"}])

        out = self.framework(cfg).graph_neighbors("schedule-employment")

        self.assertEqual(out["related"], [{"id": "schedule-parks",
                                           "title": "Parks Records Schedule",
                                           "doc_type": "schedule"}])

    def test_authority_chain_frontier_does_not_follow_external_neighbours(self):
        """An external neighbour has no local edges. Following it would re-emit the same
        record at every level up to `depth`, which reads as a real chain and is not one."""
        cfg = self.corpus(edges=[{"from": "schedule-employment", "type": "implements",
                                  "to": "OAR 166-300-0015"},
                                 {"from": "OAR 166-300-0015", "type": "implements",
                                  "to": "OAR 166-300-0015"}])
        out = self.framework(cfg).authority_chain("schedule-employment", depth=6)
        self.assertEqual(len(out["up_implements"]), 1)


class TestNoGraphCondition(GraphToolTestCase):
    """corpus-toolkit#5 — three conditions that used to collapse into one false message."""

    def test_absent_graph_reports_the_graph_not_the_document(self):
        cfg = self.corpus(graph=False)
        fw = self.framework(cfg)
        for tool in ("graph_neighbors", "authority_chain"):
            with self.subTest(tool=tool):
                out = getattr(fw, tool)("schedule-employment")
                self.assertTrue(out["no_graph"])
                self.assertEqual(out["error"], "this corpus has no relationship graph")
                # The precise regression: the document EXISTS and must never be denied.
                self.assertNotIn("no document with id", out["error"])
                self.assertIn("NOT a statement about whether", out["note"])

    def test_the_document_the_error_denied_is_in_fact_served(self):
        """Ties the error to reality rather than to a string: the same framework that
        answered the graph question serves the document with provenance."""
        cfg = self.corpus(graph=False)
        fw = self.framework(cfg)
        self.assertTrue(fw.graph_neighbors("schedule-employment")["no_graph"])
        doc = fw.get_document("schedule-employment")
        self.assertEqual(doc["id"], "schedule-employment")
        self.assertNotIn("error", doc)

    def test_stale_graph_names_the_rebuild_not_a_missing_document(self):
        """A graph that predates a document. Silent by construction — this is what the
        corpus-side `generated` CI job exists to catch."""
        cfg = self.corpus(
            extra_docs=[("schedule-parks", "Parks Records Schedule", "166-400")],
            graph_nodes=[{"id": "schedule-employment",
                          "title": "Employment Records Schedule",
                          "doc_type": "schedule",
                          "path": "schedules/schedule-employment.md"}])

        out = self.framework(cfg).graph_neighbors("schedule-parks")

        self.assertTrue(out["not_in_graph"])
        self.assertIn("exists but is not a node", out["error"])
        self.assertIn("rebuild", out["note"])

    def test_a_genuinely_absent_document_still_says_so(self):
        """The guard must not have been widened into never reporting a missing id."""
        cfg = self.corpus()
        out = self.framework(cfg).graph_neighbors("no-such-schedule")
        self.assertEqual(out["error"], "no document with id 'no-such-schedule'")
        self.assertNotIn("no_graph", out)
        self.assertNotIn("not_in_graph", out)


class TestResponseConvention1(GraphToolTestCase):
    """corpus-toolkit#6 — `corpus`, `archetype`, `authoritative_source` on every
    response, errors included."""

    SOURCE = "https://sos.oregon.gov/archives/records/Pages/default.aspx"

    def test_corpus_overview_carries_a_declared_authoritative_source(self):
        cfg = self.corpus(authoritative_source=self.SOURCE)
        out = self.framework(cfg).corpus_overview()
        self.assertEqual(out["authoritative_source"], self.SOURCE)
        self.assertNotIn("config_warning", out)

    def test_a_corpus_without_one_is_told_so_rather_than_quietly_omitting_it(self):
        cfg = self.corpus()
        out = self.framework(cfg).corpus_overview()
        self.assertIn("authoritative_source", out)       # key present...
        self.assertIsNone(out["authoritative_source"])   # ...and explicitly null
        self.assertIn("corpus.authoritative_source", out["config_warning"])

    def test_every_graph_response_carries_the_envelope_including_errors(self):
        cfg = self.corpus(authoritative_source=self.SOURCE,
                          edges=[{"from": "schedule-employment", "type": "related",
                                  "to": "OAR 166-300-0015"}])
        fw = self.framework(cfg)
        responses = {
            "graph_neighbors": fw.graph_neighbors("schedule-employment"),
            "graph_neighbors/missing": fw.graph_neighbors("nope"),
            "authority_chain": fw.authority_chain("schedule-employment"),
            "authority_chain/missing": fw.authority_chain("nope"),
            "resolve_citation": fw.resolve_citation("Schedule 166-300"),
            "corpus_overview": fw.corpus_overview(),
            "get_document": fw.get_document("schedule-employment"),
            "get_document/missing": fw.get_document("nope"),
        }
        for label, out in responses.items():
            with self.subTest(response=label):
                self.assertEqual(out["corpus"], "records-retention")
                self.assertEqual(out["archetype"], "document")
                self.assertTrue(out["authoritative_source"],
                                f"{label} carries no authoritative_source")

    def test_a_documents_own_source_url_wins_over_the_corpus_level_url(self):
        """Convention 1 asks for an authoritative source, not for the coarsest one:
        a document knows exactly where its official text is."""
        cfg = self.corpus(authoritative_source=self.SOURCE)
        out = self.framework(cfg).get_document("schedule-employment")
        self.assertEqual(out["authoritative_source"], "https://sos.oregon.gov/166-300")


if __name__ == "__main__":
    unittest.main(verbosity=2)
