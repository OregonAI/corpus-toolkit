"""Tests for corpus-declared volatile patterns (corpus-toolkit#66).

`VOLATILE_PATTERNS` shipped as an empty module constant with no configuration route, so
`normalize_volatile()` was an identity function for every consumer and the guarantee its
comment states — "strip before hashing so hash drift means real content drift" — did not
hold. One OARD footer bump (`v2.1.7` -> `v2.1.8`) turned all 484 sources in ERF's `oar`
group into phantom drift with zero rule text changed.

Two halves have to hold at once, and they pull against each other:

  * a corpus that DECLARES a pattern must have it applied, or the config is decoration;
  * a corpus that declares NOTHING must hash byte-identically to the previous release, or
    this change ships its own wave of phantom drift to every other corpus.

The digests below are pinned from the v1.25.0 tag (bba3302) for exactly that second
reason — an assertion that the two code paths agree with each other would pass just as
happily if both had moved.
"""
import io
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from corpus_toolkit import config as config_mod
from corpus_toolkit.repo import content_hash
from corpus_toolkit.sources import changes

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
"""

# A page whose only per-release token is the application footer version — the ERF/OARD
# shape, reduced. Long enough that content_hash() takes the extracted-text path rather
# than the <200-char raw-byte fallback.
PAGE_V7 = (b"<html><body><p>" + b"Rule text about administrative procedure. " * 10
           + b"</p><footer>Application version v2.1.7</footer></body></html>")
PAGE_V8 = PAGE_V7.replace(b"v2.1.7", b"v2.1.8")

# Pinned from the v1.25.0 tag (bba3302); `content_hash` is byte-identical between that
# tag and this branch's merge base. If these digests move, every baseline on the platform
# is stale and every corpus has to re-seed — see #68's --record-baseline.
HTML_DIGEST_V1_25 = "03e5ddaf068609693cfb154d7a92dcb74f4fbc6e1550b5bfd0897c943aa39d6c"
BINARY_DIGEST_V1_25 = "babd03c916874ff9c0f8051abe49e50dbbcf364e192a1c59166cc7eba5501b5e"


class VolatilePatternConfigTest(unittest.TestCase):
    """The key is read from _meta/corpus.yml, and a bad one fails at LOAD."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _load(self, extra: str):
        path = self.root / "_meta" / "corpus.yml"
        path.write_text(CORPUS_YML + extra)
        return config_mod.load(path)

    def test_absent_key_is_an_empty_list(self):
        self.assertEqual(self._load("").volatile_patterns, [])

    def test_declared_patterns_are_compiled_once_at_load(self):
        # Compiled at load, not per source: 3,447 sources x N patterns is the scale this
        # runs at, and an invalid regex must fail before the first fetch, not inside the
        # loop after 40 minutes of crawling.
        cfg = self._load('volatile_patterns:\n  - "Application version v[0-9.]+"\n')
        self.assertEqual(len(cfg.volatile_patterns), 1)
        self.assertIsInstance(cfg.volatile_patterns[0], re.Pattern)
        self.assertIsInstance(cfg.volatile_patterns[0].pattern, bytes,
                              "patterns are applied to raw bytes, before text extraction")

    def test_bare_string_is_rejected(self):
        # index_headings' worst case, repeated: a non-empty string is truthy AND iterable,
        # so a bare string would be walked CHARACTER BY CHARACTER — each character compiled
        # as its own regex, several of them ('(', '[') invalid and the rest matching
        # arbitrary bytes across every source in the corpus.
        with self.assertRaises(ValueError) as cm:
            self._load('volatile_patterns: "JSESSIONID=[^;]*"\n')
        self.assertIn("volatile_patterns", str(cm.exception))
        self.assertIn("list", str(cm.exception))

    def test_invalid_regex_is_reported_loudly_and_names_the_pattern(self):
        with self.assertRaises(ValueError) as cm:
            self._load('volatile_patterns:\n  - "v2.1.[0-9"\n')
        self.assertIn("v2.1.[0-9", str(cm.exception),
                      "an operator needs to know WHICH pattern is broken")

    def test_empty_pattern_is_rejected(self):
        # An empty regex matches at every position and substitutes nothing: a no-op that
        # looks exactly like the bug #66 is about.
        with self.assertRaises(ValueError):
            self._load('volatile_patterns:\n  - ""\n')

    def test_non_string_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load("volatile_patterns:\n  - 2117\n")

    def test_mapping_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load("volatile_patterns:\n  oar: 'v[0-9.]+'\n")


class VolatileHashingTest(unittest.TestCase):
    """What the patterns do to a hash, and what they must not do to anyone else's."""

    def test_declared_pattern_absorbs_a_footer_version_bump(self):
        pats = [re.compile(rb"Application version v[0-9.]+")]
        self.assertEqual(content_hash(PAGE_V7, "html", pats),
                         content_hash(PAGE_V8, "html", pats))

    def test_without_the_pattern_the_same_bump_reads_as_drift(self):
        # The control. If this ever passes, the fixture stopped reproducing #66.
        self.assertNotEqual(content_hash(PAGE_V7, "html"), content_hash(PAGE_V8, "html"))

    def test_no_patterns_hashes_exactly_as_v1_25_0_did(self):
        self.assertEqual(content_hash(PAGE_V7, "html"), HTML_DIGEST_V1_25)
        self.assertEqual(content_hash(PAGE_V7, "html", []), HTML_DIGEST_V1_25)

    def test_binary_path_is_untouched_by_patterns(self):
        # xls/xlsx/docx hash raw bytes with no text extraction; patterns must not reach
        # that path, or a corpus declaring one silently rehashes its spreadsheets.
        pats = [re.compile(rb"Application version v[0-9.]+")]
        self.assertEqual(content_hash(PAGE_V7, "xlsx", pats), BINARY_DIGEST_V1_25)
        self.assertEqual(content_hash(PAGE_V7, "xlsx"), BINARY_DIGEST_V1_25)

    def test_builtin_list_stays_empty(self):
        # Decided in #66's triage: site-agnostic defaults (Cloudflare's data-cfemail and
        # friends) would rehash existing sources across every corpus carrying that markup
        # — a behaviour change arriving in a version bump, which is the failure mode this
        # repo's own guidance warns against. A corpus declares its own.
        from corpus_toolkit import repo
        self.assertEqual(repo.VOLATILE_PATTERNS, [])


class VolatileBreadthIsMeasuredTest(unittest.TestCase):
    """The opposite failure to an empty list, and the worse one.

    A pattern wide enough to swallow the body deletes CONTENT before hashing, and content
    removed before hashing can never produce drift. "shall be paid on the first of the
    month" and "REPEALED, all benefits terminated" then hash identically and every run
    reports `0 changed`, exit 0, forever — a check that cannot fail, reachable from one
    line of corpus config. Nothing measured that: the only signal was the inverse (a
    pattern matching nothing).

    Measured and reported rather than refused. A breadth limit would be a policy this
    toolkit has no standing to set, and refusing pushes a corpus back to patching the
    toolkit or running a second hasher — the corpus-toolkit#66 failure mode.
    """

    WIDE = re.compile(rb"<main>.*</main>", re.DOTALL)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta" / "sources").mkdir(parents=True)
        (self.root / "documents").mkdir()
        self.bodies = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # The boilerplate is long enough on purpose: strip <main> and what remains is still
    # over 200 characters, so content_hash stays on the extracted-text path. A page whose
    # ENTIRE text is inside the stripped region falls back to the raw-byte hash instead
    # and accidentally survives — which is why this hazard cannot be waved off as
    # theoretical for pages with real chrome around the body.
    HEADER = ("Oregon Administrative Rules. This page is served by the rules database "
              "and reproduced here without warranty. Navigation: home, chapters, "
              "divisions, agencies, search, help, contact, accessibility, terms. ") * 2

    def _page(self, body: str) -> bytes:
        return ("<html><head><title>Benefit rule</title></head><body><header>"
                + self.HEADER + "</header><main>" + (body + " ") * 40
                + "</main><footer>Application version v2.1.7</footer>"
                  "</body></html>").encode()

    def _write(self, pattern: str):
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + f"source_manifest_path: _meta/sources\nvolatile_patterns:\n"
                         f"  - \"{pattern}\"\n")
        self.bodies["https://example.gov/rule"] = self._page("shall be paid on the first "
                                                             "of the month")
        (self.root / "_meta" / "sources" / "rules.yml").write_text(
            'sources:\n  - id: "rule"\n    url: "https://example.gov/rule"\n'
            '    format: html\n    sha256: ""\n')

    def _run(self):
        args = ["corpus-detect-changes", "--config",
                str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", lambda url: self.bodies[url]), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                changes.main()
            except SystemExit:
                pass
        return err.getvalue()

    def test_a_pattern_that_swallows_the_body_collides_two_different_documents(self):
        # The reproduction, at the hashing layer: this is what the warning is warning about.
        paid = self._page("shall be paid on the first of the month")
        repealed = self._page("REPEALED, all benefits terminated")
        self.assertNotEqual(content_hash(paid, "html"), content_hash(repealed, "html"))
        self.assertEqual(content_hash(paid, "html", [self.WIDE]),
                         content_hash(repealed, "html", [self.WIDE]),
                         "a wide pattern makes two opposite rules hash the same — this is "
                         "the condition the run has to report")

    def test_the_run_warns_with_the_measured_share(self):
        self._write("<main>[^<]*</main>")
        err = self._run()
        self.assertIn("WARNING", err)
        self.assertIn("volatile pattern", err)
        self.assertRegex(err, r"removed \d+ byte\(s\), \d+\.\d%",
                         "the number has to be IN the message — an adjective is not a "
                         "measurement a reviewer can weigh")
        self.assertIn("can never report drift again", err.replace("\n", " "))

    def test_a_narrow_pattern_is_reported_without_a_warning(self):
        self._write("Application version v[0-9.]+")
        err = self._run()
        self.assertNotIn("WARNING", err)
        self.assertIn("removing", err)
        self.assertIn("byte(s)", err)

    def test_the_threshold_is_stated_and_far_above_any_real_token(self):
        # A session id or footer version is tens of bytes in a page of tens of thousands.
        self.assertLessEqual(changes.VOLATILE_BREADTH_WARN, 0.25)
        self.assertGreater(changes.VOLATILE_BREADTH_WARN, 0.01)


if __name__ == "__main__":
    unittest.main()


def test_a_short_page_s_raw_byte_fallback_still_honours_declared_patterns():
    """A viewer shell: under 200 characters of visible text, so `content_hash` falls back to
    hashing bytes -- and those bytes must be the pattern-stripped ones, or a per-request token
    defeats every declared pattern on exactly the pages that need one (oregon-audits#47)."""
    from corpus_toolkit.repo import content_hash
    shell = (b'<html><body><form><input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" '
             b'value="%s" /><script>var myPdfBase64 = \'JVBERi0xLjQ=\'</script>'
             b'<p>Record Viewer</p></form></body></html>')
    a, b = shell % b"tokenA1", shell % b"tokenB2"
    pat = re.compile(rb'name="__VIEWSTATE" id="__VIEWSTATE" value="[^"]*"')
    assert content_hash(a, "html") != content_hash(b, "html")          # the defect, un-patterned
    assert content_hash(a, "html", [pat]) == content_hash(b, "html", [pat])
    # and a change to what the shell actually carries is still seen
    c = shell.replace(b"JVBERi0xLjQ=", b"JVBERi0xLjc=") % b"tokenC3"
    assert content_hash(a, "html", [pat]) != content_hash(c, "html", [pat])
