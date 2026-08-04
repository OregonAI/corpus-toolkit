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
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
