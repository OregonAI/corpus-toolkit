# A persistent access failure escalates on runs or elapsed days, whichever comes first

> **Medium amended by [ADR-0015](0015-drift-is-reported-not-filed.md), 2026-09-03.** The claim
> below stands in the same words; since v1.34.0 it is a section of `DRIFT.md`, not a GitHub issue.

`corpus-detect-changes` runs in default (non-`--strict`) mode so an isolated fetch failure
does not kill a run of thousands of sources, and the systemic guard exits 1 above 20% of
fetches failing. Between those two settings there was a band in which a source could fail
every run, forever, and nothing ever said so: `FETCH FAILED` prints on every run, but
nothing accumulated it across runs, so a failure thirty runs deep read exactly like one run
deep. The live instance was executive-regulatory-frameworks#140 — 45 sources on one host
began failing on 2026-08-05, at 3.3% of 1,347 checked, comfortably under the systemic
threshold. Twenty-two days and twenty-two `success` results passed with no drift detection
at all on those 45 (corpus-toolkit#166).

We decided a source escalates — one `Access failure:` issue, opened once — on
**2 consecutive failed runs, or 14 elapsed days since the first of those failures,
whichever comes first**. The rule was the operator's, decided on 2026-09-02 against the
measured data above, not derived here; this ADR records where it lives and why.

## Considered options

**Escalate on the first failure** was rejected outright. A 429 is transient by definition —
oregon-counties carries 9 of them from ecode360.com rate-limiting alone at the time of this
decision — and a ticket per throttle trains everyone to ignore the channel. That is how 90
open `Source changed:` issues across three corpora came to sit unread: noise habituates
readers faster than it informs them.

**Run-counting alone** (the shape of a withdrawn earlier attempt at this ticket,
`wip/166-access-failure-escalation`, which used 3 consecutive runs) was rejected because it
is cadence-blind in the wrong direction. Measured cadences: oregon-counties,
oregon-collective-bargaining and oregon-kpm run WEEKLY (cron `0 14 * * 1`);
executive-regulatory-frameworks runs MONTHLY. Under a pure run-count rule, reaching even 2
consecutive runs is ~14 days for a weekly corpus and ~30-60 days for a monthly one — a
slower cadence gets a LONGER blind window, which is backwards: the slower the cadence, the
more each missed run costs, not less. "This source has been unwatched for two weeks" is
true regardless of how often the cron that watches it runs, and a threshold that ignores
the clock cannot say it.

That is why the rule has a second, clock-only arm, and why that arm is evaluated
differently from the first: **`_access_failure_escalations` checks EVERY currently-tracked
access failure on every invocation, regardless of this run's `--group` scope**, not only the
sources this run happened to fetch. A monthly-cadence group's own next fetch is ~30 days
away; a rule that only looked at sources this run touched could never notice an
elapsed-days escalation before that fetch happens — by which point 2 consecutive runs has
usually already fired too, and the two arms would never be observed to disagree. Evaluating
the clock against every tracked record lets a WEEKLY group's own run notice a MONTHLY
group's overdue failure without waiting for that group's own cron to come around. A source
outside this run's scope that has NOT crossed either threshold is left exactly as it was
(`HELD`, in the code's own naming) — this is not a scan that touches every group's data,
only one that reads the clock against what is already recorded.

**Where the state lives** was the harder question, and the one the withdrawn attempt got
wrong. Three homes were on the table:

- **The source manifest.** Rejected. The manifest is CURATED data a human reviews in a PR
  (`_record_baselines`'s docstring is explicit about this), and a fetch-failure streak is
  machine bookkeeping that changes on every run — writing it there would put unreviewed
  noise inside a file whose whole discipline is that nothing lands in it without a human
  reading the diff.
- **The issue tracker itself as the state** (an open tracking issue's existence, or its
  body, standing in for the streak) was considered and rejected: escalation must not create
  ANYTHING visible before the threshold is crossed, and a source's first-ever failure has to
  be counted somewhere for a second failure to ever be recognized as consecutive. An issue
  is exactly the visible artifact this rule exists to gate behind a threshold, so it cannot
  also be the counter that decides when to create it.
- **A dedicated artifact, `access-failures.json`, at the corpus root** — sibling to
  `source-outcomes.json` (corpus-toolkit#160), written on every run that reaches the fetch
  loop, the same trigger as that artifact and for the same reason: a run that fails every
  fetch is exactly the run whose streaks must not be lost. This is what we built.

The withdrawn attempt used this same third home and still failed, for reasons that were
never about WHERE the state lives — they were about who commits it, and this decision does
not solve that half by itself. That attempt's own postmortem measured: a plain CI job on an
ephemeral runner discards a written-but-uncommitted file every run, and even the reusable
workflow's commit-to-a-branch-and-open-a-PR path only persists a run's writes if a human
merges that PR before the NEXT scheduled run — otherwise every run reloads an empty state
and rewrites all-ones, forever, at 1 run deep. Whether a corpus's OWN CI job commits its
working-tree changes is genuinely that corpus's call, not something corpus-toolkit's Python
entry point can answer on a corpus's behalf — but `.github/workflows/detect-upstream-changes.yml`
is not a downstream repo's CI; it is corpus-agnostic and lives here, so this decision does
reach it: its existing STATUS.md commit-and-PR step now carries `access-failures.json`
along, and a corpus that calls that reusable workflow gets this feature working, contingent
on merging that PR at least as often as the schedule runs, without writing any CI of its
own. A corpus that calls `corpus-detect-changes` from its own bespoke CI still owns that
commit step itself. Either way the tool refuses to pretend the state persisted when it did
not: `_load_access_failures` degrades to `{}` on anything unreadable — now with a stderr
warning naming what was lost, not silently — and the state is always WRITTEN, so a corpus
whose CI never commits it will find that fact staring back from an unchanged
`access-failures.json` in its own working tree the moment anyone looks — which is a
diagnosable, visible failure, not a silent one.

## Consequences

**Escalation shares `MAX_ISSUES_PER_RUN` with drift, and is spent LAST** — after group
drift findings and after per-source drift tickets, which keep the priority they already
had. This is deliberate, not incidental: the withdrawn attempt filed access failures FIRST
and, at ERF's own shape (45 failing, 30 drifted), found drift reporting stopped entirely
from the third run on, because an access failure recurs every run exactly the way
unaddressed drift already does. Putting access failures last cannot fully prevent the
reverse — a sufficiently large drift run can still leave no room for them, which is the same
`MAX_ISSUES_PER_RUN` capacity question already left alone for drift tickets, and moving the
cap is out of scope for corpus-toolkit#166 same as it was for corpus-toolkit#69 — but it
does mean this feature's existence never makes a corpus's drift reporting worse.

**A source outside `--group` scope, or already accounted for, is held rather than zeroed.**
ERF runs different `--group` sets from different crons; a source this run did not touch did
not just start succeeding. Conversely, a source retired from a group this run DID actually
enumerate is pruned from the state rather than kept asserting it is currently failing
forever — the withdrawn attempt's own postmortem named this gap explicitly (item #6, "no
pruning").

**The ticket says only what this tool knows.** It cannot tell a block from a 404 from a
document that moved, so the body states a count of consecutive failed runs and a count of
elapsed days and stops there — "this is a fact about our access, not a claim that it changed
or was removed upstream," the same framing corpus-detect-changes already uses for the
existing `failed sources` summary line. A ticket that drifted from that framing would assert
something this run cannot back with a measurement.

What this does not settle: **merging** the PR that carries `access-failures.json` is still
each corpus's own responsibility, same as `STATUS.md` and a `--record-baseline` manifest
diff already are — corpus-toolkit can commit the file to a branch and open the PR, but it
cannot merge it on a corpus's behalf, and a PR left open undercounts both escalation arms
for as long as it sits there. A corpus running its own bespoke CI instead of the reusable
workflow owns the commit step itself, not only the merge.
