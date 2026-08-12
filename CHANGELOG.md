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

Nothing yet.

## v1.25.0 — 2026-08-11

### One behaviour change on the serve path, then fixes

**Read this if your corpus sets `plugins.semantic_search_module`.** The shared semantic
module now resolves its embeddings artifact from `config.root`, not from the process's
working directory. In the containers those are the same path (WORKDIR is the repo root),
and `CORPUS_SEMANTIC_DIR` still overrides both, so a normal deployment is unaffected — but
it is the code path every semantic query runs, so rebuild deliberately rather than letting
it ride along on an unrelated image build. A corpus that builds its artifact with
`--out` still needs `CORPUS_SEMANTIC_DIR` set at serve time; that was true before and has
not changed.

Nothing else changes for a corpus. No `_meta/corpus.yml` edits, no schema change, no MCP
contract change. The four items below came out of an architecture review of the retrieval
and plugin seams (corpus-toolkit#73, #74, #75).

### Fixed

- **Citation schemes silently vanished on a second `CorpusFramework` over one corpus**
  (#73). `load_module` caches a corpus's citation module, so the second construction re-ran
  none of its top-level `register_scheme` calls and collected nothing — then fell back to a
  process-wide list the collector had deliberately bypassed, which was therefore empty.
  `resolve_citation` answered *"no citation scheme recognized this format"* about a corpus
  that recognizes it perfectly well, reported `schemes_attempted: []`, and skipped sibling
  resolution entirely — so a sibling citation came back `unresolved` with no
  `sibling_unavailable` marker. That is "could not check" served as "not there".

  **No deployed server hit this**: `server.py` builds one framework per process. It was
  reachable from a corpus's own `tools_module`, a CLI, or any multi-corpus process.

- **The semantic seam had no per-corpus state** (#74). The plugin contract passed no
  corpus, so the module read `Path.cwd()` for its artifact path and kept its loaded index in
  a module global — one installed module object shared by every framework in the process.
  A server started outside the repo root served keyword-only while reporting healthy, and
  two corpora in one process shared whichever index loaded first. The builder never had this
  problem (`semantic/build.py` has always written to `cfg.root/_meta/embeddings`), so the two
  halves of one artifact disagreed about where it lives.

- **`corpus_toolkit.semantic.search.selftest()` crashed** and had for some time. Its
  synthetic fixture was a 5-tuple while the loader had grown to 6 when `rank_chunks` added
  `rows`, so two of its four checks had not executed since — including the one guarding the
  degrade path `backends.py` calls with no `try`/`except`. It was written to run without the
  artifact specifically so CI could run it, and CI never called it. It does now, and `numpy`
  joins the `test` extra so the check runs rather than skipping.

### Added

- **`RetrievalBackend.holdings_for(slug)`** (#75) — optional, and the only optional member
  of the protocol. `issuing_body_profile` used to run raw SQL against `FileBackend`'s `docs`
  table through `ensure_index()`, so the tool was unavailable to any other backend **at any
  price**, and three separate guards existed to keep that from surfacing as a crash. A
  corpus-supplied backend can now serve the tool by implementing one documented method; the
  startup message says so when it does not. `FileBackend` implements it, so a
  document-archetype corpus does nothing.

- **`plugins.load_module(..., force=True)`** — re-executes a module already in
  `sys.modules`, for the case where the import *is* the effect. Keyword-only and off by
  default; only the citation-scheme collector passes it.

- **`corpus_toolkit.semantic.search.make(config)`** — the per-corpus factory
  `CorpusFramework` now prefers. A semantic module without it is duck-typed exactly as
  before.

### Internal

- `extract_section` is a module function in `backends.py`. `CorpusFramework._extract_section`
  called it as `FileBackend._extract_section(self, ...)` — an unbound method of an unrelated
  class handed a `CorpusFramework` as `self`, which held only while the body ignored `self`.
  Both classes keep the name.
- `DOC_*` column constants for `_doc_row`, and the existing `KW_*` applied to the
  `_doc_meta_row` readers that were still positional. `tests/test_row_offsets.py` executes
  the real queries and asserts each constant lands on the column it names, so a reordered
  `SELECT` fails there instead of serving a document with its citation in the `title` field.
- `REQUIRED_BACKEND_METHODS` moved next to the protocol it restates.
- First tests for `issuing_body_profile` — `issuing_body_registry` previously appeared
  nowhere in `tests/`.

Suite: 205 tests → 231.

## v1.24.1 — 2026-08-04

### Fixed — **v1.24.0 is broken; take this one**

**If you are pinned to v1.24.0, every object-shaped tool on your corpus is failing right
now.** Bump to v1.24.1 and rebuild the image; there is no config workaround, and rolling
back the pin to v1.23.x also clears it. A pin-bump PR should have been opened against your
corpus automatically — merging it is not enough on its own, the image must rebuild.

v1.24.0 declared a `TypedDict` output schema on the six object-shaped tools
(`get_document`, `resolve_citation`, `graph_neighbors`, `corpus_overview`,
`authority_chain`, `issuing_body_profile`). That made the SDK serialize every response
through a pydantic model, which broke two things at once (corpus-toolkit#61):

- **`authoritative_source: null` was rejected.** It is a documented value for a corpus
  that declares no source, so `corpus_overview`, `resolve_citation` and unknown-id
  `get_document` returned a hard `ValidationError` instead of a response.
- **Document bodies were dropped.** Keys the schema did not declare were discarded on the
  way out, so `get_document` returned its three envelope fields and no content —
  **silently**, with the call still reporting success. This is the dangerous half: an
  agent gets a well-formed answer containing nothing.

`search_corpus` was never affected (it returns a list and was deliberately left alone),
which is also the proof that no corpus content was lost — the failure was entirely in
response serialization.

Object tools now return `dict[str, Any]`: still a real output schema, but one that permits
`null` and strips nothing. Field-level validation of response convention 1 is reopened as
corpus-toolkit#15, and `docs/mcp-interface-contract.md` now records why the obvious
implementation is the one that must not be used.

## v1.24.0 — 2026-08-04

**Rebuild corpus images to pick this up.** The serve path changed: `server.py` now serves
the app it verified rather than letting the SDK build a second one. Behaviour is unchanged
for a server started without `--allowed-origin` — same uvicorn options, and
`forwarded_allow_ips` still defaults from the `FORWARDED_ALLOW_IPS` environment variable —
but it is the code path every corpus runs, so it wants a deliberate rollout rather than
riding along on the next unrelated rebuild.

Nothing here is breaking. No config change is required by any corpus.

### Added

- **CORS** via `--allowed-origin` (repeatable), so a browser MCP client can complete the
  handshake at all. Exposes `mcp-session-id`, without which the preflight passes,
  `initialize` returns 200, and the client dies on `Missing session ID` (#37).
- **`corpus-detect-changes --check-robots`** — report each source host's robots.txt
  position, including hosts that permit our agent while blocking named AI crawlers.
  Reports only; nothing blocks a fetch (#29). Found on its first run:
  `www.yamhillcounty.gov` carries `Content-Signal: ai-train=no` and already supplies five
  documents to oregon-collective-bargaining.
- **Output schemas** on the six object-shaped tools, so response convention 1's three
  fields are visible to schema-driven validation instead of only present in the JSON text
  (#15). Extras are still permitted — a schema that constrained each tool's payload would
  break every corpus.
- **`bump_pins.py`** — move every toolkit pin in a corpus, and `--check` to detect pins
  that disagree. Measured across 10 repos: 126 pin sites, six toolkit versions live at
  once, and one repo running two versions inside one `ci.yml` (#9). A `propagate-pin`
  workflow opens the bump PRs, and needs a `CORPUS_PIN_TOKEN` secret to do anything.
- **`corpus-build-semantic-index`** — the semantic arm was the only CLI without a console
  entry point, which also kept it outside the `entrypoints` CI job (#41).
- **`check-links` gains `exclude-urls`** for hosts quoted inside mirrored third-party text,
  where the link belongs to the source document and cannot be corrected (#51).

### Fixed

- `BIG_DOC_BYTES` was defined twice, 1,200 bytes apart, and `framework.py` shadowed its own
  import of it (#52).
- Drift issue creation discarded every `gh` failure. 618 creations failed silently because
  the `source-change` label did not exist — a hard dependency of `--label` that nothing
  created (#53). Now creates the label, reports failures, and caps a run at 25.
- **`pyproject` version was five releases stale** (1.18.0 against tag v1.23.0), so an
  install reported a version it was not. Now gated: a tag whose version disagrees is
  deleted (#41).

### Documentation

CHANGELOG.md now exists; MIGRATION.md covers v1.9.0–v1.23.0; `docs/semantic.md` documents
~730 LOC that had none; `AGENTS.md` states plainly that nothing enforces robots.txt.

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
