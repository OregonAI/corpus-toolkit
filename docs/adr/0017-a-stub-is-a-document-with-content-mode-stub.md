# ADR-0017 — A stub is a document with `content_mode: stub`

**Status:** proposed, **awaiting the owner's confirmation** · **Date:** 2026-11-10 · decided provisionally during an unattended build of corpus-toolkit#213; the issue left this question to the owner and the owner was asleep. Implemented, but cheap to reverse until confirmed (see Consequences).

## Context

`states-it-governance` knows of instruments that exist and are not public, or whose official
source refuses a fetch (California's administrative rules sit behind Westlaw; some state
security standards are unpublished). The corpus should hold a **stub**: frontmatter,
citation, link and the reason, with no text — and nothing may stand in for the text, since
unofficial copies are excluded by the spec.

At v1.36.4 that is not representable. A trial `policy` with `content_mode: summary` and no
snapshot failed the schema (`authority_level`, `retrieved`, `source_sha256` required;
`summary` not valid for a `policy`) and the provenance gate (`missing source snapshot`). The
point of the stub is also the one a search cannot serve: an agent needs to tell "no
provision found" from "not in the corpus", and a third answer, "exists, not public".

Issue #213 offered three representations: a new `content_mode`, a new `status`, or a
seed-list-only record that `coverage` reads from the manifest.

## Decision

**A stub is a document (a Markdown file with frontmatter) whose `content_mode` is `stub`.**

- **Schema.** `stub` joins the `content_mode` enum. A stub requires `stub_reason` (one line,
  non-empty) and may not carry `source_sha256`, `snapshot_id` or `snapshot_policy`.
  `retrieved` and `authority_level` are not required of it. The rule that forces those
  fields on the document-bearing `doc_type`s is skipped for `content_mode: stub` only, and
  the `external_reference` rule that forces `summary` is skipped likewise; both are
  otherwise untouched, so a non-stub document validates exactly as before.
- **Provenance gate.** No snapshot is required (there is none). In place of verification the
  gate refuses text: a stub with a `## Full text` section, a `[VERBATIM]` block or a body over
  `STUB_MAX_BODY_CHARS` (1000) is an error. An unverified paraphrase is precisely what a stub
  must not hold, and without this check `content_mode: stub` would be a way to skip the gate.
- **Serving.** `get_document` and `resolve_citation` answer for it, flagged `stub: true`.
  It is excluded from keyword search, semantic candidates, `compare` and the semantic
  build, so it is never a passage. `corpus_overview` counts it under
  `content_mode.stub`. A new optional tool `coverage` reports documents and stubs per
  jurisdiction and tag.
- **No index change.** The stub's `content_mode` is already an indexed column, so
  `SCHEMA_VERSION` does not move and no corpus rebuilds its index.

## Alternatives rejected

**A new `status`.** `status` says which version of the text a document is and whether it is
in force (corpus-toolkit#159). A stub is not a version of anything and says nothing
about force; adding a value would also make every consumer that treats "not `current`" as a
warning (the sibling index does, #25) report a stub as a defective or out-of-force text.
`content_mode` is the field that already says what we hold of the instrument.

**A seed-list-only record that `coverage` reads from the manifest.** It keeps the schema
untouched but makes the stub invisible to `get_document` and `resolve_citation`, which is the
"exists, not public" answer the stub is for; an agent resolving "WA SEC-1" would get
`unresolved`, the same words as for a citation that does not exist. It also splits the
corpus's holdings across two places the toolkit would have to join, and the toolkit has no
seed-list concept (that is a corpus-side artefact).

## Consequences

- **Contract.** Additive and stays v1: new enum value, new optional tool (`coverage`, name
  reserved), new fields only on a stub's responses. The eight live corpora are single
  jurisdiction, hold no stubs and see no change except `compare` (#212) and `coverage`
  appearing on their servers.
- **A stub in a `doc_type` that must be verbatim** (`statute`, `policy`, ...) is the one
  place the `content_exception` escape hatch is not used: the exemption is by
  `content_mode`, not by an excuse. That is deliberate and should be confirmed: it means a
  corpus can hold `doc_type: policy` stubs and real policies side by side and the gate
  tells them apart by `content_mode` alone.
- **`in_corpus` in `compare`** counts a jurisdiction held only as stubs as held (stubs are
  documents). `coverage` is where a caller reads how much of it is stubbed.
- **If the owner prefers another representation**, the cost is bounded: `stub` is one enum
  value and one gate branch, no index or contract-version change, and no live corpus uses it.
- **Open for the owner.** The body cap of 1000 characters and the single-line `stub_reason`
  are judgement calls, not derived from anything.
