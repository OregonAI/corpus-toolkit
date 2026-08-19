"""Tests for drift reporting — that a failed report is LOUD.

corpus-toolkit#53: `_open_issue()` called `subprocess.run` bare and discarded the
returncode. oregon-collective-bargaining's weekly drift run detected 618 changed sources,
attempted 618 issue creations, created ZERO — the repo had no `source-change` label, so
every `gh issue create --label source-change` exited non-zero — and printed
`618 changed, 58 fetch failure(s)`, which reads as "618 were filed".

The bug was never in the detection. It was that detecting and reporting were counted by
the same number, so a reporter doing nothing was indistinguishable from one doing its job.
These tests pin the distinction: the return value must track whether an issue exists, and
the summary must say what was REPORTED, not only what drifted.
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


def _completed(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class OpenIssueReturnsOutcomeTest(unittest.TestCase):
    """_open_issue must report whether an issue actually exists afterwards."""

    def test_returns_false_and_explains_when_gh_create_fails(self):
        # `gh issue list` finds nothing open, then `gh issue create` fails — the exact
        # shape of the 618-failure case, where the label did not exist.
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run", side_effect=[
                 _completed(0, "0"),
                 _completed(1, "", "could not add label: 'source-change' not found"),
             ]):
            err = io.StringIO()
            with redirect_stderr(err):
                ok = changes._open_issue("doc-1", "https://x/1", "aaa", "bbb")
        self.assertFalse(ok, "a failed `gh issue create` must not report success")
        self.assertIn("FAILED to open issue for doc-1", err.getvalue())
        self.assertIn("source-change", err.getvalue(),
                      "the operator needs gh's own reason, not a generic message")

    def test_returns_true_on_success(self):
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run", side_effect=[
                 _completed(0, "0"), _completed(0, "https://github.com/o/r/issues/1"),
             ]):
            self.assertTrue(changes._open_issue("doc-1", "https://x/1", "a", "b"))

    def test_already_open_counts_as_reported(self):
        # An issue that is already open IS reporting — it must not read as a failure and
        # trip the "every creation failed" alarm.
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run",
                               side_effect=[_completed(0, "1")]):
            out = io.StringIO()
            with redirect_stdout(out):
                ok = changes._open_issue("doc-1", "https://x/1", "a", "b")
        self.assertTrue(ok)
        self.assertIn("already open", out.getvalue())

    def test_no_gh_binary_is_not_silent(self):
        with mock.patch.object(changes.shutil, "which", return_value=None):
            err = io.StringIO()
            with redirect_stderr(err):
                ok = changes._open_issue("doc-1", "https://x/1", "a", "b")
        self.assertFalse(ok)
        self.assertIn("gh", err.getvalue())


class EnsureLabelTest(unittest.TestCase):
    """The label is a hard dependency of `--label`, and nothing else creates it."""

    def test_creates_label_with_force(self):
        with mock.patch.object(changes.subprocess, "run",
                               return_value=_completed(0)) as run:
            self.assertTrue(changes._ensure_label())
        args = run.call_args[0][0]
        self.assertEqual(args[:4], ["gh", "label", "create", changes.ISSUE_LABEL])
        self.assertIn("--force", args,
                      "--force makes this idempotent; without it a second run errors")

    def test_warns_but_does_not_raise_when_label_creation_fails(self):
        with mock.patch.object(changes.subprocess, "run",
                               return_value=_completed(1, "", "denied")):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertFalse(changes._ensure_label())
        self.assertIn("could not ensure", err.getvalue())


class CapTest(unittest.TestCase):
    def test_cap_is_small_enough_to_surface_a_broken_baseline(self):
        # The failure this guards against is a manifest of 680 sources with `sha256: ''`,
        # where every source drifts every run. The cap only helps if it is far below that.
        self.assertLessEqual(changes.MAX_ISSUES_PER_RUN, 50)
        self.assertGreaterEqual(changes.MAX_ISSUES_PER_RUN, 5)


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
source_manifest_path: _meta/sources
"""


def _body(i: int) -> bytes:
    return b"<html><body><p>" + f"Rule text for source {i}. ".encode() * 20 + b"</p></body></html>"


class _DriftRun(unittest.TestCase):
    """Drives main() over a real manifest on disk with only the network faked."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta" / "sources").mkdir(parents=True)
        (self.root / "documents").mkdir()
        (self.root / "_meta" / "corpus.yml").write_text(CORPUS_YML)
        self.bodies = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def group(self, name: str, n: int, *, baseline: str | None, start: int = 0):
        """Write a group of n sources. baseline=None means `sha256: \'\'` (unseeded),
        "current" means the hash they will actually fetch, anything else is a literal."""
        lines = ["sources:"]
        for i in range(start, start + n):
            url = f"https://example.gov/{name}/{i}"
            self.bodies[url] = _body(i)
            if baseline is None:
                sha = ""
            elif baseline == "current":
                sha = content_hash(_body(i), "html")
            else:
                sha = baseline
            lines.append(textwrap.dedent(f"""\
                  - id: "{name}-{i}"
                    url: "{url}"
                    format: html
                    sha256: "{sha}"
            """).rstrip())
        (self.root / "_meta" / "sources" / f"{name}.yml").write_text("\n".join(lines) + "\n")

    def run_cli(self, *argv):
        args = ["corpus-detect-changes", "--config",
                str(self.root / "_meta" / "corpus.yml"), *argv]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", lambda url: self.bodies[url]), \
             mock.patch.object(changes, "_ensure_label", return_value=True), \
             mock.patch.object(changes, "_open_issue", return_value=True) as open_issue, \
             redirect_stdout(out), redirect_stderr(err):
            self.open_issue = open_issue
            try:
                changes.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()


class GroupBreakdownTest(_DriftRun):
    """corpus-toolkit#67 item 1: the one line that identifies a bulk false positive.

    ERF\'s capped run was 484/484 in `oar` and 52/52 in a DEQ group — two template-level
    faults — while three other groups carried the five genuine changes. The totals line
    could not distinguish that from 544 real revisions, and nobody could without opening
    every issue.
    """

    def test_breakdown_appears_on_an_uncapped_run_too(self):
        self.group("oar", 4, baseline="stale")
        self.group("oam", 3, baseline="current")
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("oar 4/4", out)
        self.assertIn("oam 0/3", out)

    def test_group_names_are_the_ones_the_group_flag_accepts(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 2, baseline="stale")
        _, out, _ = self.run_cli("--group", "oar")
        self.assertIn("oar 2/2", out)
        self.assertNotIn("oam", out, "an out-of-scope group is not reported as 0 drift")

    def test_unseeded_sources_are_marked_in_the_breakdown(self):
        # The two shapes that both read as 100% drift: a stale/altered baseline (#66) and
        # no baseline at all (#68). One character of the breakdown separates them.
        self.group("oar", 3, baseline="stale")
        self.group("counties", 2, baseline=None)
        _, out, _ = self.run_cli()
        self.assertIn("counties 2/2", out)
        self.assertIn("unseeded", out)


class CappedRunIsNotACleanRunTest(_DriftRun):
    """corpus-toolkit#67 item 3: the delivery channel was the broken part.

    Both observed occurrences — ERF (519 dropped) and oregon-counties (3,366 dropped) —
    concluded `success` with the truncation notice on stderr near the end of a
    multi-thousand-line log. A correct diagnosis and an incorrect one produced the same
    outcome, because in neither case did anyone read it.
    """

    def test_a_capped_run_exits_non_zero(self):
        self.group("oar", changes.MAX_ISSUES_PER_RUN + 5, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1, "a truncated run must be distinguishable from a clean one")
        self.assertIn("STOPPED after", err)

    def test_an_uncapped_run_with_drift_still_exits_zero(self):
        # Unchanged behaviour: a changed source is a signal, not an error.
        self.group("oar", 3, baseline="stale")
        code, _, _ = self.run_cli("--open-issues")
        self.assertEqual(code, 0)

    def test_capped_message_does_not_assert_an_empty_baseline_when_there_is_none(self):
        # ERF had ZERO empty baselines and was told to go check the manifest for them.
        self.group("oar", changes.MAX_ISSUES_PER_RUN + 5, baseline="stale")
        _, _, err = self.run_cli("--open-issues")
        self.assertIn("0 of", err)
        self.assertIn("not the cause", err.lower())
        self.assertNotIn("usually means the manifest baseline is empty", err)

    def test_capped_message_reports_the_measured_unseeded_count_when_there_is_one(self):
        n = changes.MAX_ISSUES_PER_RUN + 5
        self.group("counties", n, baseline=None)
        _, _, err = self.run_cli("--open-issues")
        self.assertIn(f"{n} of {n}", err)
        self.assertIn("--record-baseline", err)


class UnseededManifestIsNotDriftTest(_DriftRun):
    """corpus-toolkit#68: a run with no baseline cannot detect drift, and must say so."""

    def test_every_run_reports_the_measured_unseeded_count(self):
        self.group("oar", 2, baseline="current")
        self.group("counties", 3, baseline=None)
        _, out, _ = self.run_cli()
        self.assertIn("3 with no recorded baseline", out)

    def test_wholly_unseeded_run_files_nothing_and_names_the_seeding_mode(self):
        self.group("counties", 4, baseline=None)
        code, out, err = self.run_cli("--open-issues")
        self.open_issue.assert_not_called()
        self.assertEqual(code, 1, "detection is inert here; the run must not report success")
        self.assertIn("--record-baseline", err)

    def test_a_partly_seeded_manifest_still_files(self):
        # The refusal is for the wholly-unseeded case only — one unseeded source among
        # real ones must not switch reporting off for the corpus.
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self.open_issue.call_count, 3)
        self.assertEqual(code, 0)


class NothingCheckedIsNotCleanTest(_DriftRun):
    def test_an_empty_group_scope_does_not_exit_zero(self):
        # `--group` takes a free-text name and nothing validates it against the manifest,
        # so a typo checked nothing and reported "0 changed ... of 0 checked", exit 0 —
        # could-not-check served as not-there, inside the run that exists to prevent it.
        self.group("oar", 2, baseline="current")
        code, out, err = self.run_cli("--group", "nosuchgroup", "--open-issues")
        self.assertEqual(code, 1)
        self.assertIn("NOTHING WAS CHECKED", err)
        self.assertIn("nosuchgroup", err)


class InertRunOutputsTest(_DriftRun):
    def test_inert_run_does_not_set_changed_true_for_the_workflow(self):
        # `changed=true` fires whatever the calling workflow does next. On an inert run
        # every source "changed" against an empty baseline, which is not a finding.
        self.group("counties", 3, baseline=None)
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        text = gh.read_text()
        self.assertIn("changed=false", text)
        self.assertIn("unseeded=3", text)

    def test_a_real_drift_run_still_sets_changed_true(self):
        self.group("oar", 1, baseline="stale")
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        self.assertIn("changed=true", gh.read_text())


if __name__ == "__main__":
    unittest.main()
