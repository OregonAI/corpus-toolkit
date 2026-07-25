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
