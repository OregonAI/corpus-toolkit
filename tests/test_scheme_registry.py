#!/usr/bin/env python3
"""Citation schemes are a property of a CORPUS, not of a process (corpus-toolkit#73).

The registry used to be a module-level list with a collector bolted over it, and the
collector's own fallback could not fire: `load_module` caches a corpus's citation module,
so a SECOND CorpusFramework over the same corpus re-ran none of its top-level
`register_scheme` calls, collected nothing, and fell back to the process-wide list the
collector had bypassed — which was empty. `resolve_citation` then reported "no citation
scheme recognized this format" about a corpus that recognizes it perfectly well.

Stdlib unittest only, matching the rest of tests/.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit import config as config_mod                    # noqa: E402
from corpus_toolkit.mcp.framework import (                         # noqa: E402
    CorpusFramework, clear_schemes, register_scheme,
)

CORPUS_YML = """\
corpus:
  id: repro
  name: Repro
  jurisdiction: test
  archetype: document
content_roots:
  - path: docs
    doc_type: statute
plugins:
  citation_module: "src.citations"
"""

CITATIONS_PY = """\
from corpus_toolkit.mcp.framework import register_scheme

register_scheme("ors", r"ORS\\s+(?P<num>[\\d.]+)", "ors-{num}")
register_scheme("oar", r"OAR\\s+(?P<num>[\\d-]+)", "oar-{num}")
"""


def make_corpus(root: Path, *, citation_module: bool = True) -> Path:
    (root / "_meta").mkdir(parents=True)
    (root / "docs").mkdir()
    yml = CORPUS_YML if citation_module else CORPUS_YML.split("plugins:")[0]
    (root / "_meta" / "corpus.yml").write_text(yml)
    if citation_module:
        (root / "src").mkdir()
        (root / "src" / "__init__.py").write_text("")
        (root / "src" / "citations.py").write_text(CITATIONS_PY)
    return root / "_meta" / "corpus.yml"


class SchemeRegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="corpus-schemes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        clear_schemes()
        self.addCleanup(clear_schemes)

    def framework(self, cfg_path: Path) -> CorpusFramework:
        return CorpusFramework(config_mod.load(cfg_path))


class TestRepeatedConstruction(SchemeRegistryTestCase):
    def test_second_framework_over_one_corpus_keeps_its_schemes(self):
        cfg = make_corpus(self.tmp / "repo")
        first = self.framework(cfg)
        second = self.framework(cfg)

        self.assertEqual([s[0] for s in first.schemes], ["ors", "oar"])
        self.assertEqual([s[0] for s in second.schemes], ["ors", "oar"],
                         "a second framework over the same corpus lost its schemes")

    def test_second_framework_still_recognizes_the_citation_format(self):
        """The user-visible half: `unresolved` is fine, denying the FORMAT is not.

        A corpus with no documents resolves nothing either way — what must not happen is
        the response claiming no scheme matched, which tells an agent the corpus does not
        handle this citation style at all.
        """
        cfg = make_corpus(self.tmp / "repo")
        self.framework(cfg)
        out = self.framework(cfg).resolve_citation("ORS 192.355")

        self.assertNotIn("no citation scheme recognized this format", out.get("note", ""))
        self.assertEqual(out["schemes_attempted"], ["ors", "oar"])

    def test_clearing_the_registry_does_not_strand_the_next_framework(self):
        """`clear_schemes()` evicts the cache, so the next collect must RE-EXECUTE the
        module rather than be handed the cached one. Without force=True this is the bug
        again, one layer down."""
        cfg = make_corpus(self.tmp / "repo")
        self.framework(cfg)
        clear_schemes()

        self.assertEqual([s[0] for s in self.framework(cfg).schemes], ["ors", "oar"])


class TestIsolationBetweenCorpora(SchemeRegistryTestCase):
    def test_two_corpora_do_not_share_schemes(self):
        a = make_corpus(self.tmp / "a")
        b_root = self.tmp / "b"
        (b_root / "_meta").mkdir(parents=True)
        (b_root / "docs").mkdir()
        (b_root / "_meta" / "corpus.yml").write_text(CORPUS_YML)
        (b_root / "src").mkdir()
        (b_root / "src" / "__init__.py").write_text("")
        (b_root / "src" / "citations.py").write_text(
            'from corpus_toolkit.mcp.framework import register_scheme\n'
            'register_scheme("schedule", r"Schedule\\s+(?P<num>[\\d-]+)", "schedule-{num}")\n')

        fa = self.framework(a)
        fb = self.framework(b_root / "_meta" / "corpus.yml")

        self.assertEqual([s[0] for s in fa.schemes], ["ors", "oar"])
        self.assertEqual([s[0] for s in fb.schemes], ["schedule"])

    def test_direct_registration_still_reaches_a_framework_without_a_module(self):
        """The module-level registry stays the path for tests and scripts that call
        register_scheme directly, with no plugins.citation_module configured."""
        cfg = make_corpus(self.tmp / "repo", citation_module=False)
        register_scheme("adhoc", r"ADHOC\s+(?P<num>\d+)", "adhoc-{num}")

        self.assertEqual([s[0] for s in self.framework(cfg).schemes], ["adhoc"])


if __name__ == "__main__":
    unittest.main()
