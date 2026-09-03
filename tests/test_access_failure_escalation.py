"""Tests for corpus-toolkit#166: a source can fail every fetch, forever, and nothing
accumulates that across runs — `FETCH FAILED` prints on every run but a source failing its
thirtieth run in a row is indistinguishable, in that run's own output, from one failing its
first.

The operator's decision (2026-09-02), against measured data:

    ESCALATE ON 2 CONSECUTIVE FAILED RUNS **OR** 14 ELAPSED DAYS, WHICHEVER COMES FIRST.

Two arms, tested SEPARATELY, because a test that only drives the run-count arm proves
nothing about the arm that actually matters for a slow-cadence corpus
(executive-regulatory-frameworks runs MONTHLY): under run-counting alone, 2 consecutive
runs of a monthly cron is ~30 days away. The elapsed-days arm exists to catch that source
without waiting for a second monthly fetch to ever happen — which means it has to fire off
state alone, before this run's own group is even due to be re-fetched. See
`ElapsedDaysEscalationTest` below for how that is driven without waiting 14 real days.

State lives in `access-failures.json` at the corpus root, sibling to `source-outcomes.json`
(corpus-toolkit#160) rather than in the source manifest: the manifest is CURATED data a
human reviews in a PR (`_record_baselines`'s docstring), and a fetch-failure streak is
machine bookkeeping that changes every run — writing it there would put unreviewed noise
in a file whose whole discipline is that nothing lands in it without a human reading the
diff.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest import mock

from test_drift_reporting import _DriftRun  # noqa: E402 — bare import, tests/ has no __init__

from corpus_toolkit.sources import changes


class _AccessFailureRun(_DriftRun):
    """`_DriftRun` plus a mockable clock and a mockable access-failure issue opener.

    `_open_access_failure_issue` is the new seam this feature adds, mocked the same way
    `_open_issue` and `_open_group_finding` already are: the test drives `main()` through
    the CLI and observes what it decided to file, without reaching into how it decided.
    """

    def setUp(self):
        super().setUp()
        self.failing_af_ids: set[str] = set()
        self._today = date(2026, 1, 1)

    def set_today(self, iso_date: str) -> None:
        self._today = date.fromisoformat(iso_date)

    def run_cli(self, *argv):
        args = ["corpus-detect-changes", "--config",
                str(self.root / "_meta" / "corpus.yml"), *argv]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             mock.patch.object(changes, "_utcnow_date", side_effect=lambda: self._today), \
             mock.patch.object(changes, "_ensure_label", return_value=True), \
             mock.patch.object(changes, "_ensure_access_failure_label", return_value=True), \
             mock.patch.object(changes, "_open_issue",
                               side_effect=lambda sid, *a: sid not in self.failing_ids
                               ) as open_issue, \
             mock.patch.object(changes, "_open_group_finding",
                               side_effect=lambda g, *a, **k: g not in self.failing_findings
                               ) as group_finding, \
             mock.patch.object(changes, "_open_access_failure_issue",
                               side_effect=lambda sid, *a, **k: sid not in self.failing_af_ids
                               ) as af_issue, \
             redirect_stdout(out), redirect_stderr(err):
            self.open_issue = open_issue
            self.group_finding = group_finding
            self.af_issue = af_issue
            try:
                changes.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def _filed_af_ids(self):
        return [c.args[0] for c in self.af_issue.call_args_list]


class ConsecutiveRunsEscalationTest(_AccessFailureRun):
    """The run-count arm: 2 consecutive failed runs escalate, 1 does not."""

    def test_a_single_failed_run_does_not_escalate(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        code, out, err = self.run_cli("--open-issues")
        self.af_issue.assert_not_called()

    def test_a_second_consecutive_failed_run_escalates_exactly_once(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues")
        self.set_today("2026-08-06")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self._filed_af_ids(), ["erf-0"])

    def test_a_recovered_source_resets_the_streak(self):
        # Fails once, succeeds once, fails again: that is two isolated failures, never two
        # CONSECUTIVE ones, and must not escalate.
        self.group("erf", 1, baseline="current")
        self.set_today("2026-08-05")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.run_cli("--open-issues")
        self.set_today("2026-08-06")
        from test_drift_reporting import _body
        self.bodies["https://example.gov/erf/0"] = _body(0)  # fetch succeeds this run
        self.run_cli("--open-issues")
        self.set_today("2026-08-07")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.run_cli("--open-issues")
        self.af_issue.assert_not_called()


class ElapsedDaysEscalationTest(_AccessFailureRun):
    """The elapsed-days arm — the one that actually matters for a slow-cadence corpus.

    A test that only drives the run-count arm proves nothing here: by the time a source
    fails its SECOND consecutive run, the run-count arm (>= 2) has already fired, so the
    two arms can never be observed to disagree from within a single group's own repeated
    runs — the second observation always satisfies both at once. To prove the days arm
    on its own, the source must be held below the run-count threshold (exactly 1 failure
    recorded) while wall-clock time passes, which means driving it through a DIFFERENT
    group's run — exactly the monthly-corpus-inside-a-multi-cadence-workflow shape ERF is
    (a `department-of-human-services-policies` group checked only monthly, alongside other
    groups on other schedules).
    """

    def test_a_single_failure_escalates_after_14_elapsed_days_via_another_groups_run(self):
        # The failing source lives in "monthly", never touched again by name.
        self.group("monthly", 1, baseline="stale")
        self.bodies["https://example.gov/monthly/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues", "--group", "monthly")
        self.af_issue.assert_not_called()  # 1 failure, 0 elapsed days: neither arm fires

        # A DIFFERENT group's own run, 15 days later. It never fetches "monthly-0" —
        # "monthly" is not in --group — so the run-count arm cannot have moved: this is
        # still the ONE failure recorded above.
        self.group("weekly", 1, baseline="current")
        self.set_today("2026-08-20")
        code, out, err = self.run_cli("--open-issues", "--group", "weekly")

        self.assertEqual(self._filed_af_ids(), ["monthly-0"],
                         "elapsed days must escalate a HELD source from an out-of-scope "
                         "group, off another group's run, without a second fetch of it")
        # Prove this really was the days arm and not a disguised run-count trip: the state
        # file must still show exactly one recorded failure for this source.
        state = json.loads((self.root / "access-failures.json").read_text())
        rec = next(s for s in state["sources"] if s["id"] == "monthly-0")
        self.assertEqual(rec["consecutive_failures"], 1,
                         "the run-count arm must NOT have fired — this is the days arm")

    def test_under_14_elapsed_days_does_not_escalate(self):
        self.group("monthly", 1, baseline="stale")
        self.bodies["https://example.gov/monthly/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues", "--group", "monthly")

        self.group("weekly", 1, baseline="current")
        self.set_today("2026-08-15")  # 10 days later — under the 14-day threshold
        self.run_cli("--open-issues", "--group", "weekly")
        self.af_issue.assert_not_called()


class AccessFailureStatePersistenceTest(_AccessFailureRun):
    """The state artifact itself: held, pruned, and cleared, on the ground the withdrawn
    attempt's own postmortem measured (`wip/166-access-failure-escalation`, item #6:
    "no pruning" — a retired source kept asserting it was currently failing forever)."""

    def _state(self):
        return json.loads((self.root / "access-failures.json").read_text())

    def test_a_recorded_streak_survives_a_run_of_a_different_group(self):
        self.group("monthly", 1, baseline="stale")
        self.bodies["https://example.gov/monthly/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues", "--group", "monthly")
        self.assertEqual(self._state()["sources"],
                         [{"group": "monthly", "id": "monthly-0",
                           "consecutive_failures": 1, "first_failed_at": "2026-08-05"}])

        self.group("weekly", 1, baseline="current")
        self.run_cli("--open-issues", "--group", "weekly")
        self.assertEqual(self._state()["sources"],
                         [{"group": "monthly", "id": "monthly-0",
                           "consecutive_failures": 1, "first_failed_at": "2026-08-05"}],
                         "a source outside this run's --group scope must be HELD, not "
                         "cleared just because this run did not observe it failing again")

    def test_a_source_retired_from_a_checked_group_is_pruned_not_kept_forever(self):
        self.group("monthly", 1, baseline="stale")
        self.bodies["https://example.gov/monthly/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues", "--group", "monthly")
        self.assertEqual(len(self._state()["sources"]), 1)

        # The source is removed from the manifest entirely (retired upstream, or the
        # curator dropped it) and its OWN group is checked again.
        self.group("monthly", 0, baseline="stale")
        self.run_cli("--open-issues", "--group", "monthly")
        self.assertEqual(self._state()["sources"], [],
                         "a retired source must not go on asserting it is currently "
                         "failing forever")

    def test_a_group_deleted_entirely_is_pruned_not_held_forever(self):
        # Different from the case above: there the group FILE stays (sources: []). Here
        # the whole file is gone, so the group is not `declared_groups` at all and
        # `checked_groups` (derived from `declared_groups`) can never contain it either --
        # the rule that prunes a retired SOURCE never reaches a retired GROUP.
        self.group("gone", 1, baseline="stale")
        self.bodies["https://example.gov/gone/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues", "--group", "gone")
        self.assertEqual(len(self._state()["sources"]), 1)

        (self.root / "_meta" / "sources" / "gone.yml").unlink()
        # A run of some OTHER group -- the deleted group is not even named in --group,
        # which is exactly the scope a held source would otherwise survive under.
        self.group("other", 1, baseline="current")
        self.run_cli("--open-issues", "--group", "other")
        self.assertEqual(self._state()["sources"], [],
                         "a group deleted entirely must not go on asserting its sources "
                         "are currently failing forever")

    def test_a_successful_fetch_clears_the_streak(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--open-issues")
        self.assertEqual(len(self._state()["sources"]), 1)

        from test_drift_reporting import _body
        self.bodies["https://example.gov/erf/0"] = _body(0)
        self.run_cli("--open-issues")
        self.assertEqual(self._state()["sources"], [])

    def test_an_unreadable_state_file_degrades_to_empty_rather_than_crashing(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        (self.root / "access-failures.json").write_text("{not json")
        code, out, err = self.run_cli("--open-issues")
        # Recovers rather than raising, and starts a fresh streak (1, not a crash).
        self.assertEqual(self._state()["sources"][0]["consecutive_failures"], 1)


class BudgetSharedWithDriftTest(_AccessFailureRun):
    """`MAX_ISSUES_PER_RUN` covers escalations too (corpus-toolkit#166), spent LAST — after
    group findings and per-source drift tickets, which keep the priority they already had.

    The withdrawn attempt (`wip/166-access-failure-escalation`) filed access failures
    FIRST and, at ERF's own shape (45 failing, 30 drifted), found drift reporting stopped
    entirely from the third run on because access failures recur every run the same way
    unaddressed drift does. This pins the opposite ordering.
    """

    def test_drift_and_findings_are_spent_before_escalations(self):
        # One group, wholly drifted: 20 tickets + 1 group-drift finding = 21 of the budget.
        self.group("drifted", 20, baseline="stale")
        # 10 sources already past the escalation threshold, seeded directly rather than
        # driven through two real runs — the threshold logic itself is proven by the
        # tests above; this test is about ALLOCATION, not detection.
        self.group("broken", 10, baseline="current")
        for i in range(10):
            self.bodies[f"https://example.gov/broken/{i}"] = Exception("blocked")
        (self.root / "access-failures.json").write_text(json.dumps({
            "schema_version": 1,
            "sources": [{"group": "broken", "id": f"broken-{i}",
                        "consecutive_failures": 5, "first_failed_at": "2026-07-01"}
                       for i in range(10)],
        }))
        self.set_today("2026-08-01")
        code, out, err = self.run_cli("--open-issues")

        self.assertEqual(self.group_finding.call_count, 1)
        self.assertEqual(self.open_issue.call_count, 20)
        self.assertEqual(len(self._filed_af_ids()), changes.MAX_ISSUES_PER_RUN - 21)
        self.assertEqual(code, 1, "a capped run is still not a clean run")
        self.assertIn("access-failure escalation(s) were not reported either", err)


class AccessFailureIssueContentTest(unittest.TestCase):
    """`_open_access_failure_issue` itself: the ticket must say only what it knows."""

    def test_body_states_the_fact_is_about_access_not_upstream(self):
        captured = {}
        with mock.patch.object(
                changes, "_file_once",
                side_effect=lambda title, body, subj, **kw: captured.update(
                    title=title, body=body, label=kw.get("label")) or True):
            ok = changes._open_access_failure_issue("doc-1", "https://x/1", 4, 30)
        self.assertTrue(ok)
        self.assertEqual(captured["title"], "Access failure: doc-1")
        self.assertIn("fact about OUR ACCESS", captured["body"])
        self.assertIn("not a claim that it changed or was removed upstream",
                      captured["body"])
        self.assertIn("4", captured["body"])
        self.assertIn("30", captured["body"])
        self.assertNotIn("removed upstream\n", captured["body"],
                         "must not assert removal as a fact, only disclaim it")
        # corpus-toolkit review: the escalation label, not `source-change`'s -- sharing it
        # would land every escalation in the drift queue and make the two indistinguishable.
        self.assertEqual(captured["label"], changes.ACCESS_FAILURE_LABEL)

    def test_body_does_not_generalize_past_what_this_run_measured(self):
        # A single held observation, 15 days old -- the elapsed-days arm's own shape: the
        # earlier body text asserted "unreachable across multiple runs" and "printed on
        # every run since the first of these failures" unconditionally, both false here
        # against the "1" printed three lines above them.
        captured = {}
        with mock.patch.object(
                changes, "_file_once",
                side_effect=lambda title, body, subj, **kw: captured.update(
                    body=body) or True):
            changes._open_access_failure_issue("doc-1", "https://x/1", 1, 15)
        self.assertIn("Consecutive failed runs**: 1", captured["body"])
        self.assertNotIn("unreachable across multiple runs", captured["body"])
        self.assertNotIn("printed on every run since", captured["body"])
