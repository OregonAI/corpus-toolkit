# ADR-0009 — The agency crosswalk is reference data, not a corpus

**Status:** accepted · **Date:** 2026-08-12

## Context

Budget, audits, KPM and ERF name the same agencies in different vocabularies — 0 of 40 audit
agency names exact-matched the 83 budget names. The crosswalk that reconciles them is the hub
behind every cross-corpus question involving an agency (ERF#83).

Today it exists in two halves in two corpus repos: `oregon-kpm/_meta/agency-crosswalk.yml` (96
agencies, human-confirmed) and `executive-regulatory-frameworks/_meta/{agency-graph.json,
agency-profiles.yml}`. Every corpus needs it; no corpus owns it; and a corpus that retires would
take half of it along, which is not hypothetical — `oregon-records-retention` has already been
folded and archived once.

## Decision

The crosswalk gets its own repository, as **reference data**: a schema, CI, and a published
artifact resolved over HTTP with a stale-cache fallback — exactly the shape sibling indices
already use. It is explicitly **not** a corpus: no `_meta/corpus.yml`, no MCP server, and it
does not appear in `list_corpora`.

## Consequences

A corpus mirrors an authoritative external source, and its entire discipline is proving
faithfulness to that source: snapshot hashes, verbatim line-order checks, coverage floors,
`last_verified`. The crosswalk has no upstream to mirror. Asserting that two agency name strings
denote one body is **our editorial judgement**, and running it through machinery built to prove
fidelity to a source would either leave that machinery unused or manufacture a provenance claim
nothing supports.

It also should not be offered to an agent asking which corpora it can search. A join table is
not a body of civic text, and `list_corpora` answering with one would be a category error at the
exact surface the platform works hardest to keep honest.

This is the platform's second repo kind, and `CONTEXT.md` names it so the next artifact of this
shape has somewhere to go without re-litigating.
