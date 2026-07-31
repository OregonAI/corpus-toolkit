"""Tests for the `recheck:` dead-config notice.

The pair that matters is the same one every detector needs: it must FIRE on a manifest
that declares the key, and stay SILENT on one that does not. A notice that prints
unconditionally is noise nobody reads, and a notice that never prints is the dead key all
over again.

`recheck` configures nothing — `corpus-detect-changes` checks every source on every run,
and the real cadence is the calling workflow's cron. oregon-audits declared `annual`,
because an audit report is immutable once published, and re-fetched 242 PDFs weekly.
"""
import io
import shutil
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from corpus_toolkit import config as config_mod
from corpus_toolkit.sources.changes import _warn_recheck_is_not_honoured

CORPUS_YML = """\
schema_version: 1
corpus:
  id: test-corpus
  name: Test Corpus
  jurisdiction: oregon
  archetype: document
content_roots:
  - path: "documents"
    doc_type: rule
source_manifest_path: _meta/source-manifest.yml
"""


class RecheckNoticeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta").mkdir()
        (self.root / "documents").mkdir()
        (self.root / "_meta" / "corpus.yml").write_text(CORPUS_YML)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, manifest: str) -> tuple[int, str]:
        (self.root / "_meta" / "source-manifest.yml").write_text(textwrap.dedent(manifest))
        config = config_mod.load(self.root / "_meta" / "corpus.yml")
        buf = io.StringIO()
        with redirect_stderr(buf):
            n = _warn_recheck_is_not_honoured(config)
        return n, buf.getvalue()

    def test_silent_when_no_recheck_is_declared(self):
        n, err = self._run("""
            sources:
              - id: "a"
                url: "https://example.gov/a.pdf"
                format: pdf
        """)
        self.assertEqual(n, 0)
        self.assertEqual(err, "")

    def test_fires_on_a_per_source_declaration(self):
        n, err = self._run("""
            sources:
              - id: "a"
                url: "https://example.gov/a.pdf"
                format: pdf
                recheck: annual
        """)
        self.assertEqual(n, 1)
        self.assertIn("NOT honoured", err)
        self.assertIn("a", err)

    def test_fires_on_a_group_level_declaration(self):
        n, err = self._run("""
            recheck: annual
            sources:
              - id: "a"
                url: "https://example.gov/a.pdf"
                format: pdf
        """)
        self.assertEqual(n, 1)
        self.assertIn("group-level", err)

    def test_counts_both_levels_and_truncates_the_list(self):
        srcs = "\n".join(
            f'  - id: "s{i}"\n    url: "https://example.gov/{i}.pdf"\n'
            f'    format: pdf\n    recheck: annual' for i in range(8))
        n, err = self._run(f"recheck: annual\nsources:\n{srcs}\n")
        self.assertEqual(n, 9)                       # 8 sources + the group
        self.assertIn("+4 more", err)                # 5 shown of 9


if __name__ == "__main__":
    unittest.main()
