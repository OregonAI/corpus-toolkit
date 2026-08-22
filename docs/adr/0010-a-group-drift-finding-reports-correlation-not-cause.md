# A group drift finding reports correlation, not cause

A drift run files one issue per changed source. When a whole group changes at once the run
files twenty-five near-identical tickets or, once the budget is spent, none at all — and the
group with the most evidence behind it is the one reached last, so it gets nothing.
corpus-toolkit#69 fixed the starvation between groups and made this visible: in ERF's
2026-08-05 run, `deq` spends twenty of twenty-five slots describing one broken-URL fault
twenty times, while `oar` — 484 sources, 89% of the drift, one template-level cause — files
nothing at all.

We decided the run may file a **group drift finding**: one issue naming a group where every
compared source changed. It states that they changed **together**, and asserts nothing about
why.

## Considered options

**A finding that names the cause** was the tempting shape, and it is what corpus-toolkit#69's
own text asked for when it said the right outcome was "3 issues plus 2 bulk findings". It
earns the right to replace 484 tickets with one, because a ticket that explains the group
makes the individual tickets redundant. We rejected it because the tool cannot observe cause.
It observes that bytes moved. Three whole-group events are on record and they had three
different causes — a footer version bump, a set of URLs that stopped serving, and, in
oregon-counties, **no cause at all**: 3,447 of 3,447 sources "changed" because every baseline
was empty and nothing was ever compared (corpus-toolkit#68). A finding that asserted a shared
cause would have diagnosed an inert run with confidence.

So a group drift finding **accompanies** the individual tickets and never suppresses them.
That is the honest consequence of claiming only correlation, and it is also the safer one: a
genuinely independent change inside a bulk-drifting group keeps its own ticket, rather than
being buried by a finding that was never entitled to speak for it. Suppression would have
reproduced, one level down, the starvation #69 had just removed.

It follows that this does not solve the duplicate-ticket problem. `deq`'s twenty
near-identical tickets remain twenty. What changes is that `oar` stops being silent. Claiming
more than that would oversell a change that buys one specific thing.

**Every compared source, not most of them.** The trigger is 100%, because that is the only
threshold that is itself an observation. "Every source in this group changed" is checkable;
"more than eighty percent did" embeds a judgement about how much is a lot, and the twenty
percent that did *not* change are evidence against the very pattern the finding would assert.
All three recorded events were N of N. A group must also hold **more than one** compared
source: one source cannot corroborate itself, and the individual ticket already says
everything the group finding would.

**An uncompared source is not a changed source.** A group whose sources were never compared —
unseeded baselines, fetch failures — produces no group drift finding, because there is no
drift to report. corpus-toolkit#67 built the per-group breakdown precisely to separate those
two shapes, and both read as 100% to anything counting mismatches alone.

## Consequences

A group drift finding **consumes a slot from `MAX_ISSUES_PER_RUN`**. The cap exists so one run
cannot flood the tracker, and by ADR 0003 the tracker is the backlog mechanism: every finding
arrives as `needs-triage` and is somebody's work. Exempting these from the cap would mean the
cap is no longer the cap — a corpus with twenty-seven bulk-drifting groups would file
twenty-seven issues past a limit of twenty-five.

**Group findings are filed before individual tickets**, and the remaining budget then spends
smallest-drifting-group-first exactly as #69 decided. Those two orderings appear to conflict
and do not, because they order different things: there is at most one group finding per group,
so filing them first cannot flood anything, while leaving them in #69's order would put the
largest group last and file nothing for it — the very case this decision exists to answer.

**The title carries no counts.** `_open_issue` prevents re-filing by searching for its own
title, which works because `Source changed: <id>` never changes while the condition persists.
A title reading `Group drifted: oar (484 of 484)` would break that: a run finding 480 of 484
tomorrow writes a different title, the search misses, and the same unresolved condition files
again. The counts, the sample ids and the run link belong in the body.

What this does not settle is the duplicate tickets inside a bulk-drifting group. Twenty
tickets for one fault is still twenty tickets, and whether a group finding should eventually
earn the right to replace them is a question about evidence this project does not yet have —
specifically, whether a whole-group change ever has genuinely independent causes. Three
instances are on record and none does, but the absence of a counter-example is not the same as
its impossibility.
