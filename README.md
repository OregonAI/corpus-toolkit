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
| `docs/semantic.md` | Optional vector search: the artifact, the plugin contract, and three sharp edges |
| `CHANGELOG.md` | What each release changed, for a corpus deciding whether a bump is safe |
| `corpus_toolkit/schemas/` | Machine-enforced JSON Schemas (inside the package, so an installed toolkit carries them) |
| `.github/workflows/` | Reusable workflows (`workflow_call`) corpus repos invoke |
| `corpus_toolkit/` | Python package: validators, provenance diff, change detection, MCP framework (extracted from oregon-policy-repo `src/`) |
| `tests/` | `python3 -m pytest` — several files use pytest fixtures that `unittest discover` cannot execute |

## Versioning

Semver tags. Corpus repos pin tags (never `main`). Breaking changes to the
schema or MCP contract bump the major version with a migration note in the
relevant doc.

## Status

- [x] `corpus_toolkit/` package extracted from oregon-policy-repo (Phase 1)
- [x] v1.0.0 tagged; see `CHANGELOG.md` for releases and `MIGRATION.md` for upgrade notes
