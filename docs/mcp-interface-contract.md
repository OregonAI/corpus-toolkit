# MCP Interface Contract — v1

Every corpus MCP server implements the same core tools with the same
semantics, so an agent that learns one server can use them all. This
codifies (and generalizes) the tool set already live on the
oregon-policy-repo server.

## Core tools (mandatory, all archetypes)

### `corpus_overview()`
What this corpus contains and does not. Agents call this first. Fields:
`corpus`, `archetype`, `authoritative_source`, `jurisdiction`, `disclaimer`,
`documents_by_type`, `content_mode` (counts by verbatim/summary/mixed),
`commit`, `graph_edges`, `contract_version` — plus whatever the corpus's
retrieval backend adds (an API corpus reports its live endpoints here).

### `search_corpus(query, doc_type?, issuing_body?, limit?, mode?)`
Full-corpus search. Returns a ranked **list** of hits with `id`, `title`,
`citation`, `doc_type`, `issuing_body`, `path`, `snippet`.
`mode` is `hybrid` (default: BM25 + semantic fused by RRF), `keyword`
(BM25 only) or `semantic`; hybrid/semantic degrade to keyword when the
corpus configures no `semantic_search_module`. `limit` is capped at 40.
- Document corpora: search over Markdown bodies + frontmatter.
- API corpora: search over entity docs, cookbook, and (where feasible)
  proxied live search endpoints — results must be labeled `source: live`
  vs `source: docs`.

**`issuing_body` takes EITHER of the two things a body is called, and the
response says which one matched** (toolkit >= this release, corpus-toolkit#131).
This page did not say which of the two the parameter wanted, and the filter was
an exact match on the free-text `issuing_body` **frontmatter** field — a
human-written descriptor that frequently names a sub-unit ("DAS Enterprise
Information Strategy and Policy Division") rather than a registry entry.
Every *other* tool that takes a body takes a **registry slug**:
`issuing_body_profile(slug_or_query)` resolves one and `documents_by_agency(slug)`
requires one. So a caller holding a slug passed it here and got `[]` — which is
indistinguishable from "this corpus holds nothing for that body", and is worse
for an agent than for a person: an agent that has just resolved a slug has every
reason to reuse it, no signal that this one parameter wants a different kind of
string, and an empty result that reads as a finding.

Resolution, and it is by **identity, never by hit count**:

| the value | filtered on |
|---|---|
| names an entry in this corpus's issuing-body registry | the **resolved slug** (`config.registry_slug_for`'s answer — the same attribution `documents_by_agency` serves) |
| anything else | the free-text `issuing_body` frontmatter field, exactly as before |

Deciding by "did it return anything" would make the same call mean different
things on different days and would collapse the distinction this fix is about: a
slug naming a real body that holds nothing here would fall through and be
reported as a string that matched nothing.

**A value that is BOTH a registry slug and some document's frontmatter string
resolves to the slug.** That is a decision, not a default: the slug is the
identity every other tool takes, so a caller holding one got it from a
slug-shaped tool. It is not silent — the hit says `matched: "registry_slug"` —
and there is deliberately no parameter to force the other reading, since a
frontmatter descriptor is prose and a slug is lower-hyphen-case, so the overlap
is pathological rather than routine.

**Every hit gains `issuing_body_filter`** when the parameter is used, and only
then — a search that passes no `issuing_body` is unchanged, key for key:

    {"value": <as passed, stripped>,
     "matched": "registry_slug" | "issuing_body",
     "registry_checked": true | false,       # was the registry consulted at all?
     "note": "..."}                          # ONLY when the answer is qualified:
                                             #   the registry could not be consulted, or
                                             #   this backend cannot filter by slug

`registry_checked` is a second field because `matched` cannot carry two answers.
`matched: "issuing_body"` **with** `registry_checked: true` means checked, and
the value is not a registry **slug**. The same `matched` with
`registry_checked: false` means the question was never asked — this corpus
declares no registry, or declares one that is not there to read (the `note` says
which, because a broken path is a fault and no registry is a choice). Serving
the second as the first would tell a caller its slug is wrong on every corpus
that has no registry to be wrong against — the collapse `slug_in_registry: null`
already exists to prevent below. A registry that IS present and does not parse
reaches neither: it raises out of the config read before the filter resolves
(corpus-toolkit#136).

**"Not a slug" is not "not a body", and only the first is ever claimed.**
`issuing_body_profile` also takes a registry **name**, so a caller may hold one.
A name reaching `search_corpus` is filtered as frontmatter text and reported as
such; it is deliberately not resolved to its slug, because a registry name and a
document's free-text `issuing_body` overlap routinely — "Employment Department"
is plausibly both — so resolving names would hijack the reading that already
works for the callers who were always right. Turning a name into a slug is
`issuing_body_profile`'s job, and it hands back a slug to pass here.

The value is **stripped once** before either match, and the stripped value is
what is filtered and echoed — the same rule `documents_by_agency` applies to its
slug, and the reason a padded value no longer misses a row the corpus does hold.

**A body-filtered search with no matches never answers with a bare `[]`.** It
answers with exactly one record that is **not a hit** — no `id`, no `path`, no
`snippet` — carrying `no_hits: true`, the `query`, the same
`issuing_body_filter` block, and a `note` naming every filter that was applied:

    [{"no_hits": true, "query": "...", "issuing_body_filter": {...},
      "note": "no document matched ... The body filter was applied to ..."}]

`search_corpus` is the one tool with no envelope to put a note in (see the
exemption under response convention 1), so an empty list is the whole answer —
and an empty list cannot say which of two columns it looked in. The note says
what was searched for; it never says the corpus holds nothing for that body,
which is a different claim and one this tool is not in a position to make.
An unfiltered search that matches nothing still returns `[]`.

**What this asks of a backend: nothing.** `RetrievalBackend.search` may
additionally accept a keyword-only `issuing_body_slug`, and the framework passes
it only to a `search` whose signature **names** it. An adapter written before
this existed keeps working untouched and its frontmatter answer is reported as a
frontmatter answer — `matched: "issuing_body"` with a `note` naming the backend
— rather than relabelled a slug answer. `**kwargs` does not count as accepting
the keyword: a backend that swallows it returns an *unfiltered* result, and an
unfiltered result labelled "filtered by slug" is a wrong answer rather than a
missing one. The built-in `FileBackend` implements it, so a file-backed corpus
needs to do nothing.

**Everything above is additive except one shape change, named here rather than left to be
discovered**: a search filtered by `issuing_body` that matches nothing returns that
one-element list instead of `[]`. A search that matches, and any search that passes no
`issuing_body`, is unchanged key for key. Contract v1 covers the additions; the shape
change is a semantics change on one path of one tool, and whether it is worth a v2 —
which every corpus and the gateway would have to move together — is a release decision
recorded with the tag, not one this page makes on its own.

### `get_document(id, part?)`
Full document by id, including frontmatter and the provenance block. API
corpora return entity or dataset docs; live record retrieval goes through
archetype extensions.

A document body over 50 KB is **not** returned whole under `part="auto"`:
the response carries `at_a_glance`, a `sections` list of the document's
`## ` headings, and a `note` — pass `part="<heading>"` (or `part="full"`)
to page the content in. An unknown id returns `error` plus `did_you_mean`
suggestions rather than an empty document.

Three further `part=` forms make large instruments navigable (v1.21.0):

- **`subsections`** — when a body carries `### ` headings, the big-doc auto
  response also lists them, and `part=` accepts one **prefix-matched,
  case-insensitively** (`part="SEC. 188."` finds `### SEC. 188.
  NONDISCRIMINATION.`; the span runs to the next heading of the same or higher
  level). An ambiguous prefix is an ERROR listing the matches — never a guess.
- **`part="chunk:N"`** — the Nth embeddable chunk of the document, recomputed by
  the same deterministic chunker the semantic index was built with (no stored
  offsets, nothing to drift). Ordinals are 0-based and per-document.
- **search hits carry `chunk`** — when the semantic module provides
  `rank_chunks`, each hit includes the best-matching chunk's `ordinal`,
  `heading`, a 200-char `preview`, and the exact `part="chunk:N"` fetch string.
  A 900 KB statute stops collapsing to a bare id: the hit says WHERE the match
  lives and how to page it in.

### `resolve_citation(citation)`
Map a citation string ("ORS 276A.300", "OAR 166-300-0015", "HB 2049",
"Schedule 166-300") to in-corpus id(s), or a **remote resolution**
`{corpus, id, url}` when the citation belongs to a sibling corpus per the
org registry. Never guess: unresolvable → explicit `unresolved` with the
schemes attempted.

**A scheme's pattern may be a string or an already-compiled pattern, and a
compiled one is used as it is.** A corpus registers its formats with
`register_scheme(name, pattern, ...)` from its `plugins.citation_module`; a
string is compiled with no flags, so inline `(?i)` applies and nothing else
does. Pass the **compiled object** whenever the pattern carries flags —
`register_scheme("eo", EO_C)`, not `register_scheme("eo", EO_C.pattern)` — as
`re.compile()` over a pattern's source text keeps none of them and the loss is
silent: the scheme registers, the server starts, and citations stop matching in
whatever way the flag governed (`resolve_citation("executive order 23-04")`
`unresolved` while `"EO 23-04"` resolves — a difference of case reaching an
agent as a difference of content). Additive: every string call behaves exactly
as before. A compiled *bytes* pattern is refused at registration, because it
could only ever raise on a citation str.

Remote resolution (toolkit v1.1.0, additive — still contract v1): a
citation scheme registered with `corpus="<sibling id>"` resolves against
that sibling's compact `_meta/corpus-index.json` (see below) instead of the
local graph. Those matches carry `corpus` and `url` alongside
`id`/`title`/`doc_type`, and the response is tagged
`resolved_via: "sibling:<id>"`. Three outcomes must stay distinguishable:

| Outcome | Response |
|---|---|
| found in the sibling | `matches` with `corpus`+`url`, `resolved_via` |
| sibling consulted, no such document | `unresolved`, note says the sibling holds no such id |
| sibling could not be consulted | `unresolved` + `sibling_unavailable: "<id>"` — "could not check", **not** "does not exist" |

A sibling served from an expired cache still resolves, flagged
`sibling_index_stale: true`. An unreachable sibling degrades resolution and
never errors the tool.

### `corpus-index.json` (the cross-corpus lookup surface)
Every corpus that siblings cite publishes a compact index —
`{corpus, contract_version, n_documents, documents: {id: [title, doc_type,
path]}}` — generated by `corpus-generate-index`. Siblings resolve against
THIS, never `_meta/graph.json` (tens of MB on a real corpus).

**Published, not committed.** This page said "and committed", which
contradicted the workflow that actually ships it:
`.github/workflows/publish-index.yml` builds it into `site/` at deploy time
and commits nothing, deliberately — a committed index is a generated file
that can silently fall behind its own corpus, and that failure shows up in
someone ELSE's repository, as a sibling resolving a citation to a stale
title or missing a document that exists. So a sibling's `index_url` should
point at the published artifact
(`https://ORG.github.io/<corpus>/corpus-index.json`).

`corpus-generate-index --check` and the `_meta/corpus-index.json` default
output path remain, for a corpus that has a reason to commit one; if you
do, gate it in CI, because nothing else will.

### `graph_neighbors(id)`
All relationship edges of a node grouped by type, including remote edges.

A **local** neighbour is `{id, title, doc_type}`. A neighbour whose edge
target is not a node in this corpus — a citation string held by a sibling —
is `{citation, external: true}`, enriched with `{id, title, doc_type,
corpus, url, resolved_via: "sibling:<id>"}` when the sibling index resolves
it, and marked `sibling_unavailable: "<id>"` when the sibling could not be
consulted. External targets are the normal case for a corpus whose value is
citing a sibling, not an edge case: they must never error the tool.

**A relation name becomes a response key verbatim.** Each edge type found in
the graph is returned under its own key, so `implements` arrives as
`implements`. Five names are therefore RESERVED and a graph may not use them
as relation types: `corpus`, `archetype` and `authoritative_source` (response
convention 1's envelope) plus this tool's own `id` and `title`. A graph
declaring one gets the fourth row below rather than a response with the
colliding field overwritten (corpus-toolkit#105) — silently for `id`/`title`,
and as a hard serialization error for the envelope fields.

The relation types in use across the platform are `references_external`,
`related`, `supersedes`, `implements` and `implemented_by`; none collides.
The frontmatter schema's `relationships` block is closed over that set, so
only a corpus's own graph builder can introduce a collision.

The graph is a separate artifact from the corpus, so four conditions stay
distinguishable and none of them is reported as another:

| Condition | Response |
|---|---|
| the corpus has no graph at all | `no_graph: true`, error names the graph |
| the graph exists but predates this document | `not_in_graph: true`, error says rebuild |
| there is genuinely no such document | `error: "no document with id '<id>'"` |
| the graph declares a reserved relation name | `error` names the relation, `note` names the reserved set and the graph file |

The fourth is reported by **this tool only**. `corpus_overview`,
`resolve_citation` and `authority_chain` share the graph loader but cannot
have a key displaced by a relation name — `authority_chain` returns every
configured relation under a `up_`/`down_` prefix — so they keep answering. An
error surfaced by a tool that could not have been affected would report a
condition other than the one that occurred, which convention 5 forbids.

`graph_neighbors` stays registered even when a corpus has no graph — it is
a core tool, and "servers must not remove core tools" (see Versioning). A
corpus without a graph answers the first row above.

## Document-corpus extensions

- `authority_chain(id, direction, depth?)` — walk implements/implemented_by
  up to statute or down to standards, cross-corpus edges included. Same
  neighbour shape and same three graph conditions as `graph_neighbors`.
  Registered for the `document` and `hybrid` archetypes.

  `implements` / `implemented_by` are always walked, and return under
  `up_implements` / `down_implemented_by`. A corpus **may declare further
  relations** in `mcp.authority_relations`; each returns under its own key
  (`up_<name>` / `down_<name>`) and is **never merged into the implements
  result**:

  ```yaml
  mcp:
    authority_relations:
      up: {cites: [references_external]}    # -> up_cites
  ```

  The separation is the point, not a formality. `implements` asserts that this
  document implements that one; `references_external` records only that it
  cites it. A county ordinance citing ORS 215.203 is usually implementing it,
  and *usually* is not a fact — so a corpus that records citations must not
  have them served back as implementation claims. **Reading `up_cites` as an
  authority relation is a misreading of the data, not of this contract.**

  Declaring nothing leaves the response byte-identical, so this is additive for
  every corpus that predates it. Configured relations obey the same
  external-frontier rule: an external neighbour is enriched from the sibling
  index but never extends the walk, so a cross-corpus chain is one level deep.
- `issuing_body_profile(slug_or_query)` — who the issuing body is, what it
  holds in this corpus. Takes a registry slug **or** free text, which falls
  back to a unique case-insensitive substring match on the registry's **name
  fields**; an ambiguous or unmatched query returns `error` plus `candidates`.

  **Which registry fields carry a name is the corpus's declaration**, under
  `plugins.issuing_body_name_fields`. It defaults to `["name"]`, so a corpus
  that declares nothing matches exactly the one field it always matched, and a
  toolkit upgrade never widens a corpus's matcher on its behalf. A corpus whose
  readers know a body under more than one name lists them in the order it wants
  them tried:

  ```yaml
  plugins:
    issuing_body_registry: _meta/agency-registry.yml
    issuing_body_name_fields: ["name", "oar_name", "aliases"]
  ```

  A declared field's value may be a **string or a list of strings**, and a list
  is matched element-wise — so a curated alias list needs no config key of its
  own. Anything else in a registry cell (a number, a null, a mapping) is skipped
  rather than coerced: `str(None)` matching the query "none" is a match nobody
  wrote. The declaration is checked at load and a corpus fails loudly for an
  empty list, a bare string, a non-string entry, or fields declared with no
  `issuing_body_registry` to name columns of — each of those otherwise degrades
  into "matches nothing", which is indistinguishable from a body that is not
  there.

  **Whether a declared field exists in the registry is checked by
  `corpus-validate-frontmatter`, and reported rather than fatal.** A field no
  registry entry carries — `oar_nmae` — is shape-valid, so it loads and serves
  while every free-text query against it matches nothing. It is not a load
  error because a mid-migration corpus legitimately declares the column its
  registry is about to grow, so the finding surfaces where corpus-level config
  findings already do: a `warning` from the validator, next to a missing
  `corpus.authoritative_source`, naming the field and the registry it was
  checked against. A field carried by *some* entries is a partly-populated
  column and is not reported. A registry that could not be read reports the
  read failure as an **error** and says the fields went unchecked, and a
  registry holding no entries is reported as an empty registry rather than as a
  misspelled field — "could not check" is never served as "is not there"
  (response convention 5).

  **Uniqueness is per body, not per name.** A query hitting a body's `name`, its
  `oar_name` and two of its aliases is one hit, not four; otherwise a wider net
  would turn good matches into `no unique issuing body match`.

  Widening this matcher is safe in a way that widening a **join** would not be.
  This is a disambiguation surface: it demands a unique hit and otherwise hands
  back candidates for a human or agent to choose between, so a wider net can
  only ever produce a question, never a silent misattribution.

  Each candidate carries the name that matched, because a reader who searched by
  one name should not be asked to choose between names they have never seen:

      {slug, name, matched_field, matched_name}

  `name` is unchanged — the registry's `name` field. `matched_name` is the
  string that actually contained the query and `matched_field` is the field it
  came from. Both are **always present**, including for a corpus that declares
  nothing (where they read `name` and the entry's name), so a caller that renders
  candidates never branches on what the corpus declared.

  *Why config and not a second hardcoded key: `name` is the name a reader knows
  only for as long as a corpus keeps it that way. `executive-regulatory-frameworks`
  is migrating under its ADR 0003 — `name` holds the OAR chapter title, that title
  is copied to `oar_name`, and ERF#168 makes `name` the statutory name. Measured
  against ERF's committed 189-row registry with that promotion simulated, matching
  `name` alone leaves 189 of 189 bodies unfindable by the name printed on every OAR
  citation; `name` + `oar_name` + `aliases` leaves none (corpus-toolkit#128).
  `oar_name` is ERF's field name, and this toolkit serves many corpora.*

  Registered when **both** hold: the corpus declares
  `plugins.issuing_body_registry`, and its retrieval backend implements
  `holdings_for(slug)` — the optional member of `RetrievalBackend` that answers
  "what does this corpus hold for this issuing body", as
  `{"counts": {content_mode: n}, "coverage": {"documents", "in_registry",
  "declared_no_body", "no_registry_entry", "unattributed", "basis"}}`. A backend
  that cannot classify against the registry omits the counts, and one still
  returning v1.25.0's bare `{content_mode: n}` keeps working; both are reported
  as coverage unknown rather than complete. A backend that does not
  implement it leaves the tool unregistered and says so on stderr at startup;
  registering a tool that raises on every call is worse than not having it. The
  built-in `FileBackend` implements it, so a document-archetype corpus needs to
  do nothing. (Before v1.25.0 the gate tested for `ensure_index`, FileBackend's
  private FTS connection, so no corpus-supplied backend could serve this tool at
  any price — corpus-toolkit#75.)

  **`in_repo` counts the documents attributed to a body in the registry, and
  `attribution` says how much of the corpus that count could see.** Attribution
  is per-document, and only a document whose slug names a **registry entry** can
  be counted for anybody: the rest — a value the registry does not contain, or no
  value at all — are counted for nobody. A bare number cannot be told apart from
  a complete one, so `attribution` is present on every success:

  | field | meaning |
  |---|---|
  | `complete` | `true` every document in the corpus is counted for a registry body, so this count is the whole answer; `false` some are counted for none, so each per-body count is a **lower bound**; `null` nobody measured — an old-shape backend, coverage reported without its counts, or an empty index — which is **unknown, not none** |
  | `basis` | how attribution was derived (the declared frontmatter field, the path-derived scope slug, or `"unknown"`) |
  | `documents_in_corpus` | the denominator, present whenever the backend reported counts |
  | `documents_matched_to_a_registry_entry` | counted for a body |
  | `documents_declared_no_issuing_body` | carry a value this corpus declared in `plugins.issuing_body_slug_sentinels`: attributed to no body **on purpose**. Resolved, not a gap — these do not make a count a lower bound (backend coverage key: `declared_no_body`) |
  | `documents_naming_no_registry_entry` | carry a slug the registry does not contain and which this corpus has **not** declared a sentinel — a typo, or a deliberate value nobody has declared yet. Counted for no body |
  | `documents_with_no_issuing_body` | carry no slug at all. Counted for no body |
  | `note` | the same thing in prose, for a caller that renders rather than branches |

  Four buckets rather than a boolean because "has a slug" and "is counted
  somewhere" are different questions, and so are "counted for nobody" and
  "unexplained". Answering the first as though it were the second rebuilds the
  confident wrong number this field exists to prevent — on
  `executive-regulatory-frameworks`, 50.05% of documents carry a value the
  registry does not contain. Collapsing the deliberate ones into the unexplained
  bucket is the opposite error, and told a corpus that had done everything right
  that its counts were floors forever (corpus-toolkit#94).

  **A backend needs `declared_no_body` only if its corpus declares sentinels.**
  Omitting it there means the backend counted every sentinel document as
  `no_registry_entry`, so its split is wrong rather than merely incomplete, and
  coverage is reported as unknown. Where no sentinels are declared the key may be
  omitted and is taken as zero, so a three-bucket backend keeps working.

  `in_repo` for a body with nothing held stays the documented string, and now
  says **which** nothing it means: `"no documents ingested for this issuing
  body yet"` only where coverage is complete; otherwise a string naming the gap
  and pointing at `attribution`. "Nothing ingested" is a claim about the corpus
  and is only true when everything in it reaches a count.

  **How a document is attributed**, in order: a value the corpus declared in
  `plugins.issuing_body_slug_sentinels`, taken as-is and meaning "no issuing
  body"; else the declared `plugins.issuing_body_slug_field` where its value
  names a registry entry; else the path-derived slug under a `scoped: true`
  content root; else the declared value even though the registry does not contain
  it, so it is counted and reported rather than dropped. Both mechanisms are supported — the path is
  correct for a corpus organised by issuing body, the field is what reaches
  documents no agency directory can contain, since a rule is filed by chapter.
  **Both halves are now validated** — `corpus-validate-frontmatter` fails CI on
  an unregistered scoped path, and (since corpus-toolkit#94) on a declared value
  that is neither a registry entry nor a declared sentinel. An *undeclared*
  unregistered value still never overrides the path slug: otherwise one typo
  removes a correctly-filed document from a count that was previously right. A
  **sentinel** does override it, because it is the corpus positively asserting
  "no body" — re-attributing such a document by its directory would contradict
  the corpus about its own document. That validation is what makes the sentinel
  list a way to *answer* the coverage question rather than silence it. This is *not*
  the free-text `issuing_body` field, which is a sub-unit name rather than a
  registry slug.

  These are additive fields, `in_repo`'s own shape is unchanged, and a corpus
  declaring no slug field reports the counts it reported before, so this stays
  contract v1. What changes is the *number* a corpus reports once it declares the
  field — see the release note; on `executive-regulatory-frameworks` its largest
  agencies move by ~36× (corpus-toolkit#71).

  *Correction (2026-07): this extension was written here as
  `agency_profile(agency)`. No server has ever implemented that name.
  `issuing_body_profile` is what ships and what the rest of the platform
  says — the frontmatter field is `issuing_body`, the config keys are
  `issuing_body_registry`/`issuing_body_profiles`, and `search_corpus`
  filters on `issuing_body`. It is also the correct word: the Secretary of
  State Archives Division and the Legislature issue documents and are not
  agencies. The contract was stale, not the implementation, so this is a
  documentation correction and stays contract v1.*

### `documents_by_agency(slug, limit?, offset?)`

This corpus's documents for one agency registry slug, with an explicit statement
of how much of the corpus that answer could see.

Exists so an aggregating client — `corpus-gateway` — can assemble a per-agency
profile by **asking** each corpus rather than duplicating each corpus's agency
crosswalk. The crosswalks are per-consumer by design ("the table lives in the
consumer, correctness belongs to the registry"), so a client that copied them
would re-centralise what was deliberately distributed and go stale silently
every time one changed.

    {slug, slug_in_registry, documents: [{id, title, citation, doc_type,
     content_mode, path}], total, returned, limit, offset, attribution}

A **refusal** — a declared no-body sentinel, or an empty slug — carries `error`
and **omits `attribution`**: a refusal is not an answer, and attaching a
completeness claim to one invites reading it as an answer. Branch on `error`
before reading `attribution`.

`total` is the number of matches, not the page size, and documents are ordered
by `id` — paging by `offset` against an unordered scan repeats some documents
and skips others.

**Four answers that must not collapse into each other:**

| `documents` | `attribution.complete` | means |
|---|---|---|
| non-empty | `true` | the whole answer |
| non-empty | `false` | a **floor** — this corpus holds documents attributed to nobody |
| empty | `true` | this corpus genuinely holds nothing for that slug |
| empty | `null` | nobody measured. **Not** the same as none |

`slug_in_registry` answers a different question and is deliberately separate:
`null` means the slug **was not checked here** — either the corpus declares no
registry, or it declares one that could not be read — not that the slug is
absent. Those two are distinguished in `attribution.note`, because a broken
registry path is a fault and "no registry" is a choice.

A declared no-body sentinel (`plugins.issuing_body_slug_sentinels`) is refused
by name rather than answered: those documents are the ones the corpus attributes
to **no** body, deliberately, so they are not any agency's holdings. `limit` is
clamped to 200 and `offset` to ≥ 0, and the response echoes the values actually
served.

Registered when the retrieval backend implements `documents_for_slug(slug,
limit, offset)`. **Unlike `issuing_body_profile` it does not additionally
require a registry**, because "which of my documents carry this slug" needs
none. That difference is load-bearing rather than stylistic: `oregon-kpm` has
its registry commented out and `oregon-audits` declares none, so requiring one
would leave this tool unregistered on two of the three corpora a cross-corpus
agency profile has to ask.

## API-corpus extensions

- `list_datasets()` — datasets/entities with descriptions and freshness.
- `query_dataset(dataset, query, limit?)` — parameterized live query
  (OData/SoQL). Responses MUST include `executed_query`, `executed_at`,
  and `endpoint`. Servers enforce read-only queries and result caps.

## Hybrid

Implements both extension sets plus `join_lookup(document_id | dataset_key)`
returning the mapped counterparts.

## How the extensions above are implemented (toolkit >= 1.6.0)

`corpus-mcp-serve` registers the tools common to every corpus. The extension
sets on this page are corpus-specific by definition — only the corpus knows
its datasets, its query dialect, and its joins — so it supplies them via
`plugins.tools_module` in `corpus.yml`:

```yaml
plugins:
  tools_module: "src.budget_tools:register"    # register(mcp, framework)
```

The callable runs after every built-in tool. It receives the live
`CorpusFramework`, so a corpus tool reaches retrieval, the graph, and citation
resolution without reimplementing them.

**These names are RESERVED and a `tools_module` may not register one**, whether
or not this corpus happens to serve it:

    search_corpus   get_document        resolve_citation   graph_neighbors
    corpus_overview authority_chain     issuing_body_profile
    documents_by_agency

`authority_chain` and `issuing_body_profile` are conditional on archetype;
`issuing_body_profile` and `documents_by_agency` are conditional on the backend
implementing `holdings_for` / `documents_for_slug`. `documents_by_agency` has no
archetype condition — an `api` corpus registers it too. All three are reserved
anyway. A corpus
that claimed a conditional name would start clean, serve corpus semantics under
a core tool's name, and turn fatal the day the condition changed with no edit to
its tools module.

**A collision refuses to start**, naming the tools. This page used to say the
hook "can add tools but never silently replace one", which was reassurance about
the harmless direction: no built-in is added after the hook, and the SDK keeps
the tool registered FIRST, so a colliding corpus tool was the one discarded — the
built-in answered in its place and nothing said so (corpus-toolkit#111). The same
refusal covers a module registering one name twice, and a registration made
through `add_tool` rather than the decorator.

Before 1.6.0 the built-in tools were a closed set, and the only way to add
behaviour was to enrich `get_document` — which cannot express any of the
signatures above, because `query_dataset` and `join_lookup(dataset_key)` are
keyed on something that is not a document id.

**A failure to load is fatal, deliberately.** Starting anyway would yield a
server that answers every built-in call correctly while silently missing the
tools the corpus exists to provide, and a caller cannot distinguish "this
corpus has no `join_lookup`" from "`join_lookup` failed to load". Declaring
the hook and registering nothing is likewise an error rather than a no-op.

## Response conventions (all tools)

1. Every **object-shaped** response carries `corpus`, `archetype`, and
   `authoritative_source` (URL) fields — errors included, since an error is
   the response an agent is most likely to misread.

   `authoritative_source` comes from `corpus.authoritative_source` in
   `_meta/corpus.yml` (the ORS/OAR landing page, the SoS Archives schedules
   page, the legislature's bill site). `get_document` overrides it with the
   document's own `source_url`, which is the more precise answer to the same
   question. A corpus that declares none gets `authoritative_source: null`
   plus a `config_warning` on `corpus_overview` — an absent key would read as
   "the server did not look", which is not what happened.

   **Declaring one is a repo gate, not a runtime one** (corpus-toolkit#11).
   `corpus-validate-frontmatter` fails a corpus that declares no
   `corpus.authoritative_source`, and fails one whose host is a name RFC 2606
   reserves (`.test`, `.example`, `.invalid`, `.localhost`, and
   `example.com`/`.net`/`.org`) — those can never be a real front door, and one
   of them is what `corpus-template` ships as its unfilled placeholder, which
   parses as a URL and would otherwise sail through. A host still carrying the
   template's `REPLACE-ME` marker fails as well, for the case where the
   reserved name was edited away and the marker was not, as do a URL that
   names no host and one that cannot be parsed. The exception is the
   template itself: while `corpus.id` is still the unfilled `{{CORPUS_ID}}`
   **and** the repo holds no documents, both findings are warnings, so the
   template validates while no corpus can hide behind it. The **server** still
   starts either way and still emits `null`, because a corpus that was legal
   when it deployed must not be taken down by a pin bump. **That `null` is load-bearing**:
   it is a documented value, not a missing one, and anything that validates
   this field must accept it (`config.py` types it `str | None`). Treating it
   as a required string is what broke every corpus at once in v1.24.0 — see
   the note at the end of this convention.

   **The field is the corpus's front door, not a per-answer citation**
   (corpus-toolkit#70). It answers "where do I start if I want the official
   text for *this corpus*" — one URL, one corpus, `str | None` and staying
   that way. It is not an assertion that every document in the corpus lives
   under that URL, and a response is not wrong for carrying it when the thing
   the response describes was published somewhere else.

   Per-answer precision is a different field, and it already exists.
   `get_document` resolves this slot by precedence, most precise first, and
   **every backend gets it, including a corpus supplying its own
   `plugins.retrieval_module`** (corpus-toolkit#90):

   1. the record's own `authoritative_source`, if the backend supplied a
      non-empty one — a backend that knows a canonical URL distinct from where
      it fetched the record says so, and is believed;
   2. otherwise the record's `source_url`, if non-empty;
   3. otherwise the corpus front door, which may be `null`.

   Empty and absent are both "not supplied" at every step. So on the document
   path, where a caller is reading text it intends to verify, the front door is
   never cited over a document URL that exists.

   The framework resolves this from the RECORD, not from the assembled
   response. That distinction is the whole of corpus-toolkit#90: the fallback
   used to test the response slot, which `_envelope()` has already filled with
   the front door, so for any corpus declaring one it could never fire.
   `FileBackend` sets `source_url` and `authoritative_source` from the same
   column and so could not expose it — the built-in path was correct by
   accident of one backend's implementation rather than because the framework
   enforced it, and a corpus honouring the documented `get()` contract (which
   nowhere requires `authoritative_source`) had the front door stamped over a
   per-document URL sitting in the same payload.

   **This asks nothing of a backend.** `RetrievalBackend.get()`'s contract is
   unchanged and `authoritative_source` stays optional on a record; that
   optionality is precisely why the rule lives in the framework. Pushing a
   shared response-floor rule out to every corpus that writes a backend is the
   arrangement this fallback exists to avoid.

   What is left carrying the corpus URL is `corpus_overview`,
   `resolve_citation`, the graph tools, `issuing_body_profile` and the error
   shapes: responses that name documents rather than reproduce them, and that
   carry per-hit ids and citations, so the precise source is one
   `get_document` call away. `search_corpus` carries no envelope at all, by
   the exemption below, so it never stamps the corpus URL on anything; its
   hits carry the same per-hit identifiers.

   **So a corpus spanning publishers declares its best single entry point,
   and that is correct rather than a compromise.**
   `executive-regulatory-frameworks` is the case that forced the question:
   measured 2026-08-11, 1,972 sources across 7 hosts — ORS, OAR, executive
   orders and agency policy, a set for which the state publishes no combined
   index — and the field left unset on the reading that any one URL would be
   wrong for most of the corpus. Under the front-door reading it is not
   wrong, it is coarse, and the alternative it was being weighed against is
   `null`: an agent told by the same response to "verify at source" and given
   no place to start. Declare the publisher's own entry point where there is
   one, and the closest thing to it where there is not.

   Two other shapes were weighed on #70 and rejected. A **list** of URLs
   changes the envelope's type for every consumer and for #15's declared
   schema, which types this field as a nullable string; today a list is
   rejected, though by `config.load()` raising on the value's missing
   `.strip()` rather than by the validator's URL check, which never gets to
   run (corpus-toolkit#89). A **per-content-root declaration** is strictly
   more correct and considerably more work, and the precision it buys is the
   precision `get_document` already delivers. If a corpus later shows the
   front-door reading failing it in practice, that is a new issue with the
   measurement attached.

   **`search_corpus` is exempt, deliberately, and stays a bare JSON list.**
   This convention was written as if it applied to every tool; it does not,
   and the exemption is a design decision rather than a gap to close later.

   A search result is a *list of hits*, and the MCP SDK already treats it as
   one. Returning `list[dict]` produces a machine-readable output schema and
   one content block per hit; returning a bare `dict` produces **neither** —
   an unconstrained `dict` cannot be described, so the SDK emits no output
   schema at all. Measured on both SDK majors:

   | tool return | output schema | content blocks (40 hits) |
   |---|---|---|
   | `search_corpus -> list[dict]` | `{"result": [...]}` | 40, one per hit |
   | any tool `-> dict` | **none** | 1 |

   So wrapping the list in an object to satisfy this convention would make
   the response *less* machine-readable, not more, and would collapse
   per-hit content blocks into one opaque blob. The convention would be
   satisfied in the JSON text and lost everywhere a client actually looks.

   The cost of the exemption is real but small: a client fanning search out
   across corpora must track which response came from which server. It
   already must, because it chose which server to call.

   **The three fields are declared, and the declaration is open**
   (toolkit >= the release carrying corpus-toolkit#15). The object-shaped
   tools are annotated `-> ResponseEnvelope`
   (`corpus_toolkit/mcp/responses.py`), an open pydantic model, so the
   emitted output schema names all three, marks them `required`, types
   `authoritative_source` as `string | null`, and carries
   `"additionalProperties": true` alongside. Identical on both SDK majors:

   ```json
   {"type": "object", "additionalProperties": true,
    "required": ["corpus", "archetype", "authoritative_source"],
    "properties": {"corpus": {"type": "string"},
                   "archetype": {"type": "string"},
                   "authoritative_source": {"anyOf": [{"type": "string"},
                                                      {"type": "null"}]}}}
   ```

   Before that it was `{"additionalProperties": true}` and nothing else — a
   real output schema that said nothing about these three fields, so they were
   present in every response and invisible to *field-level* validation.

   **What this asks of a corpus.** Nothing for the built-in tools, unless it
   supplies its own `plugins.retrieval_module`. The fields are required, so a
   response omitting one is a serialization error rather than a quietly
   non-conforming answer; every built-in path assembles them in
   `CorpusFramework._envelope()`, and the exposure is a backend record that
   overrides one of the three with a non-string, which `get_document` merges
   over the envelope. Required-and-nullable is deliberate: `null` is a corpus
   saying it has no front door, an absent key is nobody having answered, and
   collapsing those two is the one thing this platform never does. A field
   with a default would collapse them by injecting the null.

   **It asks something of a corpus's OWN tools** (corpus-toolkit#96). An
   extension tool registered through `plugins.tools_module` that returns an
   OBJECT is an object-shaped response and carries the envelope like any other:

   ```python
   from corpus_toolkit.mcp.responses import ResponseEnvelope

   @mcp.tool()
   def list_datasets() -> ResponseEnvelope:
       return framework.with_envelope({"datasets": [...]})
   ```

   `CorpusFramework.with_envelope(payload)` is the supported accessor — the
   same single assembly point the built-ins use. Hand-rolling the three keys
   instead is the divergence that assembly point exists to prevent, spread
   across repo boundaries where it is harder to see.

   **It merges the envelope OVER the payload, and that direction is the point.**
   Every built-in puts the assembled front last, because a mapping the
   framework does not control must never displace the envelope
   (corpus-toolkit#102/#104). Spreading the three fields in FIRST and letting a
   payload land on top would invert, for corpora, the rule the toolkit enforces
   on itself. It is not hypothetical: a `join_lookup` relating this corpus to a
   sibling has `corpus` as its natural key, and a payload carrying it served
   the sibling's id as the answering corpus. If your payload needs to name
   another corpus, use a key that is not one of the three — `corpus` means
   "who is answering", and that is never an extension tool's to decide.

   **The annotation and the payload change together.** The fields are required
   with no defaults, so annotating a tool `-> ResponseEnvelope` while leaving
   its payload alone is a hard tool error, not a weaker answer — it stops
   answering. A bare `-> dict` annotation, which is what every extension tool
   on the platform ships today, declares no output schema at all and therefore
   carries no structured content either: the answer travels only as a JSON text
   block, so a client reading structured content gets nothing from a corpus's
   own tools while getting a parsed object from every built-in.

   A LIST-shaped extension tool keeps `-> list[dict]` and is exempt for exactly
   the reason `search_corpus` is. The exemption is about shape, not about which
   module registered the tool.

   **It is not as simple as declaring a TypedDict, and that is not a guess.**
   v1.24.0 did exactly that and broke every object-shaped tool on all four
   live corpora (corpus-toolkit#61). A TypedDict return makes the SDK build a
   pydantic model and push every response through it, so the declaration stops
   describing the response and starts *being* it:

   | | consequence |
   |---|---|
   | `authoritative_source: str` | rejects the `null` this section mandates — `total=False` makes a key optional, not nullable |
   | undeclared keys | dropped at serialization; `get_document` returned its envelope and no document body, silently, still reporting success |

   `Optional[str]` fixes the first and not the second. So the declaration had
   to reach validation **without** owning serialization — the response floor
   is a contract, not a container.

   What closed it is one word of configuration rather than a different
   mechanism: `extra="allow"`. The hazard was never that a model sits in the
   response path — `-> dict[str, Any]` already made the SDK build
   `RootModel[dict[str, Any]]` and dump every response through it — it was
   that a TypedDict's generated model is **closed**. Measured against that
   RootModel on 1.28.1 and 2.0.0, `ResponseEnvelope` emits the same keys with
   the same values for keys shadowing `BaseModel` methods and attributes,
   leading-underscore and dunder keys, the empty-string key, non-ASCII keys,
   non-JSON values, falsy values and deep nesting.

   The one thing that does change is **key order** in structured content:
   `model_dump` emits declared fields first, so a response that merges the
   envelope last (`resolve_citation`) now leads with it. JSON objects are
   unordered and the content blocks are unaffected, so no client can observe
   this — but it is the reason the claim above is "same keys and values"
   rather than "byte-identical".

   Setting `output_schema` on the registered tool after the fact was the other
   candidate and is worse: `@tool()` takes no output-schema argument on either
   major, and the schema and the validating model are used together, so
   patching one would advertise a requirement the server does not enforce.

   A schema assertion cannot detect either v1.24.0 failure; only a round-trip
   through the SDK's `convert_result` can. `tests/test_output_schemas.py` pins
   the convention across the object tools and
   `tests/test_result_marshalling.py` pins whole-payload equality for every
   registered tool, both halves, both majors.
2. Document content responses include the provenance block (source_url,
   retrieved, source_sha256, last_verified).
3. Live-data responses include executed_query + executed_at.
4. A `disclaimer` field ("non-authoritative; verify at source") appears in
   corpus_overview and any response an agent is likely to quote from.
5. Errors are explicit, never silently empty, and never report a different
   condition than the one that occurred: `unresolved`, `no_graph`,
   `not_in_graph`, `sibling_unavailable`, `stale` (past the corpus's own
   freshness window — NOT the manifest's `recheck`, which nothing reads),
   `schema_drift` (API shape changed since last_verified). "Could not
   check" and "is not there" are opposite answers and must never collapse
   into one message.

## Versioning

Contract version is declared in corpus.yml and reported by
corpus_overview. Additive tools/fields stay v1; changed semantics bump v2.
Servers must not remove core tools.
