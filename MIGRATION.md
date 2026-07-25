# Extracting the toolkit from oregon-policy-repo

Checklist for Phase 1 of PLAN.md.

## Move
- [x] `src/` validators → `corpus_toolkit/validate/` (frontmatter, provenance
      diff, link/reference integrity)
- [x] change-detection / snapshot code → `corpus_toolkit/sources/`
- [x] MCP server → `corpus_toolkit/mcp/` (tools per the interface contract)
- [x] frontmatter JSON schema → `schemas/document.frontmatter.v1.schema.json`
      (added schema_version/corpus/jurisdiction/snapshot_policy/snapshot_id
      per provenance-schema-v1.md)

## Genericize — grep for and eliminate:
- [x] Hardcoded paths (`agencies/department-of-administrative-services`, etc.)
      → read content roots from `_meta/corpus.yml` (`config.py`'s
      `content_roots`, with `scoped`/`subdirs` generalizing
      `DIR_DOC_TYPE`/`JURISDICTION_WIDE_DIRS`)
- [x] Oregon-specific citation regexes → pluggable citation-scheme registry
      (`mcp/framework.py`'s `register_scheme()`), populated by a corpus's
      `plugins.citation_module`; new corpora register schemes without
      touching the toolkit
- [x] Doc-type and authority-level enums → base set in schema, corpus.yml
      may extend

Deliberately NOT ported (corpus-specific, not generic — stays in
oregon-policy-repo, wired up via the extension points above in Phase 3):
ORS/OAR/EO/DAS citation regexes + OAR renumbering/ORS-disposition mining,
the authority-graph builder itself (`link_graph.py`'s citation-mining —
the toolkit only *consumes* `_meta/graph.json`), ITCS/ORS-chapter/OAR
snapshot-slicing (→ `plugins.snapshot_slice_module` hook), the
SharePoint-listing change checker, and the semantic/embeddings search layer.

## Package
- [x] `pyproject.toml`, console entry points:
      `corpus-validate-frontmatter`, `corpus-verify-provenance`,
      `corpus-detect-changes`, `corpus-generate-status`, `corpus-mcp-serve`
- [x] Reusable workflows call these entry points
- [x] Tag v1.0.0

## Prove
- [x] Smoke-tested against a scratch fixture corpus (Phase 1): all 5 entry
      points run correctly (validate-frontmatter full + --check-relationships,
      verify-provenance, detect-changes, generate-status), plus 10 direct
      checks of the MCP framework (search/get/resolve_citation/authority_chain/
      graph_neighbors/corpus_overview/issuing_body_profile), including both
      pass and deliberate-failure cases (bad relationship target, unregistered
      issuing-body slug, tampered hash)
- [ ] executive-regulatory-frameworks CI green using only toolkit workflows (Phase 3, in progress)

## v1.0.1 (Phase 3 — additive, see PLAN.md's "expect to iterate" framing)
- [x] `register_scheme()` accepts a `resolver` callable (candidate lookup
      beyond a flat id_template — renumbering maps, division expansion)
- [x] `corpus-detect-changes`/`corpus-generate-status` accept a directory of
      per-group source-manifest files, not just one flat file
- [x] `issuing_body_registry_key` made configurable (was hardcoded `entries`)
- [x] `validate-frontmatter.yml`/`verify-provenance.yml` branch on
      `github.event_name` themselves (PR/push `--changed`, else full) + a
      `jobs` input
- [x] `check-links.yml` gains an `exclude-paths` input
- [x] optional `semantic_search_module` plugin hook + reinstated `mode` param
      on `search_corpus` (RRF fusion when a provider is registered)

## v1.0.2 (surfaced while actually writing executive-regulatory-frameworks's
citation_schemes.py plugin — the OAR-division case needs the corpus's node
universe, and the OAR-renumbering/ORS-repealed cases need to attach an
informational note even when resolution SUCCEEDS, not just when it fails)
- [x] `resolver` may take `(match, nodes)` instead of just `(match)` —
      detected by arity, so existing 1-arg resolvers are unaffected
- [x] `resolver` may return `(candidates, note)` instead of a bare list —
      `note` is surfaced whether resolution succeeds or not, overriding the
      generic unresolved message

## v1.0.3 (also surfaced writing executive-regulatory-frameworks's corpus.yml
— oregon has 17 per-group source-manifest files needing schema validation,
not one)
- [x] `extra_schema_checks`' `path` may be a glob (e.g. `_meta/sources/*.yml`)
      to validate many files against the same schema, not just one exact path

## v1.0.4 (a real correctness bug, found testing `issuing_body_profile`
against the actual repo, not the fixture — DAS returned "no documents
ingested" despite having 300+)
- [x] Oregon's frontmatter has BOTH `agency` (the registry/path-scoping slug,
      e.g. `department-of-administrative-services`) and `issuing_body` (a
      free-text descriptor, e.g. "DAS Enterprise Information Strategy and
      Policy Division" — a *sub-unit*, not the same string). The toolkit's
      FTS index only stored the frontmatter `issuing_body` value, so
      `issuing_body_profile`'s stats join (which needs the registry slug)
      silently matched zero rows for every real agency.
- [x] Added `config.scope_slug_for(rel_parts)` (path-derived, independent of
      any frontmatter field) and a new `issuing_body_slug` FTS column sourced
      from it; `issuing_body_profile` now joins on that instead of the
      free-text `issuing_body` field. `search_corpus`'s existing
      `issuing_body` filter is untouched (still frontmatter-sourced — still
      correct for corpora without this two-fields distinction).

## v1.0.5 (bundles two things found running the real 69k-file corpus through
`corpus-validate-frontmatter` for the first time, plus writing
executive-regulatory-frameworks's check-links.yml call)
- [x] **Real schema bugs**, found because the fixture never exercised the
      full field/conditional surface real content hits: `effective_date`/
      `last_reviewed`/`source_version` must allow `null` (oregon's real data
      has thousands of nulls — the Phase-1 generalization dropped the
      original schema's nullable typing); the `content_mode` verbatim-
      required conditional needs the `content_exception`/`migration_pending`
      exemption the original schema had (missing it flagged 39 legitimately-
      excused documents); `source_format`'s enum was missing `xls`/`xlsx`/
      `docx`. All three were "0 errors on the fixture, ~172k errors on the
      real corpus" until fixed — a strong argument for testing against real
      data, not just a synthetic fixture, before calling a schema change done.
- [x] `check-links.yml` gains an `accept-codes` input (lychee's own
      comma-separated codes/ranges syntax; oregon's own workflow deliberately
      accepted 403 and the full 200-299 range, the toolkit's default was
      narrower), default unchanged
- [x] `check-links.yml` always also scans `llms.txt` (part of the standard
      repo anatomy per every archetype, not just `**/*.md`)
