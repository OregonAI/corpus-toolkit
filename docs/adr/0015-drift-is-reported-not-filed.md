# Drift is reported, not filed

**Status:** accepted · **Date:** 2026-09-03 · amends the *medium* of [ADR-0010](0010-a-group-drift-finding-reports-correlation-not-cause.md) and [ADR-0013](0013-a-persistent-access-failure-escalates-on-runs-or-days.md), not their claims

## Context

A drift run filed one GitHub issue per changed source, one `Group drifted:` finding per
group whose every compared source changed (ADR-0010), and one `Access failure:` issue per
source whose fetch had failed past a threshold (ADR-0013), capped at 25 a run. Measured on
2026-09-02 across the fifteen repos: **90 of 172 open issues were those tickets** — 32 in
oregon-counties, 29 in oregon-collective-bargaining, 25 in oregon-kpm already labelled
`wontfix` (2016 reports nobody will re-ingest), 3 in oregon-budget, 1 in ERF. One of the
ninety had ever been acted on. The 2026-08-12 triage pass (ADR-0003) had reached the two
worst repos and the tickets re-formed within a fortnight.

Meanwhile the run's two decisive steps were human commands in no workflow: seeding a
baseline (`--record-baseline`), without which every source "changed" against `""` and the
run exited 0 — three corpora ran that way for weeks — and merging the weekly chore PR that
carried the run's state, without which `access-failures.json` reset every run and the
ADR-0013 escalation could never fire.

The operator's position, recorded during the 2026-09-02 architecture review: *"I don't
really care about drift."* The report is wanted at exactly one moment — when someone is
about to re-ingest a corpus — and at no other.

## Decision

**A drift run files no issues.** It writes one rolling report per corpus, `DRIFT.md`,
rendered from `drift-state.json`: every source whose upstream differs from the baseline
this corpus mirrors and the date that was first observed; the groups whose every compared
source changed (ADR-0010's claim, in its words: together, and nothing about why); the
sources our fetches have been failing for, with the ADR-0013 threshold marked as
**escalated** (a fact about our access, never about upstream); what the run seeded or
accepted; and what it could not compare. The issue-filing code, its cap, its spend order
and its two labels are deleted, and the `ISSUE_TEMPLATE/source-change.md` files with them.

**The state is a snapshot, not a ledger.** `drift-state.json` holds the last observation of
every source, held for groups a run did not check and pruned for sources retired from
groups it did — the rules `access-failures.json` already follows. A changed source stays
changed until it is re-ingested and re-seeded or its baseline is accepted; `first_changed_at`
carries across runs while the baseline it changed from is unchanged. A per-run log would
grow without bound and bury state under history.

**Seeding is the run's job.** A source with no recorded baseline is seeded on the run that
first fetches it, listed under "Seeded this run", and compared from the next run on.
`--record-baseline=refresh` remains the one deliberate act: accepting an observed change as
the new baseline without re-ingesting.

**The chore PR merges itself.** Every corpus's `main` now carries required status checks
and no required reviews (ADR-0014), so the reusable workflow requests auto-merge on the
`chore/drift` PR that carries `DRIFT.md`, the state files, seeded manifests and STATUS.md.

**Red means the run could not do its job**, never that upstream changed: systemic fetch
failure, nothing in scope, a refused manifest rewrite, or a watched source that arrived
and still could not be compared. `RunVerdict` is the one place those reasons live.

**Monthly, everywhere.** Drift runs on the 1st of the month in every template-shaped
corpus; ERF keeps its monthly-on-the-5th plus quarterly sweep; full validation stays
weekly because the release canary relies on it. The reusable workflow takes a `groups`
input so ERF's bespoke per-group job folds back into it.

**The 89 open tickets close** with one comment each naming the corpus's `DRIFT.md`. The
one that carried human triage (ERF #113, a confirmed real upstream revision, `ready-for-agent`)
stays open. kpm's 25 `wontfix` sources are left in its `DRIFT.md` as changed: the report
states a true fact, and `wontfix` was about tickets, not the fact.

## Considered options

**One pinned issue per corpus, edited in place** — rejected. It keeps the report in the
medium that became backlog, and an issue is not diffable, versioned or reachable from the
corpus itself.

**A drift section inside STATUS.md** — rejected on mechanics. STATUS.md is `--check`-gated
in every corpus's CI, which requires it to reproduce locally; fetch results cannot.

**Keep filing `Access failure:` issues only** (they are about our fetches, not upstream) —
rejected. Two mediums for one fact drift apart, and the report needs the section anyway.

**Refresh kpm's 25 `wontfix` baselines so its report starts clean** — rejected. That records
"we accept the new upstream" for reports nobody read; the honest report costs nothing.

**A per-run ledger** — rejected, see above.

**Keep `--open-issues` as a deprecated no-op for one release** — rejected. A flag that is
accepted and does nothing is the silent no-op the platform's own rules forbid; argparse
rejecting it is loud, and the only bespoke caller (ERF's `monthly-drift`) is folded into
the reusable workflow in the same sweep.

## Consequences

ADR-0010 and ADR-0013 stand: a group finding still says "together" and nothing more; an
access failure still escalates on two runs or fourteen days and still says nothing about
upstream. What changed is where the sentence is written.

`changed-sources.tsv` and `source-outcomes.json` keep their contracts; ERF's bulletin
report reads the first. `DRIFT.md` is generated but **not `--check`-gated** — the one
committed generated file exempt from the template's rule, because it records observations
no local command can reproduce. `corpus-drift-report --check` exists for a corpus that
wants to assert the report matches the state files.

`corpus-detect-changes` joins the release gate: two locally served sources, seed, change
one, detect exactly that one, re-render from state. The largest module on the platform is
no longer the one the gate cannot see.

What this does not settle: a corpus whose `chore/drift` PR cannot auto-merge (a private
repo on this plan, or a red check) still starts its next run from whatever `main` last had.
The report says so in its "Last run" section, and the PR waits.
