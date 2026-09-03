"""DRIFT.md and drift-state.json — the rolling drift report, and the state it renders from
(ADR 0015).

    corpus-drift-report --config _meta/corpus.yml     # re-render DRIFT.md from the state files

WHY A FILE AND NOT A TICKET. Until v1.34.0 a drift run filed one GitHub issue per changed
source, one per group whose every compared source changed (ADR 0010) and one per source
whose fetch had failed past a threshold (ADR 0013), capped at 25 a run. Measured on
2026-09-02: 90 of the 172 open issues across the org were those tickets, 25 of them already
labelled `wontfix`, none of them read. The medium was the problem, not the claims — each
claim is still made here, in the same words, in a file a person opens when they are about
to re-ingest, which is the only time a drift report is wanted.

TWO FILES, ONE READING. `drift-state.json` is the machine state: one record per source the
platform knows about, carrying the LAST observation of that source and, for a changed one,
the date the change was first observed. `DRIFT.md` is rendered from it and from
`access-failures.json`, and is never the source of anything. A run rewrites both;
`corpus-drift-report` rewrites only the markdown, from the state as it stands.

ROLLING BY CONSTRUCTION. A source that changed stays `changed` in the state until its
baseline is refreshed (`corpus-detect-changes --record-baseline=refresh`) or it is
re-ingested and the manifest re-seeded, because the manifest still records the hash it
changed FROM. A source in a group this run was not told to check is HELD as last observed
(the rule `access-failures.json` already follows); a source retired from a group this run
did enumerate is pruned rather than kept asserting an observation nobody made.

WHAT THIS FILE DOES NOT KNOW. `source-outcomes.json` remains the per-run artifact with its
own contract (corpus-toolkit#160); `changed-sources.tsv` remains the four-column file
corpus repos read positionally. Neither is replaced. This module reads neither: the state
is built by `corpus-detect-changes` from the same in-memory outcomes those two are written
from, so the three cannot disagree about a run they all describe.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import NamedTuple

from corpus_toolkit import config as config_mod

STATE_FILE = "drift-state.json"
REPORT_FILE = "DRIFT.md"
SCHEMA_VERSION = 1


class DriftRecord(NamedTuple):
    """The last observation of one source, as `drift-state.json` records it.

    `outcome` uses the vocabulary of `source-outcomes.json` (`changed`, `unchanged`,
    `no_baseline`, `fetch_failed`, `unreadable_json`, `watch_path_missing`) — the same
    six strings, so a reader who learned one file has learned both.

    `first_changed_at` is set the first run a source is observed `changed` against a given
    baseline and CARRIED while it stays changed against that same baseline; it is cleared
    the run it is observed anything else. That is the one fact the per-run artifacts cannot
    hold: they know a source changed this run, not since when.

    `seeded_at` names the run that wrote this source's first baseline; `accepted_at` names
    the run that refreshed a recorded baseline to the observed value, i.e. the operator
    accepting the change without re-ingesting.
    """
    group: str
    id: str
    url: str
    outcome: str
    had_baseline: bool
    observed_at: str          # ISO date of the run that made this observation
    first_changed_at: str | None
    old: str                  # baseline hash at observation ("" when none)
    new: str                  # observed hash ("" when the fetch did not complete)
    seeded_at: str | None
    accepted_at: str | None


def _key(rec) -> tuple[str, str]:
    return (rec.group, rec.id)


def load_drift_state(root: Path) -> dict[tuple[str, str], DriftRecord]:
    """Last run's state, or `{}` — with a warning, never silently — when unreadable.

    Degrades the same way `access-failures.json` does and for the same reason: a state
    file that cannot be read must not abort the run that would repair it, but an operator
    must be told that every `first_changed_at` just reset to today.
    """
    path = root / STATE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = {}
        for r in data.get("sources", []):
            rec = DriftRecord(r["group"], r["id"], r.get("url", ""), r["outcome"],
                              bool(r.get("had_baseline")), r["observed_at"],
                              r.get("first_changed_at"), r.get("old", ""), r.get("new", ""),
                              r.get("seeded_at"), r.get("accepted_at"))
            out[_key(rec)] = rec
        return out
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"WARNING: {STATE_FILE} exists but could not be read ({e}) — starting from "
              f"empty state; every `first_changed_at` this run records will read as today.",
              file=sys.stderr)
        return {}


def update_drift_state(prior: dict[tuple[str, str], DriftRecord], outcomes, changed,
                       *, seeded: set, accepted: set, in_scope: set, checked_groups: set,
                       declared_groups: set, today: date) -> dict[tuple[str, str], DriftRecord]:
    """This run's state: last run's, with every in-scope observation replaced.

    `outcomes` are the run's `SourceOutcome`s (group, id, url, outcome, had_baseline);
    `changed` its `ChangedSource`s (group, id, url, old, new), used for the hashes.
    `seeded` and `accepted` are `(group, id)` keys the run wrote a baseline for, in seed
    and refresh mode respectively.

    Three rules, all shared with `access-failures.json`:
    * an in-scope source is REPLACED by this run's observation;
    * a source in a group this run was told to check but that is no longer in scope was
      retired from the manifest and is PRUNED;
    * a source in a group this run did not check is HELD exactly as it was.
    """
    hashes = {(c.group, c.id): (c.old, c.new) for c in changed}
    today_s = today.isoformat()
    state = {}
    for k, rec in prior.items():
        if rec.group in checked_groups and k not in in_scope:
            continue  # retired from a group this run enumerated
        if rec.group not in declared_groups and rec.group not in checked_groups:
            # the whole group is gone from the manifest: nothing can observe it again
            continue
        state[k] = rec
    for o in outcomes:
        k = (o.group, o.id)
        old, new = hashes.get(k, ("", ""))
        prev = prior.get(k)
        first_changed = None
        if o.outcome == "changed":
            first_changed = (prev.first_changed_at
                             if prev and prev.outcome == "changed" and prev.old == old
                             and prev.first_changed_at else today_s)
        rec = DriftRecord(o.group, o.id, o.url, o.outcome, o.had_baseline, today_s,
                          first_changed, old, new,
                          today_s if k in seeded else (prev.seeded_at if prev else None),
                          today_s if k in accepted else None)
        if k in accepted:
            # The operator accepted the observed value as the new baseline: from the
            # state's point of view the source now agrees with its manifest.
            rec = rec._replace(outcome="unchanged", first_changed_at=None, old=new)
        state[k] = rec
    return state


def write_drift_state(root: Path, state: dict[tuple[str, str], DriftRecord],
                      last_run: dict) -> Path:
    path = root / STATE_FILE
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": f"corpus-detect-changes {_toolkit_version()}",
        "last_run": last_run,
        "sources": [r._asdict() for _, r in sorted(state.items())],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_last_run(root: Path) -> dict:
    path = root / STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_run", {}) or {}
    except (OSError, ValueError):
        return {}


def _toolkit_version() -> str:
    try:
        return metadata.version("corpus-toolkit")
    except metadata.PackageNotFoundError:
        return "unknown"


# ------------------------------------------------------------------ rendering

def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_none_", ""]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out + [""]


def render_drift_md(state: dict[tuple[str, str], DriftRecord], access: dict,
                    escalations: list, last_run: dict, *, escalate_runs: int,
                    escalate_days: int, today: date) -> str:
    """The report, from the state. Pure: same inputs, same bytes.

    Sections, in the order a reader about to re-ingest wants them:
    1. what the last run was and whether it could do its job (the verdict);
    2. every source whose upstream differs from the baseline this corpus mirrors, with the
       date that was first seen — the rolling core;
    3. groups where every compared source changed together (ADR 0010, correlation not
       cause);
    4. sources our fetches have been failing for, and which have crossed the ADR 0013
       threshold — a fact about our access, never about upstream;
    5. what this run seeded, accepted, or could not compare;
    6. the per-group tally.
    """
    recs = sorted(state.values(), key=lambda r: (r.group, r.id))
    changed = [r for r in recs if r.outcome == "changed"]
    failing = [r for r in recs if r.outcome == "fetch_failed"]
    uncompared = [r for r in recs if r.outcome in ("unreadable_json", "watch_path_missing")]
    no_baseline = [r for r in recs if r.outcome == "no_baseline" and not r.seeded_at]
    lr = last_run or {}
    run_date = lr.get("date", "never")
    seeded_now = [r for r in recs if r.seeded_at == run_date]
    accepted_now = [r for r in recs if r.accepted_at == run_date]

    lines = [
        "# Drift report",
        "",
        f"**Generated by `corpus-detect-changes` — do not edit by hand.** Rendered "
        f"{today.isoformat()} from `{STATE_FILE}` and `access-failures.json`; re-render with "
        f"`corpus-drift-report --config _meta/corpus.yml`. This file replaces the "
        f"`Source changed:`, `Group drifted:` and `Access failure:` issues a run used to "
        f"file (corpus-toolkit ADR 0015). It states what a fetch observed and never why.",
        "",
        "## Last run",
        "",
    ]
    if not lr:
        lines += ["No drift run has written state yet.", ""]
    else:
        red = lr.get("red_reasons") or []
        verdict = ("**RED** — the run could not do its job: " + "; ".join(red)) if red \
            else "green — the run compared what it set out to compare"
        scope = ", ".join(lr.get("groups_in_scope") or []) or "(nothing in scope)"
        asked = lr.get("group_filter")
        t = lr.get("totals") or {}
        lines += [
            f"- **Date**: {run_date} · **toolkit**: {lr.get('toolkit_version', 'unknown')}",
            f"- **Verdict**: {verdict}",
            f"- **Scope**: {scope}" + (f" (asked for: {', '.join(asked)})" if asked else ""),
            f"- **This run**: {t.get('total', 0)} in scope · {t.get('changed', 0)} changed · "
            f"{t.get('unchanged', 0)} unchanged · {t.get('seeded', 0)} seeded · "
            f"{t.get('accepted', 0)} baselines accepted · {t.get('fetch_failed', 0)} fetch "
            f"failed · {t.get('unreadable_json', 0) + t.get('watch_path_missing', 0)} not "
            f"comparable",
            "",
        ]
    lines += [
        f"## Changed since baseline ({len(changed)})",
        "",
        "Upstream serves something different from the hash this corpus's manifest records. "
        "A source stays here until it is re-ingested and re-seeded, or its baseline is "
        "accepted with `--record-baseline=refresh`. The date is when the change was first "
        "observed against the current baseline.",
        "",
    ]
    lines += _table(["group", "id", "since", "was", "now", "url"],
                    [[r.group, f"`{r.id}`", r.first_changed_at or r.observed_at,
                      f"`{r.old[:12]}…`", f"`{r.new[:12]}…`", r.url] for r in changed])

    # ADR 0010: every compared source in the group changed. Computed over the state's
    # last observations, group by group. Compared = changed + unchanged.
    by_group: dict[str, dict] = {}
    for r in recs:
        g = by_group.setdefault(r.group, {"total": 0, "changed": 0, "unchanged": 0,
                                          "fetch_failed": 0, "no_baseline": 0, "other": 0})
        g["total"] += 1
        g[r.outcome if r.outcome in g else "other"] += 1
    together = [(g, c) for g, c in sorted(by_group.items())
                if c["changed"] > 1 and c["unchanged"] == 0]
    lines += [
        f"## Groups whose every compared source changed ({len(together)})",
        "",
        "Correlation, not cause (ADR 0010): the sources changed together, and this report "
        "asserts nothing about why. A whole group moving at once has been a template edit, "
        "a set of URLs that stopped serving what they did, and a manifest whose baselines "
        "had never been recorded.",
        "",
    ]
    lines += _table(["group", "changed of compared", "in group"],
                    [[g, f"{c['changed']} of {c['changed'] + c['unchanged']}", c["total"]]
                     for g, c in together])

    esc_keys = {(e.group, e.id) for e in escalations}
    acc_rows = []
    for k, rec in sorted(access.items()):
        elapsed = (today - date.fromisoformat(rec.first_failed_at)).days
        acc_rows.append([rec.group, f"`{rec.id}`", rec.consecutive_failures,
                         f"{rec.first_failed_at} ({elapsed}d)",
                         "**escalated**" if k in esc_keys else ""])
    lines += [
        f"## Access failures ({len(access)}, {len(escalations)} escalated)",
        "",
        f"Our fetches of these sources have been failing. A source is **escalated** after "
        f"{escalate_runs} consecutive failed runs or {escalate_days} elapsed days since the "
        f"streak began, whichever comes first (ADR 0013). This is a fact about our access: "
        f"the tool cannot tell a block from a page that moved, and does not guess.",
        "",
    ]
    lines += _table(["group", "id", "consecutive failed runs", "failing since", ""], acc_rows)

    lines += [f"## Seeded this run ({len(seeded_now)})", "",
              "Sources that had no recorded baseline; the run recorded what upstream served "
              "so the next run can compare. They were not compared this run.", ""]
    lines += _table(["group", "id", "baseline now"],
                    [[r.group, f"`{r.id}`", f"`{r.new[:12]}…`"] for r in seeded_now])
    if accepted_now:
        lines += [f"## Baselines accepted this run ({len(accepted_now)})", "",
                  "The operator ran `--record-baseline=refresh`: the observed value is now "
                  "the baseline, without re-ingesting.", ""]
        lines += _table(["group", "id", "baseline now"],
                        [[r.group, f"`{r.id}`", f"`{r.old[:12]}…`"] for r in accepted_now])
    if uncompared or no_baseline:
        lines += [f"## Not comparable ({len(uncompared) + len(no_baseline)})", "",
                  "Fetched, but not compared: a `watch`-declared source whose body is not "
                  "JSON or lacks a declared path, or a source still without a baseline.", ""]
        lines += _table(["group", "id", "why"],
                        [[r.group, f"`{r.id}`", r.outcome] for r in uncompared + no_baseline])
    lines += ["## By group", ""]
    lines += _table(["group", "sources", "changed", "unchanged", "fetch failed", "no baseline", "other"],
                    [[g, c["total"], c["changed"], c["unchanged"], c["fetch_failed"],
                      c["no_baseline"], c["other"]] for g, c in sorted(by_group.items())])
    return "\n".join(lines).rstrip("\n") + "\n"


def write_report(root: Path, text: str) -> Path:
    path = root / REPORT_FILE
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if DRIFT.md differs from what the state files render to")
    args = ap.parse_args(argv)
    # Imported here: `changes` imports this module, and the thresholds live there.
    from corpus_toolkit.sources import changes
    config = config_mod.load(args.config)
    root = config.root
    state = load_drift_state(root)
    access = changes._load_access_failures(config)
    today = changes._utcnow_date()
    text = render_drift_md(state, access, changes._access_failure_escalations(access, today),
                           load_last_run(root), escalate_runs=changes.ACCESS_FAILURE_ESCALATE_RUNS,
                           escalate_days=changes.ACCESS_FAILURE_ESCALATE_DAYS, today=today)
    if args.check:
        current = (root / REPORT_FILE).read_text(encoding="utf-8") if (root / REPORT_FILE).exists() else ""
        # The render date line is the only thing that legitimately differs.
        strip = lambda s: "\n".join(l for l in s.splitlines() if not l.startswith("**Generated by"))
        if strip(current) != strip(text):
            print(f"{REPORT_FILE} is stale; re-run corpus-drift-report --config {args.config}",
                  file=sys.stderr)
            return 1
        print(f"{REPORT_FILE} is current")
        return 0
    write_report(root, text)
    print(f"wrote {REPORT_FILE}: {sum(1 for r in state.values() if r.outcome == 'changed')} "
          f"changed since baseline, {len(access)} access failure(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
