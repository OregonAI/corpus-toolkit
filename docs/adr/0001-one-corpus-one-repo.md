# ADR-0001 — One corpus is one repo

**Status:** accepted · **Date:** 2026-08-12

## Context

Eight live corpora, each carrying the full skeleton: `Dockerfile`, ~8 workflows, `_meta/`,
`CHANGELOG.md`, `CONTRIBUTING.md`, `DISCLAIMER.md`, `LICENSE`, `STATUS.md`, `llms.txt`,
`renovate.json`. The obvious objection is duplication, and the obvious alternative is grouping
several small corpora into one repo.

Measured at the time of the decision, open issues clustered rather than spread: 30 in
`oregon-collective-bargaining` (nine days old), 27 in `oregon-kpm`, 16 in `federal-reference`,
14 in `corpus-toolkit` — 86 of roughly 107 org-wide.

## Decision

Keep one corpus per repo. Treat repo count as **not** the cost.

## Consequences

The isolation is what made the `oregon-records-retention` retirement clean: fold into ERF,
serve a tombstone, archive the repo, zero provenance mismatches, and a rollback that is one
repository. Grouping corpora trades that away to save duplication that is generated from a
template anyway.

The real cost is issue mass without an owner, which is a different problem with a different
fix — see [ADR-0003](0003-triage-labels-are-the-backlog-mechanism.md).

Growth is therefore expected to keep adding repos. That makes the correctness of any
org-wide *list* of corpora load-bearing, which is [ADR-0004](0004-two-kinds-of-corpus-list.md).
