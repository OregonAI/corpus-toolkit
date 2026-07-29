#!/usr/bin/env python3
"""corpus-validate-frontmatter's corpus-wide checks: `joins[].document_id` resolution
(corpus-toolkit#3) and the `corpus.authoritative_source` config check
(corpus-toolkit#6).

Both are gates, so both are written the way a gate has to be tested: the assertion is
that the command EXITS NON-ZERO with the right message on the wrong input, not that it
exits zero on the right one. A guard that only ever sees valid input is
indistinguishable from a guard that cannot fire, and this codebase has shipped nine of
those.
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit.validate import frontmatter as fm_mod        # noqa: E402

CORPUS_YML = """\
corpus:
  id: budget
  name: Budget
  jurisdiction: oregon
  archetype: hybrid
{extra}  schema_version: 1
  contract_version: 1
content_roots:
  - path: documents
    doc_type: dataset_doc
"""

DOC = """\
---
schema_version: 1
corpus: budget
jurisdiction: oregon
id: {id}
title: {title}
doc_type: dataset_doc
citation: {title}
issuing_body: Department of Administrative Services
source_url: https://example.org/{id}
source_format: json
status: current
content_mode: summary
last_verified: ""
verified_by: ""
maintainer: "@OregonAI/maintainers"
{joins}---

## At a glance

NON-AUTHORITATIVE curated copy. Verify at source.
"""


class ValidateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="corpus-fmv-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        (self.root / "_meta").mkdir(parents=True)
        (self.root / "documents").mkdir(parents=True)

    def write_corpus(self, *, authoritative_source=None):
        extra = (f"  authoritative_source: {authoritative_source}\n"
                 if authoritative_source is not None else "")
        (self.root / "_meta" / "corpus.yml").write_text(CORPUS_YML.format(extra=extra))

    def write_doc(self, doc_id, title, joins=""):
        (self.root / "documents" / f"{doc_id}.md").write_text(
            DOC.format(id=doc_id, title=title, joins=joins))

    def validate(self, *extra_argv):
        """Run the CLI in-process. Returns (exit_code, combined output)."""
        argv = sys.argv
        sys.argv = ["corpus-validate-frontmatter", "--config",
                    str(self.root / "_meta" / "corpus.yml"), *extra_argv]
        buf = io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                fm_mod.main()
        except SystemExit as e:
            code = e.code or 0
        finally:
            sys.argv = argv
        return code, buf.getvalue()


class TestJoinReferentialIntegrity(ValidateTestCase):
    """corpus-toolkit#3 — the field was shape-validated and nothing read it."""

    JOINS = ("joins:\n"
             "  - document_id: {target}\n"
             "    dataset: expenditures\n"
             "    key: fund-100\n")

    def test_a_dangling_document_id_fails_the_gate(self):
        self.write_corpus(authoritative_source="https://example.org/budget")
        self.write_doc("appropriation-100", "Appropriation 100",
                       self.JOINS.format(target="does-not-exist"))

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a dangling join:\n{out}")
        self.assertIn("joins[0].document_id", out)
        self.assertIn("'does-not-exist' does not resolve", out)

    def test_a_resolvable_document_id_passes(self):
        self.write_corpus(authoritative_source="https://example.org/budget")
        self.write_doc("spending-100", "Spending 100")
        self.write_doc("appropriation-100", "Appropriation 100",
                       self.JOINS.format(target="spending-100"))

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertNotIn("joins[", out)

    def test_the_check_links_entry_point_covers_joins_too(self):
        """--check-relationships is what the check-links reusable workflow runs. A join
        is a reference; leaving it out of that path would mean the gate exists in a
        command no corpus's CI actually invokes."""
        self.write_corpus(authoritative_source="https://example.org/budget")
        self.write_doc("appropriation-100", "Appropriation 100",
                       self.JOINS.format(target="does-not-exist"))

        code, out = self.validate("--check-relationships")

        self.assertEqual(code, 1, f"--check-relationships passed on a dangling join:\n{out}")
        self.assertIn("does not resolve", out)

    def test_a_join_target_outside_the_changed_set_still_resolves(self):
        """The resolution universe must stay corpus-wide even when validation is scoped.
        Otherwise a one-file PR fails on joins that are perfectly valid — the same
        mistake _all_content_ids was written to prevent for relationships."""
        self.write_corpus(authoritative_source="https://example.org/budget")
        self.write_doc("spending-100", "Spending 100")
        joined = self.root / "documents" / "appropriation-100.md"
        joined.write_text(DOC.format(id="appropriation-100", title="Appropriation 100",
                                     joins=self.JOINS.format(target="spending-100")))

        universe = fm_mod._resolution_universe(
            fm_mod.config_mod.load(self.root / "_meta" / "corpus.yml"), {})
        config = fm_mod.config_mod.load(self.root / "_meta" / "corpus.yml")

        self.assertEqual(fm_mod._join_findings([joined], universe, config), [])


class TestAuthoritativeSourceConfigCheck(ValidateTestCase):
    """corpus-toolkit#6, part 3."""

    def test_a_missing_authoritative_source_is_reported_but_does_not_fail(self):
        """Deliberately a warning: all four live corpora omit it today and a hard
        failure would redden their CI on the next pin bump. It must still be SAID."""
        self.write_corpus()
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertIn("warning", out)
        self.assertIn("corpus.authoritative_source is not set", out)

    def test_a_non_url_authoritative_source_is_an_error(self):
        """Convention 1 says the field IS a URL, so a caller will try to follow it —
        a plausible-looking non-URL is worse than the omission."""
        self.write_corpus(authoritative_source='"Oregon Secretary of State"')
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a non-URL authoritative_source:\n{out}")
        self.assertIn("must be a URL", out)

    def test_a_real_url_is_silent(self):
        self.write_corpus(authoritative_source="https://sos.oregon.gov/archives/")
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertNotIn("authoritative_source", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
