"""drift-state.json and DRIFT.md — the rolling report (ADR 0015), tested through the
interface the run and `corpus-drift-report` share: state in, markdown out.

The state's three rules are the ones `access-failures.json` already follows and a test
here holds them to the same words: an in-scope source is REPLACED by this run's
observation, a source retired from a group this run enumerated is PRUNED, a source in a
group this run did not check is HELD. The one fact only this file can hold —
`first_changed_at` — is carried while a source stays changed against the same baseline and
cleared the run it is observed anything else.
"""
import json
import pathlib
import tempfile
from datetime import date

import pytest

from corpus_toolkit.sources import changes, drift_report
from corpus_toolkit.sources.drift_report import DriftRecord

SO, CS = changes.SourceOutcome, changes.ChangedSource
D1, D2, D3 = date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)


def _run(prior, outcomes, changed=(), *, seeded=(), accepted=(), checked=None, declared=None, today=D1):
    in_scope = {(o.group, o.id) for o in outcomes}
    groups = {o.group for o in outcomes}
    return drift_report.update_drift_state(
        prior, list(outcomes), list(changed), seeded=set(seeded), accepted=set(accepted),
        in_scope=in_scope, checked_groups=set(checked if checked is not None else groups),
        declared_groups=set(declared if declared is not None else groups), today=today)


def test_a_changed_source_carries_first_changed_at_while_the_baseline_is_the_same():
    s1 = _run({}, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "old", "new")], today=D1)
    assert s1[("g", "a")].first_changed_at == "2026-09-01"
    s2 = _run(s1, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "old", "new2")], today=D2)
    assert s2[("g", "a")].first_changed_at == "2026-09-01", "still changed against the same baseline: the date is carried"
    assert s2[("g", "a")].observed_at == "2026-09-08" and s2[("g", "a")].new == "new2"


def test_first_changed_at_resets_when_the_baseline_it_changed_from_moved():
    s1 = _run({}, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "old", "new")], today=D1)
    s2 = _run(s1, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "other-old", "new")], today=D2)
    assert s2[("g", "a")].first_changed_at == "2026-09-08", "a different baseline is a different change"


def test_an_unchanged_observation_clears_first_changed_at():
    s1 = _run({}, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "old", "new")], today=D1)
    s2 = _run(s1, [SO("g", "a", "u", "unchanged", True)], today=D2)
    assert s2[("g", "a")].outcome == "unchanged" and s2[("g", "a")].first_changed_at is None


def test_a_source_in_an_unchecked_group_is_held_exactly():
    s1 = _run({}, [SO("g", "a", "u", "changed", True), SO("h", "b", "v", "unchanged", True)],
              [CS("g", "a", "u", "old", "new")], today=D1)
    # next run checks only h; g is out of scope and must be held as observed on D1
    s2 = _run(s1, [SO("h", "b", "v", "changed", True)], [CS("h", "b", "v", "o", "n")],
              checked={"h"}, declared={"g", "h"}, today=D2)
    assert s2[("g", "a")] == s1[("g", "a")]
    assert s2[("h", "b")].outcome == "changed"


def test_a_source_retired_from_a_checked_group_is_pruned():
    s1 = _run({}, [SO("g", "a", "u", "unchanged", True), SO("g", "b", "u2", "unchanged", True)], today=D1)
    s2 = _run(s1, [SO("g", "a", "u", "unchanged", True)], checked={"g"}, declared={"g"}, today=D2)
    assert ("g", "b") not in s2, "b left the manifest of a group this run enumerated"


def test_a_group_deleted_from_the_manifest_entirely_is_pruned_too():
    s1 = _run({}, [SO("g", "a", "u", "unchanged", True), SO("h", "b", "v", "unchanged", True)], today=D1)
    s2 = _run(s1, [SO("g", "a", "u", "unchanged", True)], checked={"g"}, declared={"g"}, today=D2)
    assert ("h", "b") not in s2


def test_seeding_and_accepting_are_recorded():
    s1 = _run({}, [SO("g", "a", "u", "no_baseline", False)], [CS("g", "a", "u", "", "new")],
              seeded={("g", "a")}, today=D1)
    assert s1[("g", "a")].seeded_at == "2026-09-01" and s1[("g", "a")].outcome == "no_baseline"
    s2 = _run(s1, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "new", "newer")],
              accepted={("g", "a")}, today=D2)
    r = s2[("g", "a")]
    assert r.accepted_at == "2026-09-08" and r.outcome == "unchanged" and r.old == "newer", \
        "an accepted baseline means the source agrees with its manifest again"
    assert r.seeded_at == "2026-09-01", "the seed date survives later observations"


def test_state_round_trips_through_the_file(tmp_path):
    s1 = _run({}, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "old", "new")], today=D1)
    drift_report.write_drift_state(tmp_path, s1, {"date": "2026-09-01", "red_reasons": []})
    loaded = drift_report.load_drift_state(tmp_path)
    assert loaded == s1
    assert drift_report.load_last_run(tmp_path)["date"] == "2026-09-01"
    data = json.loads((tmp_path / "drift-state.json").read_text())
    assert data["schema_version"] == 1 and data["sources"][0]["id"] == "a"


def test_an_unreadable_state_file_degrades_to_empty_with_a_warning(tmp_path, capsys):
    (tmp_path / "drift-state.json").write_text("{not json")
    assert drift_report.load_drift_state(tmp_path) == {}
    assert "could not be read" in capsys.readouterr().err


# ------------------------------------------------------------------ rendering

def _render(state, access=None, escalations=(), last_run=None, today=D3):
    return drift_report.render_drift_md(
        state, access or {}, list(escalations),
        last_run if last_run is not None else {"date": today.isoformat(), "toolkit_version": "t",
                                               "groups_in_scope": sorted({k[0] for k in state}),
                                               "group_filter": None, "red_reasons": [],
                                               "totals": {"total": len(state)}},
        escalate_runs=2, escalate_days=14, today=today)


def test_changed_since_baseline_lists_the_source_with_its_first_observed_date():
    s = _run({}, [SO("g", "a", "https://x/a", "changed", True), SO("g", "b", "https://x/b", "unchanged", True)],
             [CS("g", "a", "https://x/a", "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb")], today=D1)
    md = _render(s)
    assert "## Changed since baseline (1)" in md
    assert "| g | `a` | 2026-09-01 | `aaaaaaaaaaaa…` | `bbbbbbbbbbbb…` | https://x/a |" in md
    assert "`b`" not in md.split("## Changed since baseline")[1].split("##")[0]


def test_a_group_whose_every_compared_source_changed_is_named_and_a_partial_one_is_not():
    s = _run({}, [SO("g", "a", "u", "changed", True), SO("g", "b", "u", "changed", True),
                  SO("h", "c", "u", "changed", True), SO("h", "d", "u", "unchanged", True),
                  SO("i", "e", "u", "changed", True)],
             [CS("g", "a", "u", "o", "n"), CS("g", "b", "u", "o", "n"), CS("h", "c", "u", "o", "n"),
              CS("i", "e", "u", "o", "n")], today=D1)
    md = _render(s)
    sec = md.split("## Groups whose every compared source changed")[1].split("## Access")[0]
    assert "(1)" in md.split("## Groups whose every compared source changed")[1][:5]
    assert "| g | 2 of 2 | 2 |" in sec
    assert "| h |" not in sec, "one compared source holding still withholds the finding (ADR 0010)"
    assert "| i |" not in sec, "one source cannot corroborate itself"


def test_access_failures_are_their_own_section_and_escalation_is_marked():
    access = {("g", "a"): changes.AccessFailureRecord("g", "a", 3, "2026-08-20"),
              ("g", "b"): changes.AccessFailureRecord("g", "b", 1, "2026-09-14")}
    esc = [changes.AccessFailureEscalation("g", "a", 3, 26)]
    md = _render({}, access, esc)
    assert "## Access failures (2, 1 escalated)" in md
    assert "| g | `a` | 3 | 2026-08-20 (26d) | **escalated** |" in md
    assert "| g | `b` | 1 | 2026-09-14 (1d) |  |" in md
    assert "fact about our access" in md


def test_seeded_this_run_lists_only_this_runs_seeds():
    s = _run({}, [SO("g", "a", "u", "no_baseline", False), SO("g", "b", "u", "unchanged", True)],
             [CS("g", "a", "u", "", "n" * 20)], seeded={("g", "a")}, today=D3)
    md = _render(s, today=D3)
    assert "## Seeded this run (1)" in md and "| g | `a` | `nnnnnnnnnnnn…` |" in md
    s_next = _run(s, [SO("g", "a", "u", "unchanged", True), SO("g", "b", "u", "unchanged", True)], today=date(2026, 10, 1))
    md_next = _render(s_next, today=date(2026, 10, 1),
                      last_run={"date": "2026-10-01", "red_reasons": [], "totals": {}})
    assert "## Seeded this run (0)" in md_next


def test_a_red_verdict_is_on_the_face_of_the_report():
    md = _render({}, last_run={"date": "2026-09-15", "toolkit_version": "t", "groups_in_scope": [],
                               "group_filter": ["nosuchgroup"],
                               "red_reasons": ["nothing was in scope, so nothing was checked"],
                               "totals": {"total": 0}})
    assert "**RED** — the run could not do its job: nothing was in scope" in md
    assert "(asked for: nosuchgroup)" in md


def test_render_is_pure():
    s = _run({}, [SO("g", "a", "u", "changed", True)], [CS("g", "a", "u", "o", "n")], today=D1)
    assert _render(s) == _render(s)


def test_by_group_tally_counts_every_source_once():
    s = _run({}, [SO("g", "a", "u", "changed", True), SO("g", "b", "u", "fetch_failed", True),
                  SO("g", "c", "u", "no_baseline", False)], [CS("g", "a", "u", "o", "n")], today=D1)
    md = _render(s)
    assert "| g | 3 | 1 | 0 | 1 | 1 | 0 |" in md
