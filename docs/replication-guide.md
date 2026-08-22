# Replication Guide — Stand Up a New Corpus

Target: from zero to a CI-green corpus with its first documents ingested
and an MCP server answering, in roughly a day of focused work (excluding
large-scale ingestion).

## 1. Instantiate (15 min)
- "Use this template" on `corpus-template` → repo named
  `<jurisdiction>-<domain>`.
- Fill `_meta/corpus.yml`: id, display name, jurisdiction, archetype,
  schema_version: 1, contract_version: 1, toolkit pin (e.g. v1.0.0).
- Find/replace `{{CORPUS_*}}` placeholders in README, AGENTS.md, llms.txt,
  DISCLAIMER.md.
- **Replace `corpus.authoritative_source`**, which the template ships as
  `https://REPLACE-ME.invalid/where-the-official-text-lives`. It is NOT a
  `{{...}}` placeholder — it is URL-shaped so the template can validate
  itself — so the find/replace above will not catch it, and
  `corpus-validate-frontmatter` refuses both the placeholder and an absent
  value once this repo has a name and its first document
  (corpus-toolkit#11). Name the one page a reader opens to reach your
  official text; one URL is enough for a corpus spanning publishers.
- Set repo topics: `civic-corpus`, jurisdiction, archetype.
- Add row to the org registry README.

## 2. Discover (agent labor, human gate #1)
- Seed an agent with the authoritative index pages for the domain.
- Agent proposes `_meta/source-manifest.yml`: every candidate source with
  citation, title, URL, doc_type, recheck cadence (advisory — see below),
  why-relevant, and what
  it references outward.
- **Human approves/prunes the manifest via PR before any ingestion.**

  > `recheck:` is ADVISORY. The toolkit carries it through and never interprets it:
  > `corpus-detect-changes` checks every source on every run, and the cadence that
  > actually applies is the cron in the calling workflow. Write it to record intent,
  > not to configure behaviour — and set the cron to match. `corpus-detect-changes`
  > prints a NOTE when it sees the key.

## 3. Configure guardrails (30 min)
- CODEOWNERS: assign content dirs.
- Confirm `ci.yml` toolkit calls run green on the empty scaffold.
- Archetype API/hybrid: register endpoints in corpus.yml; run the
  schema-snapshot job once to baseline `live_schema_hash` values.

## 4. Ingest (agent labor, human gate #2)
- Document archetype: per approved source — fetch, snapshot, hash, convert
  full text per template, frontmatter, relationships; PR per knowledge
  body; human review sets last_verified/verified_by at approval.
- API archetype: write one entity_doc per entity from the live schema,
  build the query cookbook from real executed queries (paste actual
  responses' shapes, never invented ones).
- Hybrid: documents first, then dataset docs, then join files.

## 5. Serve (30 min)
- Enable the toolkit MCP framework with corpus.yml; deploy to the gateway
  host; add route.
- Smoke test the contract: corpus_overview, search_corpus, get_document,
  resolve_citation (including one deliberately unresolvable citation),
  graph_neighbors.

## 6. Verify (definition of done)
- [ ] CI green: frontmatter, provenance/schema-drift, links.
- [ ] An agent given only the MCP server answers a domain question with
      correct citation and no fabrication.
- [ ] Cross-corpus citation resolves (if applicable).
- [ ] CHANGELOG has the initial ingest entries; STATUS.md generates.
- [ ] Disclaimer visible in README, llms.txt, corpus_overview.
