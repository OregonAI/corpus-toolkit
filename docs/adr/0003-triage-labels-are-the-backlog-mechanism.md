# ADR-0003 — The existing triage labels are the backlog mechanism

**Status:** accepted · **Date:** 2026-08-12

## Context

86 of roughly 107 open org issues sat in four repos. The tempting read is that the platform
needs a triage process. It already has one — `docs/agents/triage-labels.md`, five canonical
roles — and coverage split cleanly:

| Repo | Open | Carrying a triage label |
|---|---|---|
| federal-reference | 16 | 16 |
| corpus-toolkit | 14 | 14 |
| executive-regulatory-frameworks | 8 | 8 |
| oregon-collective-bargaining | 30 | 5 |
| oregon-kpm | 27 | 1 |

The three repos at 100% are where the 2026-08-01 review's findings landed. The two near zero
are the ones that grew fastest afterwards.

## Decision

Do not invent a cadence. Apply the mechanism that is already at 100% in three repos: one
triage pass over `oregon-kpm` and `oregon-collective-bargaining`, then `needs-triage` as the
default on new issues so a backlog cannot silently re-form.

## Consequences

This is what makes [ADR-0001](0001-one-corpus-one-repo.md) sustainable: if repo count is not
the cost, unowned issue mass is, and it needs a named mechanism rather than periodic alarm.

A label default is deliberately weaker than an SLA. The platform's own convention is that a
gate which fires on noise gets deleted; a triage label that merely makes state *visible* has no
noise floor to trip.
