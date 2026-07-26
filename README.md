# corpus-toolkit

The shared, versioned core of the OregonAI civic corpus platform: schemas,
reusable CI workflows, validation tooling, and the MCP server framework.
Corpus repos pin a tagged version of this repo — nothing corpus-specific
lives here.

## Contents

| Path | What |
|---|---|
| `docs/reference-architecture.md` | The portable pattern: principles, three corpus archetypes, repo anatomy |
| `docs/provenance-schema-v1.md` | The frontmatter metadata standard |
| `docs/mcp-interface-contract.md` | The tool vocabulary every corpus MCP server implements |
| `docs/replication-guide.md` | Stand up a new corpus in a day |
| `schemas/` | Machine-enforced JSON Schemas |
| `.github/workflows/` | Reusable workflows (`workflow_call`) corpus repos invoke |
| `corpus_toolkit/` | Python package: validators, provenance diff, change detection, MCP framework (extracted from oregon-policy-repo `src/`) |
| `tests/` | `python3 -m unittest discover -s tests` |

## Versioning

Semver tags. Corpus repos pin tags (never `main`). Breaking changes to the
schema or MCP contract bump the major version with a migration note in the
relevant doc.

## Status

- [x] `corpus_toolkit/` package extracted from oregon-policy-repo (Phase 1)
- [ ] v1.0.0 tagged (needs a real GitHub repo — see MIGRATION.md)
