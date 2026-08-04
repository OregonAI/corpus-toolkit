# Changelog

Release notes for `corpus-toolkit`, the shared platform every OregonAI corpus pins.

This file exists because `docs/reference-architecture.md` mandates a CHANGELOG in the repo
anatomy every corpus must follow, and `repo.py` hardcodes `CHANGELOG.md` into
`NON_CONTENT_NAMES` on the assumption it exists — while the toolkit itself had none
(corpus-toolkit#41). The de-facto changelog was `git log`, which is good prose but not
something a downstream can read when deciding whether to move a pin across 27 tags.

**Entries before v1.19.0 are back-filled from tag messages and merge commits.** They are
accurate about what shipped and deliberately terse; `MIGRATION.md` carries the upgrade
notes and the reasoning, and remains the file to read before moving a pin.

The audience is a corpus deciding whether a bump is safe, so each entry leads with whether
it can break you.

## Unreleased

- **`--check-robots`** on `corpus-detect-changes`: report each source host's robots.txt
  position, including hosts that permit our agent while blocking named AI crawlers.
  Reports only; nothing blocks a fetch (#29).
- **CORS** on the streamable-HTTP endpoint via `--allowed-origin`, so browser MCP clients
  can complete the handshake. Also serves the app `server.py` verified rather than letting
  the SDK build a second one (#37).
- **Fixes** — `BIG_DOC_BYTES` defined twice with different values (#52); drift issue
  creation discarding every `gh` failure (#53); `check-links` gaining `exclude-urls` (#51).

## v1.23.0 — 2026-08-03

`_sdk` spans the **client** side of the mcp 1.x/2.x break, not just the server side.

Six client-side breaks between majors — entry-point name, signature, which httpx it wants,
the arity it yields, and two silently renamed model fields. corpus-gateway crash-looped
four times discovering them one at a time. `open_client_streams`, `tool_input_schema` and
`result_is_error` now hide all six (#49).

## v1.22.0 — 2026-08-02

M4 integrity: `corpus-verify-extraction`, `source_data_file` provenance, fetch-failure
tolerance in `corpus-detect-changes`, and `corpus-verify` — the one tool that writes
`last_verified`/`verified_by`, which until then nothing on the platform could set.

## v1.21.0 — 2026-08-02

**Serve the inside of big documents.** `### ` subsections, chunk paging, chunk-aware search
hits. Before this, `get_document` on a 900 KB body was a glance-or-everything binary.

Corpora with anchored large documents need **at least this version** for those anchors to
be addressable at all — below it, `part=` cannot reach a subsection.

## v1.20.1 — 2026-08-02

`viz` slot guard requires a letter, so a bare `______` is treated as content.

## v1.20.0 — 2026-08-02

`corpus_toolkit.viz`: the shared chart chrome (M5-A).

## v1.19.0 — 2026-08-01

**Possibly breaking.** Archetype is enforced; `toolkit-ref` becomes required on the
reusable workflows. Also: status in index rows, multi-scheme matching, and per-corpus
`schema.doc_types` — a new instrument family no longer costs a toolkit release plus an
org-wide pin bump (#40).

## v1.18.0 — 2026-08-01

Declare `numpy` as the semantic extra, and say why the loader failed instead of degrading
silently.

## v1.17.0 — 2026-08-01

`corpus_toolkit.semantic`: semantic search any corpus can enable.

## v1.16.0 — 2026-08-01

`corpus_toolkit.site`: the shared landing-page shell, replacing eight copies of the same
chrome and the same cross-corpus contracts.

## v1.15.0 — 2026-08-01

Scheme-bug guard: a candidate id that is unresolvable by construction is reported as a
scheme bug rather than as a missing document. The two mean opposite things to a curator.

## v1.14.0 — 2026-08-01

`authority_chain` walks the relations a corpus declares, rather than a hardcoded set.

## v1.13.0 — 2026-07-31

The `performance_report` doc_type.

## v1.12.0 — 2026-07-30

The `federal_instrument` doc_type.

## v1.11.0 — 2026-07-30

Serve corpus-specific frontmatter on `get_document` (`extra_document_fields`).

## v1.10.0 — 2026-07-30

The `audit_report` doc_type.

## v1.9.0 — 2026-07-29

Contentless FTS5. The `fts` table no longer stores text, so reading its columns returns
NULL instead of raising — tests that inspected `fts.body` were asserting on an
implementation detail.

## v1.8.0 and earlier

See `MIGRATION.md`, which covers v1.0.3 through v1.8.0 with full upgrade notes.
