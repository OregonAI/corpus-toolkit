"""Tests for the unsourced-frontmatter detector.

The two that matter are the pair: it must FIRE on a real fabrication and stay SILENT on
a legitimately-unsourced corpus. A detector that only does the first is a nuisance nobody
runs twice — the mature corpus produced 1,792 false reports before `citation` was
excluded, which is what that exclusion is defending.
"""
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from corpus_toolkit import config as config_mod
from corpus_toolkit.grounding import CITATION_RE, DATEISH_RE, fold, scan

DOC = """---
schema_version: 1
corpus: test-corpus
jurisdiction: oregon
id: {id}
title: "{title}"
doc_type: rule
citation: "{citation}"
authority_level: state_rule
issuing_body: "Test Body"
legal_authority: {authority}
source_url: "https://example.gov/{id}"
source_format: html
retrieved: "2026-07-26"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: ""
verified_by: ""
maintainer: "@x"
---

> NON-AUTHORITATIVE

# {title}

## Full text

{body}
"""


class GroundingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "_meta" / "snapshots").mkdir(parents=True)
        (self.tmp / "rules").mkdir()
        (self.tmp / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
            corpus:
              id: test-corpus
              jurisdiction: oregon
              archetype: document
            content_roots:
              - path: "rules"
                doc_type: rule
        """).strip() + "\n")

    def _doc(self, doc_id, body, authority="[]", citation="OAR 123-456-0001"):
        (self.tmp / "rules" / f"{doc_id}.md").write_text(
            DOC.format(id=doc_id, title=f"Rule {doc_id}", citation=citation,
                       authority=authority, sha="a" * 64, body=body))
        (self.tmp / "_meta" / "snapshots" / f"{doc_id}.txt").write_text(body)

    def _scan(self):
        return scan(config_mod.load(str(self.tmp / "_meta" / "corpus.yml")))

    def test_fires_on_a_citation_present_in_no_source(self):
        """The oregon-records-retention failure exactly: three plausible citations
        hard-coded onto every document and present in none of them."""
        for i in range(3):
            self._doc(f"r-{i}", f"Body text of rule {i}, long enough to be real.",
                      authority='["ORS 192.005", "OAR 166-030-0027"]')
        res = self._scan()
        misses = {f"{f}:{v}" for (f, v) in res["citation_misses"]}
        self.assertIn("legal_authority:ORS 192.005", misses)
        self.assertIn("legal_authority:OAR 166-030-0027", misses)
        self.assertEqual(len(res["citation_misses"][("legal_authority", "ORS 192.005")]), 3)

    def test_silent_when_the_citation_is_actually_in_the_source(self):
        self._doc("r-1", "This rule implements ORS 192.005 as written.",
                  authority='["ORS 192.005"]')
        self.assertEqual(dict(self._scan()["citation_misses"]), {})

    def test_a_documents_own_citation_is_not_an_assertion(self):
        """A statute's body is the text OF its own citation and has no reason to restate
        its number. Checking `citation` produced 1,792 false reports on the real corpus."""
        self._doc("ors-100.022", "Definitions for this chapter follow.",
                  citation="ORS 100.022")
        self.assertEqual(dict(self._scan()["citation_misses"]), {})

    def test_dates_are_not_treated_as_citations(self):
        for s in ("September 2015", "Dec. 2012", "July 31, 2003", "2017-08-31"):
            self.assertTrue(DATEISH_RE.match(s) or not CITATION_RE.match(s),
                            f"{s!r} must not register as a citation")
        for s in ("ORS 192.005", "OAR 166-030-0027", "42 CFR 2.1"):
            self.assertTrue(CITATION_RE.match(s), f"{s!r} should register as a citation")

    def test_constant_and_absent_is_reported_separately(self):
        for i in range(4):
            self._doc(f"r-{i}", f"Unique body {i} with enough text to matter.",
                      authority='["ORS 192.005"]')
        res = self._scan()
        self.assertEqual(len(res["value_docs"][("legal_authority", "ORS 192.005")]), 4)
        self.assertEqual(res["value_grounded"].get(("legal_authority", "ORS 192.005"), 0), 0)

    def test_folding_prevents_false_reports(self):
        """Typographic punctuation in the source vs ASCII in the field is the 43-false-
        alarm failure the conflict quote matcher hit; folding is what stops it here."""
        self.assertEqual(fold("“Curly” — text"), fold('"Curly" - text'))

    def test_missing_snapshot_is_not_evidence_of_fabrication(self):
        self._doc("r-1", "Body text here for the rule.", authority='["ORS 192.005"]')
        (self.tmp / "_meta" / "snapshots" / "r-1.txt").unlink()
        res = self._scan()
        self.assertEqual(res["n_no_snapshot"], 1)
        self.assertEqual(dict(res["citation_misses"]), {})


if __name__ == "__main__":
    unittest.main()
