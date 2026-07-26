# Civic Corpus Reference Architecture (v1)

The portable pattern behind `oregon-policy-repo`, generalized so any body of
public-sector knowledge can be stood up as a corpus repo + MCP server.

## Principles

1. **Non-authoritative mirror.** Every corpus is a trusted quick-access
   layer over official sources, never a source of truth. Disclaimers and
   authoritative-source links are mandatory at every level.
2. **Provenance or it doesn't exist.** No statement without a pinned
   source (URL + retrieval date + hash) or, for live data, a reproducible
   query + timestamp.
3. **Humans gate, agents labor.** AI agents discover, ingest, and draft;
   humans approve the source list and every merge.
4. **One interface.** Every corpus MCP server implements the same core
   tool contract (see mcp-interface-contract.md), so agents learn once.
5. **Git is the database.** Markdown + frontmatter + git history. Diffs
   are the audit trail; CI is the guardrail enforcement point.

## The three corpus archetypes

### 1. Document corpus
Sources are documents (statutes, rules, policies, schedules, ordinances).
- Content: full verbatim text mirrored into Markdown, one document per
  file, curator content confined to designated headings.
- Guardrail: CI diffs `## Full text` against a pinned source snapshot;
  scheduled re-fetch detects upstream drift by hash.
- Examples: oregon-policy-repo, records retention schedules, county/city
  policy sets.

### 2. API corpus
Sources are live structured-data services (OData, Socrata/SODA, REST).
- Content: the repo holds no mirrored records. It holds the MCP adapter,
  entity/schema documentation (one .md per entity), a query cookbook, and
  optionally small cached reference tables (code lists, lookups).
- Guardrail: verbatim-diff does not apply. Instead every answer the MCP
  server returns must carry the exact query executed and the execution
  timestamp. Entity docs are verified against the live API schema by a
  scheduled CI job (field drift opens an issue).
- Examples: legislature OData, data.oregon.gov datasets.

### 3. Hybrid corpus
Document layer + API layer joined by a mapping.
- Content: documents mirrored per archetype 1; live data per archetype 2;
  plus explicit join files (e.g., budget bill line item ↔ dataset keys).
- Guardrail: both regimes apply to their halves; join files are
  human-reviewed and CI-checked for referential integrity.
- Example: budget bills + expenditure actuals.

Every corpus declares its archetype in `_meta/corpus.yml`; the toolkit's
validators and MCP framework key off that declaration.

## Repo anatomy (all archetypes)

```
README.md          purpose + disclaimer + navigation table
AGENTS.md          canonical agent guide; hard anti-fabrication rules
DISCLAIMER.md      full non-authoritative statement
llms.txt           curated machine-readable index
CHANGELOG.md       Keep a Changelog + domain change types
_meta/
  corpus.yml       corpus config: id, jurisdiction, archetype, versions, siblings
  corpus-index.json     generated compact id→[title,doc_type,path] index siblings resolve against
  source-manifest.yml   every upstream source, recheck cadence, hashes
  templates/       document template(s)
  snapshots/       pinned source snapshots (document/hybrid)
<content dirs>     archetype-dependent
.github/workflows/ci.yml  → calls toolkit reusable workflows
```

## Naming and identity

- Repos: `<jurisdiction>-<domain>` (oregon-legislature, county-marion-policy).
- Document ids: stable citation-aligned slugs (`oar-166-300-0015`).
- Corpus ids: the repo name; used as the MCP server name and in
  cross-corpus citations.

## Cross-corpus citation resolution

`resolve_citation` on any server may return a **remote** resolution:
`{corpus: "oregon-policy-repo", id: "ors-276A.300", url: …}` for citations
it recognizes but does not hold. The registry (org profile README plus a
machine-readable `registry.yml` in the org `.github` repo, added when a
second corpus exists) maps citation schemes → owning corpus. This keeps
each corpus small while letting agents walk the whole graph.

Implemented in toolkit v1.1.0: each corpus publishes a compact
`_meta/corpus-index.json` (`corpus-generate-index`) — id → [title, doc_type,
path] and nothing else, because a real corpus's `_meta/graph.json` runs to
tens of megabytes and is unusable as a remote lookup target. A citing corpus
declares the corpora it cites under `siblings:` in its `corpus.yml` and marks
the owning corpus on the relevant citation schemes
(`register_scheme(..., corpus=...)`). Sibling indexes are fetched over plain
HTTPS and cached on disk for a day; an unreachable sibling degrades to an
explicit "could not check" (never a fabricated hit, and never an error that
takes the server down).

## Status layer

Each corpus generates a `STATUS.md` (freshness distribution, drift alerts,
coverage vs. manifest, CI pass rates) on a schedule. Org-level dashboard
aggregates these later; don't build the aggregate before three corpora
exist.
