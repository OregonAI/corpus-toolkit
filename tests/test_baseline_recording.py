"""Tests for `corpus-detect-changes --record-baseline` (corpus-toolkit#68).

The detector computed exactly the value the manifest needs, compared it, printed it,
wrote it to `changed-sources.tsv` — and discarded it. Nothing in the package ever
assigned `sha256`, so the only route to a baseline was a per-corpus script
reimplementing `content_hash` (format inference, volatile normalization, `pdftotext
-layout`, whitespace normalization, the <200-char raw-byte fallback). Any divergence is
silent and permanent: every source reports CHANGED forever. oregon-counties ran that way
for its whole lifetime — 3,447 sources, all `sha256: ''`, 25 spurious issues and 3,366
unreported ones per week, concluding `success`.

The manifest is CURATED DATA a human reviews in a PR, so the tests below pin the limits of
what the recorder may touch as hard as they pin that it writes at all:

  * it writes only under an explicit flag, never as a side effect of a drift run;
  * seed mode fills EMPTY baselines only — overwriting a recorded one is accepting an
    upstream change without review, and needs `--record-baseline=refresh` to say so;
  * a failed fetch leaves the recorded value byte-for-byte alone;
  * everything else in the file survives — comments, key order, other keys, group names.
"""
import io
import shutil
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

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
source_manifest_path: {manifest}
"""

BODY_A = b"<html><body><p>" + b"Ordinance text for source A. " * 20 + b"</p></body></html>"
BODY_B = b"<html><body><p>" + b"Ordinance text for source B. " * 20 + b"</p></body></html>"

HASH_A = content_hash(BODY_A, "html")
HASH_B = content_hash(BODY_B, "html")


class _CorpusFixture(unittest.TestCase):
    """A real corpus on disk, driven through main() with only the network faked."""

    manifest = "_meta/source-manifest.yml"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta").mkdir()
        (self.root / "documents").mkdir()
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML.format(manifest=self.manifest))
        self.bodies = {"https://example.gov/a": BODY_A, "https://example.gov/b": BODY_B}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_manifest(self, text, name="source-manifest.yml", subdir=None):
        target = self.root / "_meta" / (subdir or "")
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(textwrap.dedent(text))
        return target / name

    def _fetch(self, url):
        body = self.bodies[url]
        if isinstance(body, Exception):
            raise body
        return body

    def run_cli(self, *argv):
        args = ["corpus-detect-changes", "--config",
                str(self.root / "_meta" / "corpus.yml"), *argv]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                changes.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()


class SeedAnEmptyManifestTest(_CorpusFixture):
    MANIFEST = """\
        group: ordinances
        last_checked: 2026-07-31
        sources:
          - id: "src-a"
            url: "https://example.gov/a"
            format: html
            sha256: ""            # filled at first fetch
          - id: "src-b"
            url: "https://example.gov/b"
            format: html
            sha256: ""
    """

    def test_seeding_then_an_ordinary_run_reports_zero_changed(self):
        path = self.write_manifest(self.MANIFEST)
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, err)
        self.assertIn(HASH_A, path.read_text())
        self.assertIn(HASH_B, path.read_text())
        self.assertIn("2 baseline(s) recorded", out)

        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertIn("0 changed", out)
        self.assertIn("0 with no recorded baseline", out)

    def test_everything_else_in_the_file_survives(self):
        path = self.write_manifest(self.MANIFEST)
        before = path.read_text()
        self.run_cli("--record-baseline")
        after = path.read_text()
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        diff = [(b, a) for b, a in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(diff), 2, f"only the two sha256 lines may change: {diff}")
        for b, a in diff:
            self.assertIn("sha256", b)
        self.assertIn("group: ordinances", after)
        self.assertIn("last_checked: 2026-07-31", after)
        self.assertIn("# filled at first fetch", after,
                      "a curator's comment must survive; a yaml round-trip would eat it")

    def test_recording_opens_no_issues(self):
        self.write_manifest(self.MANIFEST)
        with mock.patch.object(changes, "_open_issue") as open_issue:
            code, out, err = self.run_cli("--record-baseline", "--open-issues")
        open_issue.assert_not_called()
        self.assertEqual(code, 2, "seeding is not a drift report; the combination is refused")
        self.assertIn("--record-baseline", err)
        self.assertIn("--open-issues", err)
        self.assertNotIn("unrecognized", err,
                         "must be REFUSED with a reason, not rejected as an unknown flag")
        self.assertIn("seeding", err.lower(),
                      "the refusal has to say why, or an operator just drops one flag")

    def test_a_source_with_no_sha256_key_at_all_gets_one(self):
        path = self.write_manifest("""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
        """)
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, err)
        self.assertIn(HASH_A, path.read_text())


class FetchFailureIsNotABaselineTest(_CorpusFixture):
    def test_failed_fetch_leaves_the_recorded_value_untouched(self):
        # 56 fetch failures across 6 hosts in the run that motivated #68. A 403 must never
        # overwrite a good baseline and must never write an empty one — "could not check"
        # is not "unchanged".
        path = self.write_manifest(f"""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
                sha256: "{HASH_A}"
              - id: "src-b"
                url: "https://example.gov/b"
                format: html
                sha256: ""
        """)
        self.bodies["https://example.gov/b"] = OSError("HTTP Error 403: Forbidden")
        before = path.read_text()
        code, out, err = self.run_cli("--record-baseline")
        after = path.read_text()
        self.assertEqual(after, before, "nothing may be written for a source we could not fetch")
        self.assertIn("1 skipped (fetch failed)", out)
        self.assertIn("1 already current", out)


class SeedDoesNotOverwriteCuratedValuesTest(_CorpusFixture):
    """Seed fills blanks. Replacing a recorded baseline is accepting drift, and says so."""

    def _manifest(self):
        return self.write_manifest(f"""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
                sha256: "{'0' * 64}"
        """)

    def test_seed_mode_leaves_a_recorded_baseline_alone(self):
        path = self._manifest()
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, err)
        self.assertIn("0" * 64, path.read_text())
        self.assertNotIn(HASH_A, path.read_text())
        self.assertIn("1 left alone", out)
        self.assertIn("--record-baseline=refresh", out)

    def test_refresh_mode_replaces_it(self):
        path = self._manifest()
        code, out, err = self.run_cli("--record-baseline=refresh")
        self.assertEqual(code, 0, err)
        self.assertIn(HASH_A, path.read_text())
        self.assertNotIn("0" * 64, path.read_text())


class GroupScopingTest(_CorpusFixture):
    manifest = "_meta/sources"

    def setUp(self):
        super().setUp()
        self.a = self.write_manifest("""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
                sha256: ""
        """, name="alpha.yml", subdir="sources")
        self.b = self.write_manifest("""\
            sources:
              - id: "src-b"
                url: "https://example.gov/b"
                format: html
                sha256: ""
        """, name="beta.yml", subdir="sources")

    def test_recording_honours_group_scoping_exactly_as_detection_does(self):
        untouched = self.b.read_text()
        code, out, err = self.run_cli("--record-baseline", "--group", "alpha")
        self.assertEqual(code, 0, err)
        self.assertIn(HASH_A, self.a.read_text())
        self.assertEqual(self.b.read_text(), untouched,
                         "a group the run did not check must not be rewritten")


class RefusesAnUnverifiableRewriteTest(_CorpusFixture):
    def test_duplicate_ids_are_refused_rather_than_guessed(self):
        # Two entries with one id: the recorder cannot tell which line belongs to which
        # fetch, so it writes neither and says which id it skipped. Silently writing one
        # hash into both is how a manifest acquires a baseline that is wrong for a source
        # nobody will re-examine.
        path = self.write_manifest("""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
                sha256: ""
              - id: "src-a"
                url: "https://example.gov/b"
                format: html
                sha256: ""
        """)
        before = path.read_text()
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(path.read_text(), before)
        self.assertIn("src-a", err)
        self.assertIn("duplicate", err.lower())
        self.assertEqual(code, 1, "a run asked to record, that recorded nothing, is not a "
                                  "clean run — in CI it is the documented remedy for #68 "
                                  "going green having done nothing")


class RefusalSaysWhatActuallyHappenedTest(_CorpusFixture):
    """A refusal message an operator can act on without losing work."""

    def test_the_other_sources_in_the_file_are_still_written_and_the_message_says_so(self):
        # The duplicate id is skipped; src-b is fine and IS written. A message claiming
        # "nothing was written to this file" would send an operator to `git checkout` and
        # throw away a good seed.
        path = self.write_manifest("""\
            sources:
              - id: "dupe"
                url: "https://example.gov/a"
                format: html
                sha256: ""
              - id: "dupe"
                url: "https://example.gov/a"
                format: html
                sha256: ""
              - id: "src-b"
                url: "https://example.gov/b"
                format: html
                sha256: ""
        """)
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 1)
        self.assertIn(HASH_B, path.read_text(), "src-b's baseline was written")
        self.assertIn("written normally", err)
        self.assertNotIn("NOTHING was written to this file", err)


class LineEndingsSurviveTest(_CorpusFixture):
    def test_a_crlf_manifest_stays_crlf(self):
        # read_text/write_text translate newlines, so a CRLF manifest came back LF
        # THROUGHOUT — a whole-file rewrite, which is what the line-level editor exists to
        # avoid, and invisible to a check that compares parsed YAML.
        path = self.write_manifest("""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
                sha256: ""
        """)
        path.write_bytes(path.read_text().replace("\n", "\r\n").encode())
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, err)
        raw = path.read_bytes()
        self.assertIn(HASH_A.encode(), raw)
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"),
                         "every line ending must still be CRLF")

    def test_an_inserted_key_takes_the_file_s_own_line_ending(self):
        path = self.write_manifest("""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
        """)
        path.write_bytes(path.read_text().replace("\n", "\r\n").encode())
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, err)
        raw = path.read_bytes()
        self.assertIn(b'sha256: "' + HASH_A.encode() + b'"\r\n', raw)


class MalformedBaselineValueTest(_CorpusFixture):
    def test_a_bare_sha256_key_does_not_crash_the_run(self):
        # `sha256:` with no value parses to None, and the CHANGED line's `old[:12]` died
        # mid-crawl — on a manifest shape #68 is precisely about, i.e. in front of the
        # operator running the documented remedy.
        path = self.write_manifest("""\
            sources:
              - id: "src-a"
                url: "https://example.gov/a"
                format: html
                sha256:
        """)
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, err)
        self.assertIn(HASH_A, path.read_text())
        self.assertIn("1 baseline(s) recorded", out)


if __name__ == "__main__":
    unittest.main()
