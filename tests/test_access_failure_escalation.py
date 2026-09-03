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

ADR 0015: an escalation is no longer a `Access failure:` issue. Once a source crosses the
threshold it is marked **escalated** in `DRIFT.md`, and the run says so on stderr — worded,
as ever, as a fact about our access and never a claim about upstream.
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
    """`_DriftRun` plus a mockable clock, so the elapsed-days arm can be driven without
    sleeping real days."""

    def setUp(self):
        super().setUp()
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
             redirect_stdout(out), redirect_stderr(err):
            try:
                changes.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def drift_md(self) -> str:
        return (self.root / "DRIFT.md").read_text()

    def access_row(self, sid: str) -> str:
        """The DRIFT.md access-failures table row for `sid` — the row itself, not the
        fixed prose above the table, which always contains the literal word "escalated"
        regardless of whether anything actually is."""
        return next(l for l in self.drift_md().splitlines() if f"`{sid}`" in l)


class ConsecutiveRunsEscalationTest(_AccessFailureRun):
    """The run-count arm: 2 consecutive failed runs escalate, 1 does not."""

    def test_a_single_failed_run_does_not_escalate(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        code, out, err = self.run_cli()
        self.assertNotIn("marked escalated", err)
        self.assertNotIn("**escalated**", self.access_row("erf-0"))

    def test_a_second_consecutive_failed_run_escalates_exactly_once(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli()
        self.set_today("2026-08-06")
        code, out, err = self.run_cli()
        self.assertIn(
            "1 access failure(s) past 2 consecutive failed runs or 14 elapsed days "
            "(ADR 0013), marked escalated in DRIFT.md: erf-0", err)
        self.assertIn("**escalated**", self.access_row("erf-0"))

    def test_a_recovered_source_resets_the_streak(self):
        # Fails once, succeeds once, fails again: that is two isolated failures, never two
        # CONSECUTIVE ones, and must not escalate.
        self.group("erf", 1, baseline="current")
        self.set_today("2026-08-05")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.run_cli()
        self.set_today("2026-08-06")
        from test_drift_reporting import _body
        self.bodies["https://example.gov/erf/0"] = _body(0)  # fetch succeeds this run
        self.run_cli()
        self.set_today("2026-08-07")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        code, out, err = self.run_cli()
        self.assertNotIn("marked escalated", err)


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
        code, out, err = self.run_cli("--group", "monthly")
        self.assertNotIn("marked escalated", err)  # 1 failure, 0 elapsed days: neither arm fires

        # A DIFFERENT group's own run, 15 days later. It never fetches "monthly-0" —
        # "monthly" is not in --group — so the run-count arm cannot have moved: this is
        # still the ONE failure recorded above.
        self.group("weekly", 1, baseline="current")
        self.set_today("2026-08-20")
        code, out, err = self.run_cli("--group", "weekly")

        self.assertIn("marked escalated in DRIFT.md: monthly-0", err,
                      "elapsed days must escalate a HELD source from an out-of-scope "
                      "group, off another group's run, without a second fetch of it")
        self.assertIn("**escalated**", self.access_row("monthly-0"))
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
        self.run_cli("--group", "monthly")

        self.group("weekly", 1, baseline="current")
        self.set_today("2026-08-15")  # 10 days later — under the 14-day threshold
        code, out, err = self.run_cli("--group", "weekly")
        self.assertNotIn("marked escalated", err)
        self.assertNotIn("**escalated**", self.access_row("monthly-0"))


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
        self.run_cli("--group", "monthly")
        self.assertEqual(self._state()["sources"],
                         [{"group": "monthly", "id": "monthly-0",
                           "consecutive_failures": 1, "first_failed_at": "2026-08-05"}])

        self.group("weekly", 1, baseline="current")
        self.run_cli("--group", "weekly")
        self.assertEqual(self._state()["sources"],
                         [{"group": "monthly", "id": "monthly-0",
                           "consecutive_failures": 1, "first_failed_at": "2026-08-05"}],
                         "a source outside this run's --group scope must be HELD, not "
                         "cleared just because this run did not observe it failing again")

    def test_a_source_retired_from_a_checked_group_is_pruned_not_kept_forever(self):
        self.group("monthly", 1, baseline="stale")
        self.bodies["https://example.gov/monthly/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli("--group", "monthly")
        self.assertEqual(len(self._state()["sources"]), 1)

        # The source is removed from the manifest entirely (retired upstream, or the
        # curator dropped it) and its OWN group is checked again.
        self.group("monthly", 0, baseline="stale")
        self.run_cli("--group", "monthly")
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
        self.run_cli("--group", "gone")
        self.assertEqual(len(self._state()["sources"]), 1)

        (self.root / "_meta" / "sources" / "gone.yml").unlink()
        # A run of some OTHER group -- the deleted group is not even named in --group,
        # which is exactly the scope a held source would otherwise survive under.
        self.group("other", 1, baseline="current")
        self.run_cli("--group", "other")
        self.assertEqual(self._state()["sources"], [],
                         "a group deleted entirely must not go on asserting its sources "
                         "are currently failing forever")

    def test_a_successful_fetch_clears_the_streak(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        self.set_today("2026-08-05")
        self.run_cli()
        self.assertEqual(len(self._state()["sources"]), 1)

        from test_drift_reporting import _body
        self.bodies["https://example.gov/erf/0"] = _body(0)
        self.run_cli()
        self.assertEqual(self._state()["sources"], [])

    def test_an_unreadable_state_file_degrades_to_empty_rather_than_crashing(self):
        self.group("erf", 1, baseline="stale")
        self.bodies["https://example.gov/erf/0"] = Exception("blocked")
        (self.root / "access-failures.json").write_text("{not json")
        code, out, err = self.run_cli()
        # Recovers rather than raising, and starts a fresh streak (1, not a crash).
        self.assertEqual(self._state()["sources"][0]["consecutive_failures"], 1)


if __name__ == "__main__":
    unittest.main()
