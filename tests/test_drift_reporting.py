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
import json
import shutil
import sys
import tempfile
import textwrap
import unittest

import pytest
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
        # Ids whose `gh issue create` will fail. Default empty: filings succeed. The #53
        # shape is a filing that is ATTEMPTED and does not exist afterwards, and no test
        # could express it while the mock returned True unconditionally.
        self.failing_ids: set[str] = set()
        # Same idea for the group findings of ADR 0010: a finding that was ATTEMPTED and
        # does not exist afterwards is a different outcome from one that was never due.
        self.failing_findings: set[str] = set()
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta" / "sources").mkdir(parents=True)
        (self.root / "documents").mkdir()
        (self.root / "_meta" / "corpus.yml").write_text(CORPUS_YML)
        self.bodies = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def group(self, name: str, n: int, *, baseline: str | None, start: int = 0,
              file_stem: str | None = None):
        """Write a group of n sources. baseline=None means `sha256: \'\'` (unseeded),
        "current" means the hash they will actually fetch, anything else is a literal.

        `file_stem` writes the group to a differently-named file and declares `group:`
        inside it, which is what a real manifest does when the file name and the group
        name disagree. That is the only way the manifest's iteration order (file name)
        can differ from the group name, so it is the only way to observe a tiebreak that
        sorts on the name."""
        lines = ["sources:"] if file_stem is None else [f"group: {name}", "sources:"]
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
        stem = file_stem or name
        (self.root / "_meta" / "sources" / f"{stem}.yml").write_text("\n".join(lines) + "\n")

    def _fetch(self, url):
        # RAISES rather than returning the exception. Returning it let a "failed" fetch
        # reach `len(raw)` and be counted as a normalizable source before dying, so a test
        # about blocked fetches was silently exercising a shape `fetch()` cannot produce.
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
             mock.patch.object(changes, "_ensure_label", return_value=True), \
             mock.patch.object(changes, "_open_issue",
                               side_effect=lambda sid, *a: sid not in self.failing_ids
                               ) as open_issue, \
             mock.patch.object(changes, "_open_group_finding",
                               side_effect=lambda g, *a, **k: g not in self.failing_findings
                               ) as group_finding, \
             redirect_stdout(out), redirect_stderr(err):
            self.open_issue = open_issue
            self.group_finding = group_finding
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


class BudgetIsSpentSmallestGroupFirstTest(_DriftRun):
    """corpus-toolkit#69: the cap decides WHICH issues are filed, and it used to decide by
    manifest iteration order alone.

    ERF run 31022774644: 544 changed, 25 opened, 519 dropped. The 52-source DEQ group came
    first alphabetically and consumed the whole budget, so the five genuine changes in three
    small agency groups — and the 484-source `oar` template change that was 89% of the drift
    — got no ticket at all. Nothing about the budget was allocated; it was simply spent by
    whoever the loop reached first.
    """

    def _filed(self):
        return [c.args[0] for c in self.open_issue.call_args_list]

    def test_a_noisy_group_no_longer_shuts_out_the_small_ones(self):
        # Alphabetically first AND far over the cap on its own: the ERF shape.
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("wrd", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        filed = self._filed()
        # All three groups drift wholly, so each also files a group drift finding out of
        # the same budget (ADR 0010). The cap still bounds the run; what it buys changed.
        self.assertEqual(self.group_finding.call_count, 3)
        self.assertEqual(len(filed), changes.MAX_ISSUES_PER_RUN - 3,
                         "the cap is not weakened — it still bounds the run")
        self.assertEqual({"oam-0", "oam-1"}, {s for s in filed if s.startswith("oam-")},
                         "every source in the smallest drifting group must be filed")
        self.assertEqual({"wrd-0", "wrd-1", "wrd-2"},
                         {s for s in filed if s.startswith("wrd-")},
                         "a small genuine finding must not be starved by a bulk one")
        self.assertEqual(code, 1, "a capped run is still not a clean run")


    def test_equal_sized_groups_are_ordered_by_name_not_by_file_position(self):
        # Two groups drifting equally, written to files whose names sort the OPPOSITE way
        # to the group names — the manifest's own iteration order. Without a tiebreak on
        # something belonging to the group, the run files whichever file the loader
        # happened to open first, and moving a group between files silently changes which
        # sources get reported.
        # Just over half the cap each, so the second group cannot fit whole and which
        # group is second is the whole question.
        n = changes.MAX_ISSUES_PER_RUN // 2 + 1
        self.group("zebra", n, baseline="stale", file_stem="a-first")
        self.group("alpha", n, baseline="stale", file_stem="b-second")
        self.run_cli("--open-issues")
        filed = self._filed()
        # Both groups drift wholly, so two of the slots go to group findings (ADR 0010).
        tickets = changes.MAX_ISSUES_PER_RUN - 2
        self.assertEqual(self.group_finding.call_count, 2)
        self.assertEqual(len(filed), tickets)
        self.assertEqual(len([s for s in filed if s.startswith("alpha-")]), n,
                         "the tie must be broken by the group name, so `alpha` files whole")
        self.assertEqual([s for s in filed if s.startswith("zebra-")],
                         [f"zebra-{i}" for i in range(tickets - n)],
                         "within a group the manifest's own order must survive the sort")


    def test_a_capped_run_says_the_budget_was_allocated_and_how(self):
        # A group at 484/484 in the breakdown with no issue against it now means "the
        # budget went to smaller groups first", not "reporting failed" (corpus-toolkit#53
        # is the reason that distinction has to be printed rather than inferred).
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("oar", 30, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1)
        self.assertIn("smallest-group-first", err.lower())
        self.assertIn("oam (2/2 of 2)", err)
        # Three whole-group findings and `oam`'s two tickets come out of the budget first.
        oar_tickets = changes.MAX_ISSUES_PER_RUN - 3 - 2
        self.assertIn(f"oar ({oar_tickets}/{oar_tickets} of 30)", err)
        self.assertIn("deq (0/0 of 40)", err)
        self.assertIn("not reached by the budget", err)

    def test_the_starved_group_line_does_not_fire_when_every_group_got_a_ticket(self):
        # Capped, but the budget reached all three groups (deq takes the remaining 20 of
        # its 40). A line that says "not reported at all" on a run where every drifting
        # group WAS reported would send an operator looking for a group that is not there.
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("wrd", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1)
        # Three whole-group findings, then oam's 2 and wrd's 3 tickets, then the rest.
        deq_tickets = changes.MAX_ISSUES_PER_RUN - 3 - 5
        self.assertIn(f"deq ({deq_tickets}/{deq_tickets} of 40)", err,
                      "the run is capped and the line is printed")
        self.assertNotIn("not reached by the budget", err)

    def test_a_group_whose_every_filing_failed_is_not_reported_as_reported(self):
        # The allocation line is the one an operator reads on a capped run, so it must not
        # repeat the corpus-toolkit#53 confusion the summary line was fixed to remove:
        # attempting two filings and creating zero is not "reported". The global
        # every-creation-failed alarm cannot catch this — `opened` is non-zero overall
        # because the big group's filings succeeded.
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.failing_ids = {"oam-0", "oam-1"}
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1)
        self.assertIn("oam (0/2 of 2)", err,
                      "opened, attempted and changed are three different numbers")
        self.assertIn("EVERY issue creation failed", err)
        self.assertIn("oam", err.split("EVERY issue creation failed")[1][:200])
        self.assertNotIn("not reached by the budget", err,
                         "oam WAS reached — the cap is not what went wrong here")

    def test_an_uncapped_run_does_not_print_the_allocation_line(self):
        # Nothing was allocated: every drifting source was filed. Printing the policy
        # anyway would train an operator to skim past it on the run where it matters.
        self.group("oam", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 0)
        self.assertNotIn("smallest-group-first", (err + out).lower())


class ChangedSourcesTsvIsPublicSurfaceTest(_DriftRun):
    """`changed-sources.tsv` is read by corpus repos, and no test read it — so the four
    columns and their order were a claim in a comment. #69 reshaped the record behind this
    writer (a group name rides along now), which is exactly the change that would rewrite
    the file by accident."""

    def test_four_columns_id_url_old_new_in_manifest_order(self):
        # `aaa` drifts more than `zzz`, so the two orders DISAGREE: the manifest yields
        # aaa first, the issue spend takes zzz first. A fixture where they coincide cannot
        # tell which one the writer used.
        self.group("aaa", 3, baseline="stale")
        self.group("zzz", 1, baseline="stale")
        self.run_cli("--open-issues")
        rows = [ln.split("\t") for ln in
                (self.root / "changed-sources.tsv").read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["aaa-0", "aaa-1", "aaa-2", "zzz-0"],
                         "manifest order, NOT the order issues were filed in")
        self.assertEqual([c.args[0] for c in self.open_issue.call_args_list][0], "zzz-0",
                         "and the spend order really is the other one")
        self.assertTrue(all(len(r) == 4 for r in rows), rows)
        self.assertEqual(rows[0][1], "https://example.gov/aaa/0")
        self.assertEqual(rows[0][2], "stale")
        self.assertEqual(rows[0][3], content_hash(_body(0), "html"))


class SourceOutcomesArtifactTest(_DriftRun):
    """corpus-toolkit#160: `changed-sources.tsv` lists only what changed, so a source that
    was fetched and held still, a source whose fetch failed, and a source never in this
    run's scope are byte-identical in that file — none of them appears. `source-outcomes.json`
    is the companion artifact that records, per source, WHAT HAPPENED, plus the run-level
    facts (scope, per-group breakdown, totals) that otherwise exist only on stdout.

    THE RED PROOF the issue demands: a run where every fetch failed must be distinguishable,
    by reading the artifact alone, from a run where nothing changed. Both write an EMPTY
    `changed-sources.tsv` today — that collapse is the bug this file exists to fix.
    """

    def _outcomes(self):
        return json.loads((self.root / "source-outcomes.json").read_text())

    def test_a_wholly_failed_run_is_distinguishable_from_a_no_drift_run(self):
        # Scenario A: every fetch in the group fails.
        self.group("oar", 3, baseline="stale")
        for i in range(3):
            self.bodies[f"https://example.gov/oar/{i}"] = OSError("HTTP Error 403")
        self.run_cli()
        failed_tsv = (self.root / "changed-sources.tsv").read_bytes()
        failed_report = self._outcomes()

        # Scenario B: a fresh corpus where every fetch succeeds and nothing changed.
        other_root = Path(tempfile.mkdtemp())
        (other_root / "_meta" / "sources").mkdir(parents=True)
        (other_root / "documents").mkdir()
        (other_root / "_meta" / "corpus.yml").write_text(CORPUS_YML)
        for i in range(3):
            url = f"https://example.gov/oar/{i}"
            self.bodies[url] = _body(i)
        (other_root / "_meta" / "sources" / "oar.yml").write_text(
            "\n".join(["sources:"] + [textwrap.dedent(f"""\
                  - id: "oar-{i}"
                    url: "https://example.gov/oar/{i}"
                    format: html
                    sha256: "{content_hash(_body(i), 'html')}"
            """).rstrip() for i in range(3)]) + "\n")
        args = ["corpus-detect-changes", "--config", str(other_root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                changes.main()
            except SystemExit:
                pass
        clean_tsv = (other_root / "changed-sources.tsv").read_bytes()
        clean_report = json.loads((other_root / "source-outcomes.json").read_text())
        shutil.rmtree(other_root, ignore_errors=True)

        # The existing artifact collapses the two runs today — proving the bug is real,
        # not merely asserting the fix.
        self.assertEqual(failed_tsv, b"", "sanity: the failed run's tsv is empty")
        self.assertEqual(clean_tsv, b"", "sanity: the clean run's tsv is empty")
        self.assertEqual(failed_tsv, clean_tsv,
                         "sanity: changed-sources.tsv alone cannot tell these apart")

        # The companion artifact must not repeat that collapse.
        self.assertNotEqual(failed_report, clean_report,
                            "a wholly-failed run and a no-drift run produced "
                            "indistinguishable source-outcomes.json artifacts")
        self.assertEqual(failed_report["totals"]["fetch_failed"], 3)
        self.assertEqual(failed_report["totals"]["unchanged"], 0)
        self.assertEqual(clean_report["totals"]["fetch_failed"], 0)
        self.assertEqual(clean_report["totals"]["unchanged"], 3)


class ChangedSourcesTsvByteIdenticalGuaranteeTest(_DriftRun):
    """The out-of-scope acceptance criterion, proven rather than reasoned about: for the
    same inputs, `changed-sources.tsv` is byte-identical to what it was BEFORE this
    artifact existed. `tests/fixtures/changed-sources-golden.tsv` was captured by running
    this exact scenario against the pre-#160 code, on this branch, before a single line of
    `source-outcomes.json` support was written — a snapshot, not a value re-derived from
    the current implementation, so a change to the writer would actually be caught here."""

    def test_tsv_bytes_are_unchanged_by_the_new_artifact(self):
        self.group("aaa", 3, baseline="stale")
        self.group("zzz", 1, baseline="stale")
        self.group("bbb", 2, baseline="current")
        self.group("ccc", 2, baseline=None)
        self.group("ddd", 1, baseline="stale")
        self.bodies["https://example.gov/ddd/0"] = Exception("boom")
        self.run_cli("--open-issues")
        golden = (Path(__file__).parent / "fixtures" / "changed-sources-golden.tsv").read_bytes()
        actual = (self.root / "changed-sources.tsv").read_bytes()
        self.assertEqual(actual, golden)


class SourceOutcomesVocabularyTest(_DriftRun):
    """The outcome vocabulary itself: each branch the fetch loop already takes must land
    on its own, distinct outcome string — collapsing any two recreates the bug one level
    in (the issue's own words)."""

    def _by_id(self, report, sid):
        return next(s for s in report["sources"] if s["id"] == sid)

    def test_no_baseline_is_reported_as_such_not_changed_nor_unchanged(self):
        # Fetch succeeds; the manifest recorded no sha256 at all.
        self.group("counties", 1, baseline=None)
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        entry = self._by_id(report, "counties-0")
        self.assertEqual(entry["outcome"], "no_baseline")
        self.assertFalse(entry["had_baseline"])
        # And it must NOT also appear as changed or unchanged.
        self.assertNotIn(entry["outcome"], ("changed", "unchanged"))

    def test_unreadable_json_and_watch_path_missing_are_distinct_outcomes(self):
        # A watch-declared json source, hand-written so both watch exceptions are reachable.
        (self.root / "_meta" / "sources" / "ds.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "ds-bad-json"
                url: "https://example.gov/ds/bad"
                format: json
                watch: ["rowsUpdatedAt"]
                sha256: "stale"
              - id: "ds-missing-path"
                url: "https://example.gov/ds/missing"
                format: json
                watch: ["rowsUpdatedAt"]
                sha256: "stale"
        """))
        self.bodies["https://example.gov/ds/bad"] = b"<html>not json</html>"
        self.bodies["https://example.gov/ds/missing"] = json.dumps({"other": 1}).encode()
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(self._by_id(report, "ds-bad-json")["outcome"], "unreadable_json")
        self.assertEqual(self._by_id(report, "ds-missing-path")["outcome"],
                         "watch_path_missing")

    def test_fetch_failed_changed_and_unchanged_are_reported(self):
        self.group("oar", 1, baseline="stale")     # will change
        self.group("oam", 1, baseline="current")    # will not change
        self.group("deq", 1, baseline="stale")
        self.bodies["https://example.gov/deq/0"] = OSError("HTTP Error 403")
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(self._by_id(report, "oar-0")["outcome"], "changed")
        self.assertEqual(self._by_id(report, "oam-0")["outcome"], "unchanged")
        self.assertEqual(self._by_id(report, "deq-0")["outcome"], "fetch_failed")


class SourceOutcomesRunLevelFactsTest(_DriftRun):
    """The run-level facts the issue says exist only on stdout today: in-scope groups, the
    per-group breakdown, and totals. `source-outcomes.json` must carry all three, and the
    per-group breakdown must be the SAME numbers `_print_group_breakdown` prints — not a
    second tally that could disagree with it."""

    def test_groups_in_scope_and_breakdown_match_what_a_full_run_touches(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 1, baseline="current")
        _, out, _ = self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(sorted(report["groups_in_scope"]), ["oam", "oar"])
        self.assertEqual(report["groups"]["oar"]["changed"], 2)
        self.assertEqual(report["groups"]["oar"]["total"], 2)
        self.assertEqual(report["groups"]["oam"]["changed"], 0)
        self.assertIn("oar 2/2", out, "the artifact's own numbers must match the log's")

    def test_totals_are_readable_without_parsing_the_log(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 1, baseline="current")
        self.group("counties", 1, baseline=None)
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        t = report["totals"]
        self.assertEqual(t["total"], 4)
        self.assertEqual(t["changed"], 2)
        self.assertEqual(t["unchanged"], 1)
        self.assertEqual(t["no_baseline"], 1)
        self.assertEqual(t["fetch_failed"], 0)

    def test_group_filter_narrows_scope_and_the_excluded_group_is_simply_absent(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.run_cli("--group", "oar")
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(report["group_filter"], ["oar"])
        self.assertEqual(report["groups_in_scope"], ["oar"])
        self.assertNotIn("oam", report["groups"])
        self.assertFalse(any(s["group"] == "oam" for s in report["sources"]),
                         "a group filtered out by --group must not appear as any outcome, "
                         "including as if it were unchanged")

    def test_a_wholly_failed_run_still_writes_the_artifact(self):
        # The run this ticket exists for: every fetch fails, and the artifact must still
        # exist and describe the run rather than being skipped as "nothing to report".
        self.group("oar", 3, baseline="stale")
        for i in range(3):
            self.bodies[f"https://example.gov/oar/{i}"] = OSError("HTTP Error 403")
        self.run_cli()
        self.assertTrue((self.root / "source-outcomes.json").exists())
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(report["totals"]["fetch_failed"], 3)
        self.assertEqual(report["totals"]["total"], 3)


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
        # Including the group drift finding of ADR 0010, whose founding case this is:
        # oregon-counties reported 3,447 of 3,447 changed with every baseline empty.
        self.group_finding.assert_not_called()
        self.assertEqual(code, 1, "detection is inert here; the run must not report success")
        self.assertIn("--record-baseline", err)

    def test_a_partly_seeded_manifest_still_files(self):
        # The refusal is for the wholly-unseeded case only — one unseeded source among
        # real ones must not switch reporting off for the corpus.
        #
        # TWO tickets since corpus-toolkit#145, not three: the two genuinely drifted
        # sources file and the unseeded one does not, because it was never compared to
        # anything. The count moved; the intent above did not, and the last two assertions
        # are what keep it true — the corpus still reports on `counties-0`, through the
        # channel that names it and its remedy rather than through a drift ticket that
        # claims a comparison never made.
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual([c.args[0] for c in self.open_issue.call_args_list],
                         ["oar-0", "oar-1"])
        self.assertIn("counties-0", err,
                      "reporting was switched off for the unseeded source entirely")
        self.assertIn("--record-baseline", err)
        self.assertEqual(code, 1,
                         "the run reported success with one of its sources unchecked")


class AnUnseededSourceIsNotAChangedSourceTest(_DriftRun):
    """corpus-toolkit#145: the per-source half of ADR 0010's rule.

    "An uncompared source is not a changed source" was enforced for the group drift
    finding and not for the individual tickets. A source with `sha256: ''` compares
    unequal to everything, so it filed `Source changed: <id>` with an EMPTY previous hash
    every run — could-not-check reported as a finding — and entered the issue budget
    alongside genuine drift. The wholly-unseeded run was refused by the `inert` guard, but
    that guard is all-or-nothing: one seeded source anywhere switched it off for the whole
    corpus.
    """

    def _tickets(self):
        return [c.args[0] for c in self.open_issue.call_args_list]

    def test_an_unseeded_source_files_no_source_changed_ticket(self):
        # The issue's own reproduction: one unseeded group beside a genuinely drifted one.
        # `counties-0` was never compared to anything, so a ticket claiming upstream drift
        # for it is a report about a check that did not happen.
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._tickets(), ["oar-0", "oar-1"],
                         "a source with no recorded baseline filed a drift ticket")

    def test_a_partly_seeded_run_names_the_unseeded_sources_and_the_remedy(self):
        """The half that is not optional. Filing nothing for `counties-0` and saying
        nothing about it trades "could not check reported as drift" for "could not check
        reported as absent" — the same rule of CONTEXT.md broken the other way round, and
        the reason the one-line filter is the wrong fix on its own.

        IDS, not only a count: `1 of 3 have no recorded baseline` does not tell an
        operator which manifest entry to seed, and the tickets — wrong as they were —
        used to. The remedy is named on the same line for the same reason."""
        self.group("counties", 2, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("counties-0", err)
        self.assertIn("counties-1", err)
        self.assertIn("--record-baseline", err)

    def test_an_unseeded_source_spends_no_slot_from_the_issue_cap(self):
        """`MAX_ISSUES_PER_RUN` is the budget for reporting drift, and a source that was
        never compared has no drift to report. Thirty unseeded sources used to fill it and
        truncate a run that had three real findings and room for all of them."""
        self.group("counties", changes.MAX_ISSUES_PER_RUN + 5, baseline=None)
        self.group("oar", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._tickets(), ["oar-0", "oar-1", "oar-2"])
        self.assertNotIn("STOPPED after", err,
                         "sources that were never compared truncated the run")

    def test_genuine_drift_an_unseeded_group_pushed_out_is_now_filed(self):
        """The issue's measured case, at cap scale. A corpus mid-seeding — the state
        corpus-toolkit#68 left corpora in — put its unseeded group AHEAD of the genuinely
        drifted one in the smallest-group-first spend order of corpus-toolkit#69, and the
        real drift got nothing at all."""
        self.group("counties", changes.MAX_ISSUES_PER_RUN - 1, baseline=None)
        self.group("oar", changes.MAX_ISSUES_PER_RUN + 1, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertTrue(self._tickets(), "the run filed nothing at all")
        self.assertTrue(all(t.startswith("oar-") for t in self._tickets()),
                        f"the budget bought tickets for sources never compared: "
                        f"{self._tickets()}")

    def test_a_run_whose_only_finding_is_unseeded_sources_does_not_report_success(self):
        """The shape that exists only because of this fix. Two seeded sources holding
        still and one that was never compared: nothing is filed, and before the exit status
        carried it this run printed a clean summary and a green check while one of its
        three sources had not been checked at all — could-not-check served as
        nothing-to-report, which CONTEXT.md says outranks the rest of the vocabulary."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="current")
        code, out, err = self.run_cli("--open-issues")
        self.open_issue.assert_not_called()
        self.assertEqual(code, 1, "a run that checked nothing about counties-0 was green")
        self.assertIn("counties-0", err)
        self.assertIn("--record-baseline", err)

    def test_real_drift_alongside_an_unseeded_source_does_not_report_success_either(self):
        """DELIBERATELY NOT NARROWED to "unseeded was the only finding". Whether a source
        was compared is a fact about that source, and gating the signal on whether some
        OTHER source happened to drift makes it appear and disappear for unrelated
        reasons — green this week because nothing else moved, red next week because
        something did, with `counties-0` unchecked throughout. Same reasoning as a missing
        watched path (corpus-toolkit#72), which exits non-zero regardless of `--strict`:
        the bytes arrived, the comparison did not happen, and it will not happen on any
        future run either until somebody seeds it."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1)

    def test_seeding_is_not_itself_the_unchecked_condition(self):
        """`--record-baseline` is the remedy the line above names. A run that just applied
        it must not be red for the thing it fixed, or the remedy never reports done."""
        self.group("counties", 2, baseline=None)
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, f"the seeding run reported failure:\n{err}")

    def test_an_unseeded_source_does_not_set_changed_true_for_the_workflow(self):
        """`changed=true` fires whatever the calling workflow does next. An unseeded
        source is not drift, so it must not fire it — the same reasoning the inert run
        already applies, one level down."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="current")
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        self.assertIn("changed=false", gh.read_text())
        self.assertIn("unseeded=1", gh.read_text())

    def test_the_wholly_unseeded_refusal_and_its_annotation_are_unchanged(self):
        """`inert` stays a separate concept. The ticket filter now suppresses everything
        the predicate used to suppress here, but it is not the same statement: the filter
        is a fact about one source, `inert` is a diagnosis of the RUN — "this detected
        seeding, not drift" — and it is what backs this refusal, its annotation and
        `changed=false` on a corpus nobody has ever seeded.

        A PRESERVATION GUARD. It passes on `main` too; that is what "unchanged" means, and
        it is here so the fix cannot quietly delete the whole-corpus report on its way to
        deleting the per-source ticket."""
        self.group("counties", 4, baseline=None)
        with mock.patch.dict(changes.os.environ, {"GITHUB_ACTIONS": "true"}):
            code, out, err = self.run_cli("--open-issues")
        self.assertIn("REFUSING to open issues: all 4 in-scope source(s) have no "
                      "recorded baseline", err)
        self.assertIn("::warning title=Drift detection is inert::", out)
        self.assertEqual(code, 1)
        # UNCHANGED means nothing added either. The per-source naming below is the
        # partly-seeded run's report; here the refusal already speaks for the whole
        # corpus, and listing 4 of 4 ids — 3,447 of 3,447 in the founding case — adds
        # length, a second annotation and no information.
        self.assertIn("4 of 4 in-scope source(s) have NO recorded baseline. A source with "
                      "`sha256: ''` can never compare equal, so it reports CHANGED every "
                      "run and its drift means nothing. Seed them with", err)
        self.assertNotIn("counties-0", err)
        self.assertEqual(out.count("::warning title="), 1,
                         f"the wholly-unseeded run gained a second annotation:\n{out}")

    def test_the_run_does_not_count_unseeded_sources_as_tickets_it_failed_to_file(self):
        """`2 opened, 0 failed, of 3 changed source(s)` invites the subtraction that says
        one filing went missing. The denominator has to be what the run was entitled to
        file, or the summary reinstates the corpus-toolkit#53 confusion it exists to
        prevent — this time by understating."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("of 2 changed source(s) with a recorded baseline", out)

    def test_a_capped_run_does_not_count_unseeded_sources_among_what_it_dropped(self):
        """`dropped = changed - attempted` counted sources the run declined to file for as
        sources the CAP lost, which inflates the one number an operator uses to judge how
        much of the report is missing — and points them at the cap for a condition the cap
        had nothing to do with."""
        n = changes.MAX_ISSUES_PER_RUN + 5
        self.group("counties", 5, baseline=None)
        self.group("oar", n, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("STOPPED after", err)
        # 30 drifted with a baseline, 24 filed (the group finding took the 25th slot).
        self.assertIn("6 changed source(s) were not reported", err)
        self.assertIn("not the cause", err.lower(),
                      "the capped run blamed the unseeded sources for a cap they no "
                      "longer spend a slot of")

    def test_the_allocation_line_counts_a_group_by_what_it_could_file(self):
        """`mixed (24/24 of 35)` invites the reader to subtract eleven tickets the budget
        supposedly lost, when six were dropped by the cap and five were never candidates.
        This block exists to tell a cap apart from the silent-reporting failure of
        corpus-toolkit#53; a denominator that counts uncompared sources puts them back
        together."""
        self.group("mixed", changes.MAX_ISSUES_PER_RUN + 5, baseline="stale",
                   file_stem="mixed_a")
        self.group("mixed", 5, baseline=None, start=changes.MAX_ISSUES_PER_RUN + 5,
                   file_stem="mixed_b")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("STOPPED after", err)
        self.assertIn("mixed (24/24 of 30)", err)

    def test_the_tsv_still_carries_the_unseeded_source_with_an_empty_old_column(self):
        """The ticket is what goes away, not the record. `changed-sources.tsv` is read by
        corpus repos and its four columns are public surface (corpus-toolkit#53), and an
        empty `old` column is self-describing where a ticket body was not. A preservation
        guard — dropping the row would be the over-suppression this fix must not become."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 1, baseline="stale")
        self.run_cli("--open-issues")
        rows = [r.split("\t") for r in
                (self.root / "changed-sources.tsv").read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["counties-0", "oar-0"])
        self.assertEqual(rows[0][2], "", "the unseeded row lost its empty `old` column")

    def test_more_unseeded_sources_than_the_line_can_name_still_reach_the_operator(self):
        """The issue's own case is a 152-source unseeded group, and this report is now the
        only channel those sources have. Truncating at twenty matches every other listing
        in this file, so the line has to say how many it did not name and where the rest
        are — `changed-sources.tsv` carries every one of them, with the empty previous-hash
        column that says what happened."""
        self.group("counties", 25, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("counties-19", err)
        self.assertIn("first 20 of 25", err)
        self.assertIn("changed-sources.tsv", err)

    def test_the_unseeded_report_reaches_ci_where_stderr_does_not(self):
        """A notice on stderr sat unread near line 3,870 of a 3,900-line log while drift
        detection was inert for a week (corpus-toolkit#67), which is why `_annotate`
        exists. Removing the tickets moves this finding onto exactly that surface."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        with mock.patch.dict(changes.os.environ, {"GITHUB_ACTIONS": "true"}):
            code, out, err = self.run_cli("--open-issues")
        self.assertIn("::warning title=", out)
        self.assertIn("counties-0", out.split("::warning title=", 1)[1])


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




class WatchPathReportingTest(_DriftRun):
    """A declared `watch` path that is not in the document (corpus-toolkit#72).

    The first version routed this into `failed` and printed it to stdout, which made it:
    counted as a "fetch failure" in the totals line, listed under "a fact about our access,
    not about upstream" — the precise opposite of what it is — folded into the >20% SYSTEMIC
    threshold, and invisible in CI (no annotation, exit 0). A watched field disappearing
    upstream is one of the most actionable things this tool can find, and it was the
    quietest.
    """

    def totals_line(self, out: str) -> str:
        """The `N changed, …` line ALONE.

        `assertIn("1 not compared", out)` was satisfied by the group breakdown's
        `gone 0/1 [1 not compared]`, so three tests naming the totals line in their
        docstrings passed with that clause deleted from it entirely."""
        return next(l for l in out.splitlines() if l.startswith(tuple("0123456789"))
                    and " changed, " in l)

    def json_group(self, name: str, docs: dict, *, watch: list, baseline="current",
                   declare_format=True):
        """A group of json sources with a `watch` list. `docs` maps id -> dict body.

        `declare_format=False` omits `format:`, which is what a real Socrata entry looks
        like — and `_format_for` maps an unrecognised `.json` extension to `"html"`."""
        lines = ["sources:"]
        for sid, doc in docs.items():
            url = f"https://example.gov/{name}/{sid}.json"
            raw = json.dumps(doc).encode()
            self.bodies[url] = raw
            try:
                sha = content_hash(raw, "json", watch=watch) if baseline == "current" else baseline
            except Exception:
                sha = "unknowable"
            watch_yaml = "\n".join(f"      - {w}" for w in watch)
            fmt = '\n                    format: json' if declare_format else ""
            lines.append(textwrap.dedent(f"""\
                  - id: "{sid}"
                    url: "{url}"{fmt}
                    sha256: "{sha}"
                    watch:
                """).rstrip() + "\n" + watch_yaml)
        (self.root / "_meta" / "sources" / f"{name}.yml").write_text("\n".join(lines) + "\n")

    def test_a_missing_watched_path_is_not_counted_as_a_fetch_failure(self):
        """The bytes ARRIVED. Calling that a fetch failure names a condition other than the
        one that occurred, and points the operator at the network instead of at upstream's
        schema."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}, "b": {"rowsUpdatedAt": 2}},
                        watch=["rowsUpdatedAt"])
        self.json_group("gone", {"c": {"viewCount": 9}}, watch=["rowsUpdatedAt"])

        code, out, err = self.run_cli()

        self.assertIn("0 fetch failure(s)", out,
                      "a watched-path miss was counted as a failed fetch")
        self.assertNotIn("a fact about our access", out + err,
                         "a document that arrived was listed under an access problem")
        # And the totals line must not say `of 3 checked` when one of the three was not
        # compared to anything — that is could-not-check reported as checked, on the one
        # line an operator reads.
        self.assertIn("1 not compared", self.totals_line(out),
                      f"the totals line counted an uncompared source as checked:\n{out}")

    def test_a_missing_watched_path_is_visible_where_ci_looks(self):
        """It printed to stdout and exited 0, so a weekly run reported success while one
        source had silently stopped being checked at all — corpus-toolkit#67's failure mode,
        rebuilt inside its own successor."""
        import os
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        self.json_group("gone", {"c": {"viewCount": 9}}, watch=["rowsUpdatedAt"])

        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, out, err = self.run_cli()

        self.assertIn("WATCH PATH MISSING", err, "reported on stdout, where CI does not look")
        self.assertIn("::warning", out + err, "no annotation, so nothing in the run summary")
        self.assertNotEqual(code, 0,
                            "a source that could not be checked at all exited 0")

    def test_watch_failures_do_not_trip_the_systemic_access_alarm(self):
        """>20% of fetches failing means the crawler cannot reach upstream. Watched-path
        misses are the opposite finding — every fetch succeeded — and mixing them makes the
        one alarm that says "stop, our access is broken" fire when access is fine."""
        self.json_group("gone", {"c": {"viewCount": 9}, "d": {"viewCount": 8}},
                        watch=["rowsUpdatedAt"])
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])

        code, out, err = self.run_cli()

        self.assertNotIn("SYSTEMIC", out + err,
                         "2 of 3 watched-path misses read as an access outage")

    def test_a_watch_that_is_a_bare_string_is_refused_before_anything_is_fetched(self):
        """`watch: rowsUpdatedAt` (scalar) is iterated CHARACTER BY CHARACTER, so the run
        reported `watched path 'r' is not present` — an authoring typo dressed up as
        "upstream changed shape", the most misleading thing this feature could say.

        Sibling of `_validated_volatile_patterns` and `_validated_index_headings`, and
        refused at the same moment for the same reason: after a 3,447-source crawl is the
        wrong time to learn a key was mistyped."""
        (self.root / "_meta" / "sources" / "ds.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "a"
                url: "https://example.gov/ds/a"
                format: json
                sha256: ""
                watch: rowsUpdatedAt
            """))
        fetched = []

        args = ["corpus-detect-changes", "--config", str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", lambda url: fetched.append(url) or b"{}"), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception) as e:
                changes.main()

        self.assertIn("watch", str(e.exception).lower())
        self.assertIn("a", str(e.exception), "the operator needs to know WHICH source")
        self.assertEqual(fetched, [], "the crawl started before the manifest was checked")

    def test_seeding_does_not_call_an_uncompared_source_a_failed_fetch_either(self):
        """The same mislabelling one level down. `_record_baselines` knows only "not in
        `fetched`", and printed that as `skipped (fetch failed)` — so an operator seeding
        baselines was told the network was the problem for a document that arrived intact.

        Skipping it is right: a hash it could not compute must never be written. Naming the
        reason wrong is not."""
        self.json_group("gone", {"c": {"viewCount": 9}}, watch=["rowsUpdatedAt"],
                        baseline="")

        code, out, err = self.run_cli("--record-baseline")

        self.assertNotIn("1 skipped (fetch failed)", out,
                         "a document that arrived was reported as a failed fetch")
        self.assertIn("not compared", out)

    def test_a_watched_source_is_not_counted_in_the_volatile_pattern_denominator(self):
        """A `watch` source never reaches the html/xml path, so no pattern touched it — but
        its bytes were added to `normalizable_bytes` anyway, which is the DENOMINATOR of the
        >10% breadth warning.

        That warning is the only thing standing between a corpus and a pattern that deletes
        content before hashing (corpus-toolkit#66). Padding the denominator with bytes no
        pattern processed switches it off silently: the wider the JSON body, the safer a
        dangerous pattern looks."""
        self.group("html", 1, baseline="current")
        # `format:` omitted, as a real Socrata entry has it — `_format_for` then calls it
        # html, the accounting block fires, and `content_hash` still takes the watch branch.
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1, "pad": "x" * 12000}},
                        watch=["rowsUpdatedAt"], declare_format=False)
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + "volatile_patterns:\n  - 'Rule text for source [0-9]+[.] '\n")

        code, out, err = self.run_cli()

        # Measured: the pattern strips 480 of the 1,210 bytes it actually processed — 39.7%,
        # far over VOLATILE_BREADTH_WARN. Padded with the JSON body's 12 KB it reported
        # `3.83% of 12544` and downgraded itself to a NOTE.
        self.assertIn("of 1 source(s)", out + err,
                      "a watched json source was counted as an HTML/XML source")
        self.assertIn("A pattern this wide deletes CONTENT", out + err,
                      "the breadth warning was switched off by bytes no pattern processed")

    def test_a_body_that_is_not_json_is_not_reported_as_a_missing_watch_path(self):
        """A 200-with-an-error-page and a watched field disappearing are DIFFERENT findings
        with different remedies, and every aggregate called both the second one.

        The per-source line had it right; the totals line, the summary and the CI annotation
        all said `a declared 'watch' path is absent from the fetched document` and sent the
        operator to check their path list. Naming a condition other than the one that
        occurred is what `_watched_digest`'s own comment says this codebase files bugs
        about — here it is one layer up, at the site an operator actually reads."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        self.bodies["https://example.gov/ds/a.json"] = b"<html>503 Service Unavailable</html>"

        code, out, err = self.run_cli()

        blame = out + err
        self.assertNotIn("watched path missing", blame.lower(),
                         "an error page served with a 200 was blamed on the watch list")
        self.assertIn("not parseable", blame.lower())
        self.assertNotEqual(code, 0)

    def test_the_group_breakdown_marks_a_group_that_compared_nothing(self):
        """`socrata 0/2` is byte-identical whether both sources were compared and found
        stable or neither was compared at all. The group line is the one that makes a bulk
        fault self-evident (corpus-toolkit#67), and it already carries `[N unseeded]` for
        exactly this class of caveat — the adjacent unchecked site."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        self.json_group("socrata", {"c": {"viewCount": 9}, "d": {"viewCount": 8}},
                        watch=["rowsUpdatedAt"])

        code, out, err = self.run_cli()

        self.assertIn("socrata 0/2 [2 not compared]", out,
                      f"a group where nothing was compared reads as stable:\n{out}")
        self.assertIn("ds 0/1,", out + ",", "an unaffected group grew a spurious marker")

    def test_a_watch_key_with_no_value_is_refused_rather_than_silently_ignored(self):
        """`watch:` with nothing under it — a mis-indented list, or one deleted a line at a
        time — parses to None, and the source reverted to hashing the whole document. The
        run then emitted exactly the `viewCount` false positives #72 exists to remove, from
        a manifest that VISIBLY DECLARES `watch`, with nothing said anywhere.

        One character away, `watch: []` is a hard load error. The same authoring accident
        must not get opposite treatment, and the silent branch is the wrong one to keep."""
        (self.root / "_meta" / "sources" / "ds.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "a"
                url: "https://example.gov/ds/a"
                format: json
                sha256: ""
                watch:
            """))
        self.bodies["https://example.gov/ds/a"] = json.dumps({"rowsUpdatedAt": 1}).encode()
        self.bodies["https://example.gov/ds/a.json"] = self.bodies["https://example.gov/ds/a"]

        args = ["corpus-detect-changes", "--config", str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", lambda url: self.bodies[url]), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception) as e:
                changes.main()

        self.assertIn("watch", str(e.exception).lower())


    def test_the_group_breakdown_marks_a_group_whose_fetches_all_failed_too(self):
        """THE ADJACENT SITE, and it predates `watch` entirely. A fetch failure skips the
        comparison exactly as a watched-path miss does, and the group line has always
        rendered `oar 0/2` for it — indistinguishable from a group that was fully compared
        and found stable.

        This is the shape corpus-toolkit#67 added the group line to expose: ERF's run had a
        DEQ group at 52/52 from a broken fetch. Fixing the marker for the new condition and
        not the old one would leave the line honest only about the case nobody has hit yet.
        """
        self.group("ok", 1, baseline="current")
        self.group("blocked", 2, baseline="current")
        for i in (0, 1):
            self.bodies[f"https://example.gov/blocked/{i}"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli()

        self.assertIn("blocked 0/2 [2 not compared]", out,
                      f"a group where every fetch failed reads as stable:\n{out}")


    def test_seeding_does_not_claim_a_reason_it_did_not_check(self):
        """`_record_baselines` knows only "no hash was computed" — its own comment says so
        and says the caller "must not claim one of the two reasons". The caller then listed
        two, and `WatchedDocumentUnreadable` is neither, so an error page served with a 200
        was reported as a failed fetch or a missing path while the run's own stderr said
        "not parseable json" three lines up. The same mislabelling one revision later."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"],
                        baseline="")
        self.bodies["https://example.gov/ds/a.json"] = b"<html>503</html>"

        code, out, err = self.run_cli("--record-baseline")

        self.assertNotIn("fetch failed, or a declared `watch` path was absent", out,
                         "the tally named two reasons and the actual one was a third")
        self.assertIn("1 skipped (not compared)", out)

    def test_a_volatile_pattern_measured_against_no_sources_still_reports(self):
        """`content_hash` now permits `volatile_patterns` + json on the grounds that "a
        pattern that matches nothing anywhere is already named in the drift report, per
        run". Excluding watch sources from `n_normalizable` can take that denominator to
        zero, and the report then `break`s out and prints NOTHING — so the justification the
        removal of the refusal rests on stops holding exactly when a corpus has no
        non-watch HTML sources left.

        Zero sources measured is itself the finding: the pattern is configured, and there
        was nothing for it to do."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"],
                        declare_format=False)
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + "volatile_patterns:\n  - 'sid=[0-9]+'\n")

        code, out, err = self.run_cli()

        self.assertIn("sid=", out + err,
                      "a configured pattern measured against zero sources said nothing")

    def test_a_bad_watch_in_an_out_of_scope_group_does_not_abort_the_run(self):
        """`--group` is "the per-cadence cron's knob". Validating every group up front made
        one group's typo abort every OTHER group's cron with an uncaught traceback, having
        printed nothing and fetched nothing.

        Fail-before-the-first-request is worth keeping; failing on a group this run was
        told not to look at is not."""
        self.group("oar", 1, baseline="current")
        (self.root / "_meta" / "sources" / "socrata.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "ds-1"
                url: "https://example.gov/socrata/1.json"
                sha256: ""
                watch: rowsUpdatedAt
            """))

        code, out, err = self.run_cli("--group", "oar")

        self.assertEqual(code, 0, f"an out-of-scope group's typo aborted the run:\n{err}")
        self.assertIn("oar 0/1", out)


    def test_not_compared_means_the_same_thing_on_every_line(self):
        """Three adjacent lines carried three definitions: the totals line counted watch
        misses only, the group breakdown counted fetch failures too, and the baseline tally
        used a third set. An operator read `1 not compared` and then counted 2 on the next
        line — and the only way to tell which was wrong was to read the source."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}, "b": {"viewCount": 9}},
                        watch=["rowsUpdatedAt"])
        self.group("html", 2, baseline="current")
        self.bodies["https://example.gov/html/1"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli()

        n_totals = int(self.totals_line(out).split(" not compared")[0].split(", ")[-1])
        n_groups = sum(int(part.split("[")[1].split(" ")[0])
                       for part in out.splitlines()
                       if part.startswith("drift by group")
                       for part in part.split("], ") if "not compared" in part)
        self.assertEqual(n_totals, n_groups,
                         f"the totals line and the group line disagree:\n{out}")
        self.assertEqual(n_totals, 2, "1 failed fetch + 1 watch miss = 2 not compared")

    def test_the_zero_source_note_does_not_blame_config_for_a_block(self):
        """`if not n_normalizable` fires whenever no HTML/XML source was successfully
        FETCHED — so a corpus whose HTML sources all 403'd was told its pattern is
        "configured and untested", which points at the manifest when the finding is that
        the crawler is being blocked. In scope and unreachable is not the same as never in
        scope."""
        self.group("html", 1, baseline="current")
        self.bodies["https://example.gov/html/0"] = OSError("HTTP Error 403")
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + "volatile_patterns:\n  - 'sid=[0-9]+'\n")

        code, out, err = self.run_cli()

        self.assertIn("sid=", out + err, "the pattern was not mentioned at all")
        self.assertNotIn("configured and untested", out + err,
                         "a blocked fetch was reported as a configuration problem")
        self.assertIn("could not be fetched", (out + err).lower())


    def test_watch_on_a_source_declared_html_is_refused_at_the_door(self):
        """A `watch:` block pasted onto a `format: html` entry — routine in a mixed-format
        manifest — sailed through validation, and `content_hash` takes the watch branch
        BEFORE the format branch, so `json.loads` met HTML and the run reported
        `WATCH BODY UNREADABLE ... a fact about the response — an error page served with a
        200, or a block`. The exact opposite of what happened, forever, from a manifest that
        says on its face what the format is.

        The check is an ALLOWLIST — `json` or `geojson`, declared or from the url's
        extension — for the same reason the feature itself is one: enumerating the formats
        that are not json makes every format nobody thought of a silent acceptance."""
        (self.root / "_meta" / "sources" / "rules.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "rule-a"
                url: "https://example.gov/rules/a"
                format: html
                sha256: ""
                watch:
                  - rowsUpdatedAt
            """))
        self.bodies["https://example.gov/rules/a"] = _body(1)

        args = ["corpus-detect-changes", "--config", str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception) as e:
                changes.main()

        self.assertIn("rule-a", str(e.exception))
        self.assertIn("html", str(e.exception))
        self.assertNotIn("UNREADABLE", out.getvalue() + err.getvalue())

    def test_a_duplicated_id_still_counts_as_not_compared_everywhere(self):
        """The one combination where "ONE DEFINITION, used on every line that says it" was
        false: `_record_baselines` skips a duplicated id BEFORE the not-compared counter, so
        a source that was both duplicated and never compared was counted by the totals line
        and the group line and not by the tally."""
        (self.root / "_meta" / "sources" / "dup.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "d"
                url: "https://example.gov/dup/1"
                format: html
                sha256: ""
              - id: "d"
                url: "https://example.gov/dup/2"
                format: html
                sha256: ""
            """))
        for i in (1, 2):
            self.bodies[f"https://example.gov/dup/{i}"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("2 not compared", self.totals_line(out))
        self.assertIn("[2 not compared]", out)
        self.assertIn("2 skipped (not compared)", out,
                      f"the tally disagreed with the two lines above it:\n{out}")


    def test_watch_on_an_extension_derived_non_json_format_is_refused_too(self):
        """The refusal keyed on an EXPLICIT `format:`, but `_format_for` derives
        `pdf/xls/xlsx/docx/xml` from the url extension just as declaratively — only the
        UNRECOGNISED extension falls back to html, and that fallback is the one case the
        rationale needs to protect (a Socrata `.json` url with no `format:`).

        So `watch:` on a `.xml` or `.pdf` url with no `format:` sailed through and produced
        exactly what the refusal exists to stop: `WATCH BODY UNREADABLE`, blaming the
        response for a fact about the declaration, on every run, forever.

        `.html` and `.csv` are here because the FIRST fix missed them: `_format_for` returns
        its `html` fallback for every extension it does not recognise, so exempting that
        fallback exempted real HTML pages along with the Socrata `.json` urls it was meant
        to protect. `.xml` and `.pdf` were refused and `.html` was not, with no `format:`
        declared in either case."""
        for ext in ("xml", "pdf", "html", "csv", "aspx"):
            with self.subTest(ext=ext):
                (self.root / "_meta" / "sources" / f"{ext}.yml").write_text(textwrap.dedent(f"""\
                    sources:
                      - id: "{ext}src"
                        url: "https://example.gov/{ext}/doc.{ext}"
                        sha256: ""
                        watch:
                          - rowsUpdatedAt
                    """))
                args = ["corpus-detect-changes", "--config",
                        str(self.root / "_meta" / "corpus.yml")]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", args), \
                     mock.patch.object(changes, "fetch", self._fetch), \
                     redirect_stdout(out), redirect_stderr(err):
                    with self.assertRaises(Exception) as e:
                        changes.main()
                self.assertIn(f"{ext}src", str(e.exception))
                (self.root / "_meta" / "sources" / f"{ext}.yml").unlink()

    def test_a_json_format_spelled_differently_is_not_refused(self):
        """`format: JSON` works identically today, and `geojson` is json too. The refusal
        must name the formats a watch list genuinely cannot read, not everything that is not
        the literal string `json`."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        text = (self.root / "_meta" / "sources" / "ds.yml").read_text()
        (self.root / "_meta" / "sources" / "ds.yml").write_text(
            text.replace("format: json", "format: JSON"))

        code, out, err = self.run_cli()

        self.assertEqual(code, 0, f"a json source was refused for its spelling:\n{err}")

    def test_a_duplicate_id_where_one_entry_fetched_still_agrees(self):
        """`fetched` is keyed `(group, id)`, so a SUCCESSFUL sibling populated the key and
        the failing entry looked fetched to the tally. Copying an entry and editing the url
        while forgetting the id is exactly how duplicates arise, so this is the common
        shape, not an exotic one."""
        (self.root / "_meta" / "sources" / "dup.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "d"
                url: "https://example.gov/dup/1"
                format: html
                sha256: ""
              - id: "d"
                url: "https://example.gov/dup/2"
                format: html
                sha256: ""
            """))
        self.bodies["https://example.gov/dup/1"] = _body(7)
        self.bodies["https://example.gov/dup/2"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("1 not compared", self.totals_line(out))
        self.assertIn("[1 not compared]", out)
        self.assertIn("1 skipped (not compared)", out,
                      f"the tally disagreed with the two lines above it:\n{out}")


class GroupDriftFindingTest(_DriftRun):
    """ADR 0010: a group where EVERY COMPARED source changed may get one finding of its own.

    It states that they changed together and asserts nothing about why. ERF run
    31022774644 is the case it exists for: `oar` was 484 of 484 changed sources, 89% of the
    drift and a single template-level cause, and the budget — spent smallest-group-first
    since corpus-toolkit#69 — reached it last and filed nothing at all for it.
    """

    def _findings(self):
        return [c.args[0] for c in self.group_finding.call_args_list]

    def test_a_group_where_every_compared_source_changed_gets_a_finding(self):
        self.group("oar", 4, baseline="stale")
        self.group("oam", 3, baseline="current")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._findings(), ["oar"],
                         "the whole-group drift got no finding of its own")

    def test_the_finding_accompanies_the_individual_tickets_and_never_replaces_them(self):
        """ADR 0010, and the one place issue #132's own sketch says the opposite. It
        proposed filing one issue for the group "instead of individual issues for its
        sources" (the sketch's own wording; CONTEXT.md avoids "group issue", which reads
        as a diagnosis of the group).

        The tool observes bytes, not causes, so a finding is not entitled to speak for a
        source that changed for its own reasons — and suppressing would reproduce, one
        level down, the starvation corpus-toolkit#69 had just removed."""
        self.group("oar", 4, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._findings(), ["oar"])
        self.assertEqual([c.args[0] for c in self.open_issue.call_args_list],
                         ["oar-0", "oar-1", "oar-2", "oar-3"],
                         "the group finding suppressed the individual tickets")

    def test_a_group_holding_one_compared_source_gets_no_finding(self):
        """ADR 0010: more than one compared source. One source cannot corroborate itself,
        and its own ticket already says everything the finding would — so the finding
        would spend a budget slot to say the same thing twice. Three ERF groups hold
        exactly one source (`constitution`, `external`, `public-utility-commission-policies`)."""
        self.group("constitution", 1, baseline="stale")
        self.group("oar", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._findings(), ["oar"],
                         "a group of one filed a finding that restates its own ticket")

    def test_a_group_that_was_never_compared_gets_no_finding(self):
        """THE REQUIRED CASE, and the one that decided ADR 0010's shape. oregon-counties
        reported 3,447 of 3,447 sources changed (corpus-toolkit#68) because every baseline
        was empty: nothing was ever compared, and a finding there would have diagnosed an
        inert run with confidence.

        The manifest is only partly unseeded, so the run is NOT inert — the existing
        wholly-unseeded refusal cannot be what makes this pass."""
        self.group("counties", 3, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._findings(), ["oar"],
                         "an unseeded group reported drift that never happened")

    def test_a_fetch_failure_is_not_a_compared_source_either(self):
        """The other half of "an uncompared source is not a changed source" (ADR 0010), and
        the one ERF actually hit: the DEQ group read 52/52 off a broken fetch.

        Two of the four were never compared, so the finding is about the two that were —
        and it must say `2 of 2`, not `2 of 4`, which would report the group as 50% drifted
        and contradict the rule that fired it."""
        self.group("blocked", 4, baseline="stale")
        for i in (0, 1):
            self.bodies[f"https://example.gov/blocked/{i}"] = OSError("HTTP Error 403")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._findings(), ["blocked"],
                         "every source that was compared changed, and nothing was filed")
        self.assertEqual(self.group_finding.call_args_list[0].args,
                         ("blocked", ["blocked-2", "blocked-3"], 2, 4),
                         "the finding counted sources it never compared — or lost track "
                         "of how many were in the group at all")

    def test_a_source_that_is_both_unseeded_and_unfetchable_is_subtracted_once(self):
        """`unseeded` is counted before the fetch and `uncompared` after it, so ONE source
        that has no baseline and then fails to fetch increments both. Deriving the compared
        count as `total - unseeded - uncompared` removes it twice — here that reads 1
        compared source where there are 2, and the finding silently disappears under the
        "more than one compared source" rule. The count is measured at the comparison
        instead, so this shape cannot arise."""
        (self.root / "_meta" / "sources" / "mixed.yml").write_text(textwrap.dedent("""\
            group: mixed
            sources:
              - id: "mixed-gone"
                url: "https://example.gov/mixed/gone"
                format: html
                sha256: ""
              - id: "mixed-a"
                url: "https://example.gov/mixed/a"
                format: html
                sha256: "stale"
              - id: "mixed-b"
                url: "https://example.gov/mixed/b"
                format: html
                sha256: "stale"
            """))
        self.bodies["https://example.gov/mixed/gone"] = OSError("HTTP Error 403")
        self.bodies["https://example.gov/mixed/a"] = _body(1)
        self.bodies["https://example.gov/mixed/b"] = _body(2)

        code, out, err = self.run_cli("--open-issues")

        self.assertEqual(self._findings(), ["mixed"],
                         f"the finding vanished: the group reads as 1 compared source, "
                         f"below the more-than-one rule:\n{out}")
        self.assertEqual(self.group_finding.call_args_list[0].args,
                         ("mixed", ["mixed-a", "mixed-b"], 2, 3),
                         f"the both-markers source was subtracted twice:\n{out}")

    def test_one_compared_source_holding_still_is_enough_to_withhold_the_finding(self):
        """ADR 0010 rejected ">80%" on principle, not preference: 100% is the only
        threshold that is itself an observation, and the sources that did NOT change are
        evidence against the very pattern the finding would assert. Nine of ten is nine of
        ten, and the tenth says the ten did not move together.

        Sized so a share rule and the observation rule disagree: 9 of 10 is 90%."""
        self.group("oar", 9, baseline="stale")
        self.group("oar", 1, baseline="current", start=9, file_stem="oar-stable")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("oar 9/10", out, "the fixture is not 90% drift")
        self.assertEqual(self._findings(), [],
                         "a group with a source that held still still spoke for it")

    def test_a_group_whose_every_fetch_failed_gets_no_finding(self):
        """The adjacent shape to the unseeded one, and the reason the rule is stated over
        COMPARED sources rather than over the group. A group where nothing was fetched has
        no changed source and no compared source; "every compared source changed" is
        vacuously true of it, and a finding would report drift on a group the run never
        looked at."""
        self.group("blocked", 2, baseline="stale")
        self.group("oar", 2, baseline="stale")
        for i in (0, 1):
            self.bodies[f"https://example.gov/blocked/{i}"] = OSError("HTTP Error 403")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("blocked 0/2 [2 not compared]", out, "the fixture compared nothing")
        self.assertEqual(self._findings(), ["oar"],
                         "a group nothing was fetched from reported whole-group drift")

    def test_the_finding_consumes_a_slot_and_is_filed_before_the_individual_tickets(self):
        """Both orderings of ADR 0010 in one fixture, because they only hold together.

        The cap is the cap: exempting the finding would mean a corpus with twenty-seven
        bulk-drifting groups files twenty-seven issues past a limit of twenty-five. And the
        finding files FIRST — corpus-toolkit#69 spends the budget smallest-drifting-group
        first, which reaches the largest group last, so a finding queued behind the tickets
        is exactly the finding that never files. Here the group fills the budget on its
        own: if the finding were filed last there would be no slot left for it."""
        n = changes.MAX_ISSUES_PER_RUN
        self.group("oar", n, baseline="stale")

        code, out, err = self.run_cli("--open-issues")

        self.assertEqual(self._findings(), ["oar"],
                         "the finding was queued behind a budget it could not outlast")
        self.assertEqual(self.open_issue.call_count, n - 1,
                         "the finding was filed outside the cap, so the cap is not the cap")
        self.assertEqual(code, 1, "a capped run is still not a clean run")
        self.assertIn("STOPPED after", err)

    def test_findings_the_budget_did_not_reach_are_not_swallowed_silently(self):
        """The findings come out of the cap, so enough of them exhaust it on their own —
        and a finding the budget never reached is exactly the silence corpus-toolkit#132
        was opened about. It must be said, not left to be inferred from a count."""
        n = changes.MAX_ISSUES_PER_RUN + 1
        for i in range(n):
            self.group(f"g{i:02d}", 2, baseline="stale")

        code, out, err = self.run_cli("--open-issues")

        self.assertEqual(self.group_finding.call_count, changes.MAX_ISSUES_PER_RUN,
                         "the findings ignored the cap they are supposed to spend from")
        self.assertEqual(self.open_issue.call_count, 0,
                         "the budget was spent on findings; no slot is left to invent")
        self.assertEqual(code, 1)
        self.assertIn("1 group drift finding(s) were not filed", err,
                      f"a finding the budget never reached went unmentioned:\n{err}")
        # NAMED, not counted. Every neighbouring message names its groups, and a bare
        # count leaves the reader to work out which group is missing an issue by
        # subtracting two lists — the inference this message exists to remove.
        self.assertIn("g25", err.split("were not filed")[1],
                      f"the dropped finding was counted but not named:\n{err}")

    def test_the_unreached_group_line_does_not_deny_a_finding_that_was_filed(self):
        """`N group(s) with drift were not reached by the budget at all ... raising nothing
        for them is the cap` predates ADR 0010, and a group finding makes it false: those
        groups now have an issue. An operator who believes the line stops looking, and the
        issue it denies is the one corpus-toolkit#132 exists to create."""
        for i in range(5):
            self.group(f"g{i:02d}", 8, baseline="stale")

        code, out, err = self.run_cli("--open-issues")

        filed = [c.args[0] for c in self.open_issue.call_args_list]
        self.assertNotIn("g04-0", filed, "the fixture must starve the last group")
        self.assertIn("g04", self._findings(), "which still gets a finding of its own")
        self.assertNotIn("raising nothing for them", err,
                         f"the run denied an issue it had just filed:\n{err}")
        self.assertIn("did file a group drift finding", err)

    def test_the_largest_drifting_group_files_its_finding_first(self):
        """The ADR settles the findings' order against the TICKETS, not against each
        other, so this is a choice the ADR left open. It is observable only when the
        findings alone exhaust the budget — and then no per-source ticket files at all, so
        no group in the list is covered by tickets instead. What is left to prefer by is
        the evidence: dropping the largest group's finding discards the most of what the
        run observed. Ties by group name, so a re-run over the same drift files the same
        set."""
        n = changes.MAX_ISSUES_PER_RUN
        # Named to sort LAST, so alphabetical order and drift order disagree — a fixture
        # where they coincide cannot tell which one the run used.
        self.group("zbig", 4, baseline="stale")
        for i in range(n):
            self.group(f"g{i:02d}", 2, baseline="stale")

        code, out, err = self.run_cli("--open-issues")

        self.assertEqual(self._findings()[0], "zbig",
                         "the group with the most evidence behind it was dropped first")
        self.assertNotIn("g24", self._findings(),
                         "and the budget really did run out before the last one")

    def test_the_run_says_which_groups_it_filed_a_finding_for(self):
        """A new kind of issue in the tracker that the run's own log never mentions is the
        corpus-toolkit#53 shape: the summary counts what drifted, and an operator reads it
        as what was reported."""
        self.group("oar", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("1 group drift finding(s) opened or already open, 0 failed: oar",
                      out, f"the run filed a finding and did not say so:\n{out}")

    def test_a_finding_that_failed_to_file_is_not_reported_as_filed(self):
        """The other half of corpus-toolkit#53: attempted is not opened."""
        self.group("oar", 3, baseline="stale")
        self.failing_findings = {"oar"}
        code, out, err = self.run_cli("--open-issues")
        self.assertIn("0 group drift finding(s) opened or already open, 1 failed", out,
                      f"a finding that does not exist was counted as filed:\n{out}")


class GroupFindingIssueTest(unittest.TestCase):
    """What the finding actually files. The seam is the same one `_open_issue` uses."""

    def test_an_already_open_finding_is_not_filed_again(self):
        """A whole-group drift persists across runs until somebody resolves it, and the
        run fires weekly. Without the dedupe search that `_open_issue` has, the same
        unresolved condition files a new issue every week."""
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run", side_effect=[
                 _completed(0, "1"), _completed(0, "https://github.com/o/r/issues/2"),
             ]) as run:
            out = io.StringIO()
            with redirect_stdout(out):
                ok = changes._open_group_finding("oar", ["oar-0", "oar-1"], 2, 2)
        self.assertTrue(ok, "an open finding already reports this group")
        self.assertEqual(run.call_args_list[0].args[0][1:3], ["issue", "list"],
                         "the finding did not look for its own title before filing")
        self.assertEqual(run.call_count, 1,
                         "a second issue was filed for a condition already tracked")

    def _file(self, group, ids, compared, in_scope=None):
        """File one finding against a mocked `gh` and return (search argv, create argv)."""
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run", side_effect=[
                 _completed(0, "0"), _completed(0, "https://github.com/o/r/issues/2"),
             ]) as run:
            changes._open_group_finding(group, ids, compared,
                                        in_scope if in_scope is not None else compared)
        return run.call_args_list[0].args[0], run.call_args_list[1].args[0]

    @staticmethod
    def _opt(argv, name):
        return argv[argv.index(name) + 1]

    def test_the_title_carries_no_counts_so_the_condition_files_once(self):
        """ADR 0010: `_open_issue` prevents re-filing by searching for its own title, and
        `Group drifted: oar (484 of 484)` breaks that — the run that reads 480 tomorrow
        writes a different title, the search misses, and the same unresolved condition
        files a second issue. The counts belong in the body."""
        first_search, first_create = self._file("oar", [f"oar-{i}" for i in range(484)], 484)
        _, later_create = self._file("oar", [f"oar-{i}" for i in range(480)], 480)

        title = self._opt(first_create, "--title")
        self.assertEqual(title, self._opt(later_create, "--title"),
                         "the title moved while the condition persisted")
        self.assertNotIn("484", title, "a count in the title defeats the dedupe search")
        self.assertIn(f'in:title "{title}"', first_search,
                      "the search must look for the title that is actually filed")
        body = self._opt(first_create, "--body")
        self.assertIn("484 of 484", body, "the counts have to be somewhere")
        self.assertIn("oar-0", body, "and a sample of the ids behind them")

    def test_it_refuses_to_write_a_finding_about_a_group_that_partly_changed(self):
        """`- **Compared sources that changed**: 480 of 484` is the ">80%" finding ADR 0010
        rejected, and nothing in this function stops it being rendered: the two numbers
        arrive independently and the rule that they be equal lives at the one call site.
        This is the gate, not a second copy of the rule — the rule stays where it is, and
        the writer refuses anything that does not satisfy it."""
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run") as run:
            with self.assertRaises(ValueError) as e:
                changes._open_group_finding("oar", [f"oar-{i}" for i in range(480)], 484, 484)
        self.assertIn("480", str(e.exception))
        self.assertIn("484", str(e.exception))
        run.assert_not_called()

    def test_the_body_says_how_much_of_the_group_was_never_compared(self):
        """`2 of 2` is true of a group of two and of a group of five where three were
        never compared, and those are the two shapes corpus-toolkit#67 built the per-group
        breakdown to separate. The denominator is honest because it excludes them; the
        report has to carry what it excluded, or the reader cannot tell how much of the
        group the finding actually speaks for."""
        _, create = self._file("oar", ["oar-3", "oar-4"], 2, in_scope=5)
        body = self._opt(create, "--body")
        self.assertIn("2 of 2", body)
        self.assertIn("5", body, "the group's size in scope is not in the report")
        self.assertIn("3", body, "nor how many of them were never compared")
        self.assertIn("not compared", body)

    def test_the_body_links_the_run_that_found_it_when_there_is_one(self):
        """ADR 0010 puts the run link in the body. A finding says only that these sources
        changed together; the run it came out of is where the breakdown, the per-source
        hashes and `changed-sources.tsv` are, and without the link a reader has to guess
        which weekly run produced the issue they are holding."""
        env = {"GITHUB_SERVER_URL": "https://github.com",
               "GITHUB_REPOSITORY": "OregonAI/erf", "GITHUB_RUN_ID": "31022774644"}
        with mock.patch.dict(changes.os.environ, env, clear=False):
            _, create = self._file("oar", ["oar-0", "oar-1"], 2)
        self.assertIn("https://github.com/OregonAI/erf/actions/runs/31022774644",
                      self._opt(create, "--body"))

    def test_the_body_does_not_invent_a_run_link_outside_actions(self):
        """The environment does not always say. Half a url built from missing variables is
        a dead link that looks like a citation."""
        for var in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
            with mock.patch.dict(changes.os.environ, {}, clear=False):
                changes.os.environ.pop(var, None)
                _, create = self._file("oar", ["oar-0", "oar-1"], 2)
            body = self._opt(create, "--body")
            self.assertNotIn("actions/runs", body,
                             f"a run link was built with {var} missing:\n{body}")


if __name__ == "__main__":
    unittest.main()
