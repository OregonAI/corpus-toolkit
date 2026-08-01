#!/usr/bin/env python3
"""The shared landing-page shell (corpus_toolkit.site).

Stdlib unittest only, matching the rest of the suite. Run with:

    python3 -m unittest discover -s tests -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit import config as config_mod                       # noqa: E402
from corpus_toolkit.site import Page, Section, Tile, build, render    # noqa: E402

CORPUS_YML = """\
corpus:
  id: test-corpus
  name: Test Corpus
  jurisdiction: oregon
  archetype: document
  schema_version: 1
  contract_version: 1
  authoritative_source: "https://example.invalid/official"
content_roots:
  - path: documents
    doc_type: policy
disclaimer_marker: "NON-AUTHORITATIVE"
snapshot_dir: _meta/snapshots
"""

DOC = """\
---
schema_version: 1
corpus: "test-corpus"
jurisdiction: "oregon"
id: doc-one
title: "A Policy"
doc_type: policy
citation: "P-1"
authority_level: policy
issuing_body: "Somebody"
agency: statewide
source_url: "https://example.invalid/p1"
retrieved: "2026-01-01"
content_mode: verbatim
relationships:
  implements: []
  implemented_by: []
  references_external: []
  related: []
  supersedes: []
---

> **NON-AUTHORITATIVE**

## Full text

Text.
"""


def make_page(tmp: Path, **over):
    (tmp / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp / "_meta" / "corpus.yml").write_text(CORPUS_YML)
    (tmp / "documents").mkdir(exist_ok=True)
    (tmp / "documents" / "doc-one.md").write_text(DOC)
    cfg = config_mod.load(tmp / "_meta" / "corpus.yml")
    kw = dict(
        config=cfg, repo="test-corpus",
        title="Test Corpus — a title",
        description="A description.",
        eyebrow="Oregon · somebody",
        headline="A headline sentence",
        lede_html="A <b>lede</b>.",
        disclaimer="NON-AUTHORITATIVE reference.",
        tiles=[Tile("Documents", "1", "just the one")],
        sections=[Section("For agents", '<ul class="plain"><li>Hi</li></ul>')],
    )
    kw.update(over)
    return Page(**kw)


class SiteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TestRender(SiteTestCase):
    def test_no_placeholder_survives_a_render(self):
        """The whole point of the guard. A leftover `__MCP__` publishes a page that looks
        finished and tells an agent the wrong endpoint."""
        html = render(make_page(self.tmp))
        self.assertNotIn("__", html.replace("_blank", ""))
        self.assertNotIn("<!--TILES-->", html)
        self.assertNotIn("<!--SECTIONS-->", html)

    def test_unfilled_slot_raises_rather_than_shipping(self):
        page = make_page(self.tmp)
        import corpus_toolkit.site as site_mod
        original = site_mod.TEMPLATE
        site_mod.TEMPLATE = original.replace("__TITLE__", "__TITLE__ __A_NEW_SLOT__")
        try:
            with self.assertRaises(ValueError) as cm:
                render(page)
            self.assertIn("__A_NEW_SLOT__", str(cm.exception))
        finally:
            site_mod.TEMPLATE = original

    def test_content_lands_where_the_caller_put_it(self):
        html = render(make_page(self.tmp))
        self.assertIn("A headline sentence", html)
        self.assertIn("A <b>lede</b>.", html)          # lede_html is NOT escaped
        self.assertIn("just the one", html)
        self.assertIn("For agents", html)
        self.assertIn("https://oregonai.morficflux.com/test-corpus/mcp", html)

    def test_tile_values_are_escaped(self):
        """Tiles carry derived data, so a corpus that computes a label containing markup
        must not be able to inject it."""
        html = render(make_page(self.tmp, tiles=[Tile("<script>x</script>", "1&2")]))
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("1&amp;2", html)


class TestBuild(SiteTestCase):
    def test_emits_the_cross_corpus_index_at_the_site_root(self):
        """The contract that fails in SOMEONE ELSE's repository. A corpus deploying Pages
        itself cannot also call the reusable publish-index workflow, so if the page builder
        does not write this file, sibling resolution breaks silently while the corpus keeps
        serving perfectly."""
        page = make_page(self.tmp)
        out = build(page)
        idx = page.site_dir / "corpus-index.json"
        self.assertTrue(idx.is_file())
        data = json.loads(idx.read_text())
        self.assertEqual(data["corpus"], "test-corpus")
        self.assertEqual(data["n_documents"], 1)
        self.assertIn("doc-one", data["documents"])
        self.assertIn("1 documents", out["index"])

    def test_writes_nojekyll(self):
        """Without it Pages runs Jekyll, which silently drops anything starting with `_`."""
        page = make_page(self.tmp)
        build(page)
        self.assertTrue((page.site_dir / ".nojekyll").is_file())

    def test_missing_extra_file_is_reported_not_skipped(self):
        """An absent visualisation renders as a dead link on a page that looks finished."""
        page = make_page(self.tmp, extra_files=[self.tmp / "viz" / "nope.html"])
        out = build(page)
        self.assertTrue(any(c.startswith("MISSING") for c in out["copied"]))

    def test_llms_txt_is_copied_when_present(self):
        (self.tmp / "llms.txt").write_text("# llms")
        page = make_page(self.tmp)
        out = build(page)
        self.assertIn("llms.txt", out["copied"])
        self.assertEqual((page.site_dir / "llms.txt").read_text(), "# llms")


if __name__ == "__main__":
    unittest.main(verbosity=2)
