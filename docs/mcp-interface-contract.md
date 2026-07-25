# MCP Interface Contract — v1

Every corpus MCP server implements the same core tools with the same
semantics, so an agent that learns one server can use them all. This
codifies (and generalizes) the tool set already live on the
oregon-policy-repo server.

## Core tools (mandatory, all archetypes)

### `corpus_overview()`
What this corpus contains and does not: counts by doc_type, jurisdiction,
coverage notes, archetype, freshness summary, and the non-authoritative
disclaimer. Agents call this first.

### `search_corpus(query, filters?)`
Full-corpus search. Filters: doc_type, status, tags, agency/issuing_body.
Returns ranked hits with id, citation, title, snippet, path.
- Document corpora: search over Markdown bodies + frontmatter.
- API corpora: search over entity docs, cookbook, and (where feasible)
  proxied live search endpoints — results must be labeled `source: live`
  vs `source: docs`.

### `get_document(id)`
Full document by id, including frontmatter. API corpora return entity or
dataset docs; live record retrieval goes through archetype extensions.

### `resolve_citation(citation)`
Map a citation string ("ORS 276A.300", "OAR 166-300-0015", "HB 2049",
"Schedule 166-300") to in-corpus id(s), or a **remote resolution**
`{corpus, id, url}` when the citation belongs to a sibling corpus per the
org registry. Never guess: unresolvable → explicit `unresolved` with the
schemes attempted.

### `graph_neighbors(id)`
All relationship edges of a node grouped by type, including remote
`corpus:id` edges.

## Document-corpus extensions

- `authority_chain(id, direction)` — walk implements/implemented_by up to
  statute or down to standards, cross-corpus edges included.
- `agency_profile(agency)` — who the agency is, what it holds in this
  corpus.

## API-corpus extensions

- `list_datasets()` — datasets/entities with descriptions and freshness.
- `query_dataset(dataset, query, limit?)` — parameterized live query
  (OData/SoQL). Responses MUST include `executed_query`, `executed_at`,
  and `endpoint`. Servers enforce read-only queries and result caps.

## Hybrid

Implements both extension sets plus `join_lookup(document_id | dataset_key)`
returning the mapped counterparts.

## Response conventions (all tools)

1. Every response carries `corpus`, `archetype`, and `authoritative_source`
   (URL) fields.
2. Document content responses include the provenance block (source_url,
   retrieved, source_sha256, last_verified).
3. Live-data responses include executed_query + executed_at.
4. A `disclaimer` field ("non-authoritative; verify at source") appears in
   corpus_overview and any response an agent is likely to quote from.
5. Errors are explicit, never silently empty: `unresolved`, `stale`
   (past recheck cadence), `schema_drift` (API shape changed since
   last_verified).

## Versioning

Contract version is declared in corpus.yml and reported by
corpus_overview. Additive tools/fields stay v1; changed semantics bump v2.
Servers must not remove core tools.
