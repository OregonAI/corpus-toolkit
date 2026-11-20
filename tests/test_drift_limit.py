"""`corpus-detect-changes --limit N` — bound a run, least recently checked first
(corpus-toolkit#204), tested through the CLI over a real manifest with only the network faked.

The contract under test: every source a run fetches gets a `last_checked_at`; `--limit N`
checks at most N of the selected sources, never-checked first and then oldest, ties broken
by group then id; the sources it did not check keep their state untouched (not unchanged,
not failed, access-failure streaks neither advanced nor cleared, not pruned as retired);
and a limited run says so in every report it writes.
"""
import json
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_drift_reporting import _DriftRun  # noqa: E402 — bare import, tests/ has no __init__

from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
import io  # noqa: E402

from corpus_toolkit.repo import content_hash  # noqa: E402
from corpus_toolkit.sources import changes  # noqa: E402
from test_drift_reporting import _body  # noqa: E402

TODAY = date(2026, 11, 19)


class _Limit(_DriftRun):
    def run_on(self, day, *argv, env=None):
        args = ["corpus-detect-changes", "--config",
                str(self.root / "_meta" / "corpus.yml"), *argv]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             mock.patch.object(changes, "_utcnow_date", lambda: day), \
             mock.patch.dict("os.environ", env or {}), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                changes.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def run_cli(self, *argv):  # noqa: D401 — fixed clock for every call
        return self.run_on(TODAY, *argv)

    def state(self):
        return json.loads((self.root / "drift-state.json").read_text())

    def rec(self, gid):
        return {(r["group"], r["id"]): r for r in self.state()["sources"]}[gid]

    def checked_ids(self):
        outs = json.loads((self.root / "source-outcomes.json").read_text())
        return [s["id"] for s in outs["sources"]]

    def write_state(self, checked: dict, *, drop_field=False):
        """checked: {(group, id): iso date or None}. Writes a prior drift-state.json."""
        recs = []
        for (g, i), d in checked.items():
            r = {"group": g, "id": i, "url": f"https://example.gov/{i.rsplit('-', 1)[0]}/"
                 f"{i.rsplit('-', 1)[1]}", "outcome": "unchanged", "had_baseline": True,
                 "observed_at": d or "2026-01-01", "first_changed_at": None, "old": "x",
                 "new": "x", "seeded_at": None, "accepted_at": None}
            if not drop_field:
                r["last_checked_at"] = d
            recs.append(r)
        (self.root / "drift-state.json").write_text(json.dumps(
            {"schema_version": 1, "generated_by": "t", "last_run": {}, "sources": recs}))


class RecordsLastCheckedTest(_Limit):
    def test_every_fetched_or_failed_source_gets_todays_date(self):
        self.group("g", 3, baseline="current")
        self.bodies["https://example.gov/g/2"] = RuntimeError("blocked")
        self.run_cli()
        for i in range(3):
            self.assertEqual(self.rec(("g", f"g-{i}"))["last_checked_at"], "2026-11-19")

    def test_a_state_file_without_the_field_loads_and_reads_as_never_checked(self):
        self.group("g", 2, baseline="current")
        self.write_state({("g", "g-0"): "2026-01-01", ("g", "g-1"): "2026-01-01"},
                         drop_field=True)
        code, _, err = self.run_cli("--limit", "1")
        self.assertEqual(code, 0)
        self.assertNotIn("could not be read", err)
        self.assertEqual(self.checked_ids(), ["g-0"])

    def test_without_limit_every_source_is_checked_and_reports_carry_no_limit(self):
        self.group("g", 3, baseline="current")
        self.run_cli()
        outs = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertNotIn("limit", outs)
        self.assertNotIn("limit", self.state()["last_run"])
        self.assertNotIn("limited", (self.root / "DRIFT.md").read_text().lower())


class SelectionOrderTest(_Limit):
    def test_never_checked_first_then_oldest_then_group_then_id(self):
        self.group("b", 2, baseline="current")
        self.group("a", 2, baseline="current")
        self.group("c", 2, baseline="current")
        self.write_state({("a", "a-0"): "2026-11-10", ("a", "a-1"): "2026-11-01",
                          ("b", "b-0"): "2026-11-01", ("b", "b-1"): "2026-11-15"})
        # c-0, c-1 never checked (no record); then 2026-11-01: a-1 before b-0 (group).
        self.run_cli("--limit", "4")
        self.assertEqual(sorted(self.checked_ids()), ["a-1", "b-0", "c-0", "c-1"])

    def test_a_null_last_checked_is_never_checked(self):
        self.group("g", 3, baseline="current")
        self.write_state({("g", "g-0"): "2026-11-01", ("g", "g-1"): None,
                          ("g", "g-2"): "2026-11-02"})
        self.run_cli("--limit", "1")
        self.assertEqual(self.checked_ids(), ["g-1"])

    def test_selection_is_deterministic(self):
        self.group("g", 5, baseline="current")
        self.run_cli("--limit", "2")
        first = self.checked_ids()
        self.assertEqual(first, ["g-0", "g-1"])

    def test_rotates_until_everything_has_been_checked(self):
        self.group("g", 5, baseline="current")
        seen = []
        for day in (10, 11, 12):
            self.run_on(date(2026, 11, day), "--limit", "2")
            seen.append(self.checked_ids())  # manifest order within a run
        # day 12: g-4 was never checked; g-0 is the oldest of the rest (tie by id)
        self.assertEqual(seen, [["g-0", "g-1"], ["g-2", "g-3"], ["g-0", "g-4"]])

    def test_limit_applies_across_the_selected_groups_only(self):
        self.group("a", 2, baseline="current")
        self.group("b", 2, baseline="current")
        self.run_cli("--group", "b", "--limit", "1")
        self.assertEqual(self.checked_ids(), ["b-0"])
        out = self.run_cli("--group", "b", "--limit", "1")[1]
        self.assertIn("checked 1 of 2", out)


class UncheckedSourcesAreUntouchedTest(_Limit):
    def test_state_of_unchecked_sources_is_byte_identical(self):
        self.group("g", 3, baseline="stale")
        self.run_on(date(2026, 11, 1))
        before = self.rec(("g", "g-2"))
        self.run_on(date(2026, 11, 19), "--limit", "2")
        self.assertEqual(self.rec(("g", "g-2")), before)
        self.assertEqual(self.rec(("g", "g-0"))["last_checked_at"], "2026-11-19")

    def test_unchecked_sources_are_not_counted_in_any_total(self):
        self.group("g", 4, baseline="current")
        _, out, _ = self.run_cli("--limit", "3")
        outs = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(outs["totals"]["total"], 3)
        self.assertEqual(outs["totals"]["unchanged"], 3)
        self.assertEqual(outs["groups"]["g"]["checked"], 3)
        self.assertEqual(self.state()["last_run"]["totals"]["total"], 3)

    def test_unchecked_sources_are_not_pruned_as_retired(self):
        self.group("g", 3, baseline="current")
        self.run_on(date(2026, 11, 1))
        self.run_on(date(2026, 11, 19), "--limit", "1")
        ids = {r["id"] for r in self.state()["sources"]}
        self.assertEqual(ids, {"g-0", "g-1", "g-2"})

    def test_access_failure_streaks_are_held_for_unchecked_sources(self):
        self.group("g", 3, baseline="current")
        for i in range(3):
            self.bodies[f"https://example.gov/g/{i}"] = RuntimeError("blocked")
        self.run_on(date(2026, 11, 1))
        self.run_on(date(2026, 11, 2))
        fails = json.loads((self.root / "access-failures.json").read_text())["sources"]
        self.assertEqual({f["consecutive_failures"] for f in fails}, {2})
        # g-2 now serves fine but is not selected: its streak must be neither advanced
        # nor cleared. g-0, g-1 (older-or-equal; ties by id) are checked and still fail.
        self.bodies["https://example.gov/g/2"] = _body(2)
        self.run_on(date(2026, 11, 3), "--limit", "2")
        fails = {f["id"]: f for f in
                 json.loads((self.root / "access-failures.json").read_text())["sources"]}
        self.assertEqual(fails["g-0"]["consecutive_failures"], 3)
        self.assertEqual(fails["g-1"]["consecutive_failures"], 3)
        self.assertEqual(fails["g-2"]["consecutive_failures"], 2)
        self.assertEqual(self.rec(("g", "g-2"))["outcome"], "fetch_failed")

    def test_unchecked_unseeded_sources_stay_unseeded(self):
        self.group("g", 3, baseline=None)
        self.run_cli("--limit", "1")
        text = (self.root / "_meta" / "sources" / "g.yml").read_text()
        self.assertEqual(text.count('sha256: ""'), 2)
        self.assertEqual(text.count(content_hash(_body(0), "html")), 1)

    def test_a_group_with_nothing_selected_is_not_a_drift_finding(self):
        self.group("g", 3, baseline="stale")
        self.run_on(date(2026, 11, 1))
        _, out, _ = self.run_cli("--limit", "1")
        self.assertNotIn("g-1", out)


class LimitedRunSaysSoTest(_Limit):
    def setUp(self):
        super().setUp()
        self.group("g", 4, baseline="current")
        self.write_state({("g", "g-0"): "2026-11-01", ("g", "g-1"): "2026-11-05",
                          ("g", "g-2"): "2026-11-10", ("g", "g-3"): "2026-11-12"})

    def test_console_says_how_many_of_how_many_and_the_oldest_left(self):
        _, out, err = self.run_cli("--limit", "2")
        self.assertIn("LIMITED RUN: checked 2 of 4", out + err)
        self.assertIn("oldest last-checked left unchecked: 2026-11-10", out + err)

    def test_source_outcomes_records_the_limit(self):
        self.run_cli("--limit", "2")
        outs = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(outs["limit"], {
            "requested": 2, "checked": 2, "enumerated": 4, "unchecked": 2,
            "unchecked_never_checked": 0, "oldest_unchecked_last_checked": "2026-11-10"})

    def test_drift_state_last_run_records_the_limit(self):
        self.run_cli("--limit", "2")
        self.assertEqual(self.state()["last_run"]["limit"]["unchecked"], 2)

    def test_drift_md_says_the_run_was_limited_and_not_green_as_a_full_run(self):
        self.run_cli("--limit", "2")
        md = (self.root / "DRIFT.md").read_text()
        self.assertIn("LIMITED", md)
        self.assertIn("2 of 4", md)
        self.assertIn("2026-11-10", md)
        self.assertNotIn("green — the run compared what it set out to compare", md)

    def test_rerendering_without_a_fetch_keeps_the_banner(self):
        from corpus_toolkit.sources import drift_report
        self.run_cli("--limit", "2")
        (self.root / "DRIFT.md").unlink()
        with mock.patch.object(changes, "_utcnow_date", lambda: TODAY):
            self.assertEqual(drift_report.main(
                ["--config", str(self.root / "_meta" / "corpus.yml")]), 0)
        self.assertIn("LIMITED", (self.root / "DRIFT.md").read_text())

    def test_actions_warning_annotation(self):
        _, out, _ = self.run_cli("--limit", "2")
        self.assertNotIn("::warning", out)
        _, out, _ = self.run_on(TODAY, "--limit", "2", env={"GITHUB_ACTIONS": "true"})
        self.assertIn("::warning title=Drift run limited by --limit::", out)
        self.assertIn("2 of 4", out)

    def test_never_checked_is_named_when_it_is_what_was_left(self):
        self.group("h", 3, baseline="current")
        self.run_cli("--limit", "1")
        outs = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(outs["limit"]["unchecked_never_checked"], 2)
        self.assertIsNone(outs["limit"]["oldest_unchecked_last_checked"])
        self.assertIn("never checked", (self.root / "DRIFT.md").read_text())

    def test_a_limit_that_covers_everything_is_an_ordinary_run(self):
        self.run_cli("--limit", "4")
        outs = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertNotIn("limit", outs)
        self.assertNotIn("LIMITED", (self.root / "DRIFT.md").read_text())


class LimitValidationAndInterplayTest(_Limit):
    def test_zero_and_negative_and_non_integer_are_refused(self):
        self.group("g", 2, baseline="current")
        for bad in ("0", "-3", "x"):
            code, _, err = self.run_cli("--limit", bad)
            self.assertEqual(code, 2, bad)
        self.assertFalse((self.root / "drift-state.json").exists())

    def test_robots_report_is_not_narrowed_by_limit_and_says_so(self):
        self.group("g", 3, baseline="current")
        with mock.patch.object(changes, "_report_robots", return_value=0) as rep:
            code, out, err = self.run_cli("--check-robots", "--limit", "1")
        self.assertEqual(code, 0)
        rep.assert_called_once()
        self.assertIn("--limit does not narrow", out + err)

    def test_record_baseline_refresh_rewrites_only_the_checked_sources(self):
        self.group("g", 3, baseline="stale")
        self.run_cli("--limit", "2", "--record-baseline=refresh")
        text = (self.root / "_meta" / "sources" / "g.yml").read_text()
        self.assertEqual(text.count("stale"), 1)
        self.assertEqual(text.count(content_hash(_body(0), "html")), 1)
        self.assertNotIn(("g", "g-2"), {(r["group"], r["id"]) for r in self.state()["sources"]},
                         "never observed, so no record is invented for it")
        md = (self.root / "DRIFT.md").read_text()
        self.assertIn("LIMITED", md)

    def test_nothing_enumerated_is_still_nothing_checked(self):
        self.group("g", 2, baseline="current")
        code, _, err = self.run_cli("--group", "nosuch", "--limit", "5")
        self.assertEqual(code, 1)
        self.assertIn("NOTHING WAS CHECKED", err)
