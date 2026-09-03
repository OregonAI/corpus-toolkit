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

  * since ADR 0015 it writes automatically whenever any in-scope source is unseeded — the
    edit rides the same reviewed PR as the rest of the run's state — and `--record-baseline`
    (bare, or `seed`) is a synonym for that default;
  * seed mode fills EMPTY baselines only — overwriting a recorded one is accepting an
    upstream change without review, and needs `--record-baseline=refresh` to say so;
  * a failed fetch leaves the recorded value byte-for-byte alone;
  * everything else in the file survives — comments, key order, other keys, group names.
"""
import io
import json
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
        self.assertIn("2 baseline(s) written", out)

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

    # `test_recording_opens_no_issues` retired (ADR 0015): `--open-issues` and its
    # combination refusal with `--record-baseline` no longer exist, and no run of
    # `corpus-detect-changes` files anything — there is no seam left here to patch or
    # assert against.

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
        # The tally cannot tell a failed fetch from an unreadable body from an absent
        # `watch` path — all three are "no hash was computed", the same decision — so the
        # line names NO reason. It named two, and the third then read as one of those
        # (corpus-toolkit#72). The per-source line above carries the actual condition.
        self.assertIn("1 skipped (not compared)", out)
        self.assertIn("FETCH FAILED src-b", out)
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
        self.assertIn("1 baseline(s) written", out)


class WatchDoesNotBreakBaselineRecordingTest(_CorpusFixture):
    """corpus-toolkit#72's own adoption path, and the feature broke it.

    `_plan_sha_edits` cleared the current entry on ANY line starting `- `, which was safe
    only because no source key had ever held a block sequence. `watch:` is the first one.
    Written above `sha256:` — the order a human reaches for, since `watch` describes the
    source and `sha256` is machine-filled — the sha line was never associated with its id,
    a second `sha256:` was inserted after `id:`, the re-parse check saw the old trailing
    value win, and the whole file was refused.

    NOTHING was written for that group file, including sources that verified fine. And
    MIGRATION.md prescribes exactly this: "run `corpus-detect-changes
    --record-baseline=refresh` in the same PR". The remedy for the feature broke on the
    feature, and the diagnostic never says the word `watch`.
    """

    WATCH = ["rowsUpdatedAt", "columns[].name"]

    def setUp(self):
        super().setUp()
        self.bodies["https://example.gov/a"] = json.dumps(
            {"rowsUpdatedAt": 1765475245, "viewCount": 13812,
             "columns": [{"name": "agency"}]}).encode()
        self.watched_hash = content_hash(self.bodies["https://example.gov/a"], "json",
                                         watch=self.WATCH)

    def _manifest(self, watch_first: bool):
        entry = ['  - id: "a"', '    url: "https://example.gov/a"', '    format: json']
        watch = ["    watch:", "      - rowsUpdatedAt", "      - columns[].name"]
        sha = ['    sha256: ""']
        body = entry + (watch + sha if watch_first else sha + watch)
        return "sources:\n" + "\n".join(body) + "\n"

    def test_watch_written_above_sha256_still_records(self):
        path = self.write_manifest(self._manifest(watch_first=True))

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("1 baseline(s) written", out,
                      f"a `watch:` block above `sha256:` broke the rewrite:\n{out}{err}")
        self.assertIn(self.watched_hash, path.read_text())
        self.assertEqual(path.read_text().count("sha256:"), 1,
                         "a second sha256 key was inserted")

    def test_watch_written_below_sha256_records_identically(self):
        """The two orders must not differ. They did: one worked, one took the file down."""
        path = self.write_manifest(self._manifest(watch_first=False))

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("1 baseline(s) written", out)
        self.assertIn(self.watched_hash, path.read_text())

    def test_the_shape_oregon_records_retention_actually_ships(self):
        """MEASURED, NOT IMAGINED. `watch` is not the first block sequence to appear under a
        source key — `oregon-records-retention` has carried `references_out:` as one in 76
        of its sources all along, and only key order saved it: `sha256:` is written above
        `references_out:`, so it was already assigned before the reset fired.

        Its entries also sit at column 0 (`- id:`, no leading indent), which the top-level
        branch handles rather than the new indent comparison. Both facts are worth a test,
        because a manifest that reorders those two keys — or adopts `watch` — walks straight
        into the refusal, and this is real curated data, not a fixture."""
        # this class's setUp serves JSON at /a; these two entries are html
        self.bodies["https://example.gov/a"] = BODY_A
        path = self.write_manifest(
            "sources:\n"
            "- id: schedule-agriculture\n"
            "  url: https://example.gov/a\n"
            "  format: html\n"
            "  sha256: ''\n"
            "  references_out:\n  - OAR 166 general schedules\n  - ORS 192\n"
            "- id: schedule-aviation\n"
            "  url: https://example.gov/b\n"
            "  format: html\n"
            "  sha256: ''\n"
            "  references_out:\n  - OAR 166 general schedules\n")

        code, out, err = self.run_cli("--record-baseline")

        text = path.read_text()
        self.assertIn("2 baseline(s) written", out, f"{out}{err}")
        a_block, b_block = text.split("- id: schedule-aviation")
        self.assertIn(HASH_A, a_block)
        self.assertIn(content_hash(BODY_B, "html"), b_block)
        self.assertIn("- ORS 192", text, "the reference list was disturbed")

    def test_sha256_below_a_block_sequence_records_too(self):
        """The latent half of the same bug: with `sha256:` written BELOW `references_out:`
        the sha line was orphaned and the file refused — no `watch` required. Nothing in the
        manifest convention fixes the order of those two keys."""
        # this class's setUp serves JSON at /a; these two entries are html
        self.bodies["https://example.gov/a"] = BODY_A
        path = self.write_manifest(
            "sources:\n"
            "- id: schedule-agriculture\n"
            "  url: https://example.gov/a\n"
            "  format: html\n"
            "  references_out:\n  - OAR 166 general schedules\n"
            "  sha256: ''\n")

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("1 baseline(s) written", out, f"{out}{err}")
        self.assertIn(HASH_A, path.read_text())
        self.assertEqual(path.read_text().count("sha256:"), 1)

    def test_a_nested_sha256_is_not_claimed_as_the_entry_own(self):
        """A REGRESSION INTRODUCED BY THE FIX ABOVE, caught on review.

        Relaxing the `- ` reset so a nested list no longer ends the entry also let the first
        `sha256:`-matching line INSIDE that list be claimed as the entry's own. An entry
        with an `attachments:` block carrying per-attachment digests then had its hash
        written into the attachment, the re-parse check failed, and the whole group file was
        refused — nothing written, including sources that verified. On `main` this seeded
        correctly, so the fix traded one whole-file refusal for another.

        The entry's own keys sit at exactly its key column. A `sha256:` deeper than that
        belongs to something else."""
        self.bodies["https://example.gov/a"] = BODY_A
        path = self.write_manifest(
            "sources:\n"
            '  - id: "a"\n'
            "    url: https://example.gov/a\n"
            "    format: html\n"
            "    attachments:\n"
            "      - url: https://example.gov/appendix.pdf\n"
            '        sha256: "aaaaaaaaaaaa"\n')

        code, out, err = self.run_cli("--record-baseline")

        text = path.read_text()
        self.assertIn("1 baseline(s) written", out, f"{out}{err}")
        self.assertIn("aaaaaaaaaaaa", text, "the attachment's own digest was overwritten")
        self.assertIn(HASH_A, text)
        self.assertLess(text.index(HASH_A), text.index("attachments:"),
                        "the hash was written below the entry's own keys")

    def test_a_sibling_entry_still_ends_the_previous_one(self):
        """The fix must not go the other way. A `- ` at the entry's own indent IS a new
        source, and treating it as nested would write a's hash into b's line — the wrong-
        entry write `_plan_sha_edits`'s docstring refuses by name."""
        self.write_manifest(
            'sources:\n'
            '  - id: "a"\n    url: "https://example.gov/a"\n    format: json\n'
            '    watch:\n      - rowsUpdatedAt\n    sha256: ""\n'
            '  - id: "b"\n    url: "https://example.gov/b"\n    format: html\n'
            '    sha256: ""\n')

        code, out, err = self.run_cli("--record-baseline")

        text = (self.root / "_meta" / "source-manifest.yml").read_text()
        a_hash = content_hash(self.bodies["https://example.gov/a"], "json",
                              watch=["rowsUpdatedAt"])
        a_block, b_block = text.split('- id: "b"')
        self.assertIn(a_hash, a_block)
        self.assertNotIn(a_hash, b_block, "a's hash landed in b's entry")
        self.assertIn(content_hash(BODY_B, "html"), b_block,
                      "b was not recorded — the sibling boundary was lost")


class ShaAboveIdTest(_CorpusFixture):
    """`sha256:` written ABOVE `id:` gets a duplicate key inserted (corpus-toolkit#119).

    `_plan_sha_edits` associates a `sha256:` with its entry by scanning FORWARD from the
    entry's `id:` line, so one written above it is never claimed — `cur` is still None when
    the sha line goes by — and `_rewrite_sha256` concludes the entry has none and inserts a
    fresh one after `id:`.

    BOTH VERIFICATION GUARDS PASS IT, which is why it is silent. PyYAML resolves duplicate
    keys last-wins, so the INSERTED value is what the re-parse check reads back and
    `actual == expected` holds; the line diff sees one added line carrying a value in
    `updates`, which is the shape it is designed to allow. The run reports
    `1 baseline(s) written` and exits 0, leaving a stale `sha256: ""` above a live one in a
    file a human reviews — and on the next run the manifest parses to the new value, so the
    source reads as current and the stale key is never noticed.
    """

    def test_a_sha_above_id_is_claimed_not_duplicated(self):
        path = self.write_manifest(
            'sources:\n'
            '  - sha256: ""\n'
            '    id: "a"\n'
            '    url: https://example.gov/a\n'
            '    format: html\n')

        code, out, err = self.run_cli("--record-baseline")

        text = path.read_text()
        self.assertEqual(text.count("sha256:"), 1,
                         f"a duplicate sha256 key was inserted:\n{text}")
        self.assertIn(HASH_A, text)
        self.assertIn("1 baseline(s) written", out)

    def test_a_nested_sha_above_id_is_still_not_claimed(self):
        """The backward scan needs the same key-column rule the forward one has. Without it
        it claims the FIRST `sha256:` above `id:` whatever its depth — so an entry whose
        `attachments:` list carries per-file digests above its own `id:` has the source's
        hash written into the attachment. That is the wrong-entry write this function
        refuses by name, reached from the other direction."""
        path = self.write_manifest(
            'sources:\n'
            '  - attachments:\n'
            '      - url: https://example.gov/appendix.pdf\n'
            '        sha256: "aaaaaaaaaaaa"\n'
            '    id: "a"\n'
            '    url: https://example.gov/a\n'
            '    format: html\n')

        code, out, err = self.run_cli("--record-baseline")

        text = path.read_text()
        self.assertIn("aaaaaaaaaaaa", text, "the attachment's own digest was overwritten")
        self.assertIn(HASH_A, text)
        self.assertIn("1 baseline(s) written", out, f"{out}{err}")
        self.assertLess(text.index("aaaaaaaaaaaa"), text.index(HASH_A))


class CrossFileDuplicateIdTest(_CorpusFixture):
    """Duplicate ids across two group files sharing a `group:` name (corpus-toolkit#120).

    `occurrences` is built PER FILE while `fetched`/`in_scope` are keyed `(group, id)`, so
    two files declaring the same group collide in the hash map while looking unique to the
    duplicate guard. One entry's hash is then written into the other, silently, exit 0 — the
    wrong-entry write `_plan_sha_edits` refuses by name, arriving one level up where the
    guard does not look.
    """

    manifest = "_meta/sources"

    def _two_files(self):
        for name, n in (("a", 1), ("b", 2)):
            self.write_manifest(
                f'group: shared\nsources:\n  - id: "d"\n'
                f'    url: https://example.gov/{"a" if n == 1 else "b"}\n'
                f'    format: html\n    sha256: ""\n',
                name=f"{name}.yml", subdir="sources")

    def test_neither_file_is_written_and_both_are_named(self):
        self._two_files()

        code, out, err = self.run_cli("--record-baseline")

        a = (self.root / "_meta" / "sources" / "a.yml").read_text()
        b = (self.root / "_meta" / "sources" / "b.yml").read_text()
        self.assertNotIn(HASH_A, a + b, "a hash was written despite an ambiguous id")
        self.assertNotIn(HASH_B, a + b)
        self.assertIn("REFUSED", err, f"{out}{err}")
        self.assertIn("a.yml", err, "the refusal did not name both colliding files")
        self.assertIn("b.yml", err, "the refusal did not name both colliding files")
        self.assertNotEqual(code, 0)

    def test_the_same_id_in_two_DIFFERENT_groups_is_not_a_collision(self):
        """Directory mode defaults `group` to the file stem, so two files may legitimately
        carry the same id under different groups — and they key differently. Refusing those
        would break every corpus using directory mode."""
        for name in ("a", "b"):
            self.write_manifest(
                f'sources:\n  - id: "d"\n'
                f'    url: https://example.gov/{"a" if name == "a" else "b"}\n'
                f'    format: html\n    sha256: ""\n',
                name=f"{name}.yml", subdir="sources")

        code, out, err = self.run_cli("--record-baseline")

        a = (self.root / "_meta" / "sources" / "a.yml").read_text()
        b = (self.root / "_meta" / "sources" / "b.yml").read_text()
        self.assertIn(HASH_A, a)
        self.assertIn(HASH_B, b)
        self.assertIn("2 baseline(s) written", out, f"{out}{err}")


if __name__ == "__main__":
    unittest.main()
