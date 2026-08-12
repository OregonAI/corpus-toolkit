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

## v1.1.0 — cross-corpus citation resolution

Corpora in one org cite each other constantly (a records-retention corpus
cites `OAR 166-300-0040`, which lives in the rules corpus). Until now
`resolve_citation` only ever consulted the LOCAL graph and silently dropped
every candidate it didn't hold, so those citations were simply unresolvable.
A sibling's `_meta/graph.json` can't be the lookup target — it is 26 MB for
the real Oregon rules corpus.

**Fully backward-compatible: a corpus with no `siblings:` block and no
`corpus=` on any scheme behaves exactly as it does today** (regression-tested
in `tests/test_cross_corpus.py::TestBackwardCompatibility`).

- [x] **New artifact + CLI `corpus-generate-index`** (`corpus_toolkit/index.py`)
      writes the compact `_meta/corpus-index.json` a sibling resolves against:
      `{corpus, contract_version, n_documents, documents: {id: [title,
      doc_type, path]}}`. Derived from `_meta/graph.json`'s nodes when that
      file exists, else by walking the content roots. Keys are sorted and no
      wall-clock timestamp is stamped, so the file is byte-stable and
      `--check` is meaningful in CI (`--generated YYYY-MM-DD` sets the
      optional `generated` field explicitly; `--check` ignores it).
      Commit the generated file — that's the published surface.
- [x] **`siblings:` block in `_meta/corpus.yml`** (optional, default `[]`):

      ```yaml
      siblings:
        - id: executive-regulatory-frameworks
          index_url: https://raw.githubusercontent.com/OregonAI/executive-regulatory-frameworks/main/_meta/corpus-index.json
          web_base: https://github.com/OregonAI/executive-regulatory-frameworks/blob/main/
      ```

      Exposed as `config.siblings` (`Sibling(id, index_url, web_base,
      index_path)`) plus `config.sibling(id)`. `index_path` is a LOCAL file
      path for offline/dev/monorepo/test use and wins over `index_url`.
- [x] **`corpus_toolkit/remote.py`** — `load_sibling_index(sibling,
      cache_dir, ttl_seconds=86400) -> dict | None`, stdlib `urllib` only,
      10s timeout, descriptive User-Agent, on-disk cache under
      `_meta/.cache/siblings/<id>.json` (already gitignored). Serves a fresh
      cache without a fetch; on fetch failure falls back to a STALE cache
      marked `_stale: True` (`_source` is `local`/`fetch`/`cache`/
      `cache-stale`); returns `None` only when there is nothing at all. A
      payload without a `documents` dict is treated as unavailable, never
      trusted. **Never raises** — an unreachable sibling degrades resolution,
      it never breaks the server.
- [x] **`register_scheme(..., corpus="<sibling id>")`** declares that a
      scheme's candidate ids live in a sibling corpus. Default `None` =
      local, as before. `_SCHEMES` entries are now 5-tuples.
- [x] **`resolve_citation` sibling fallback**: matches from a sibling carry
      `corpus` + `url` (`web_base` + repo path; omitted when the sibling
      declares no `web_base`) and the response is tagged
      `resolved_via: "sibling:<id>"`. Resolution only ever follows an
      EXPLICIT `corpus=` declaration — it never guesses a sibling. If the
      sibling index can't be loaded the result stays `unresolved` with
      `sibling_unavailable: "<id>"` and a note saying so, deliberately
      distinct from the "sibling consulted, no such document" note: "we
      couldn't look" and "it isn't there" are opposite answers for a caller.
- [x] `tests/test_cross_corpus.py` — first tests in the repo (stdlib
      `unittest`, run `python3 -m unittest discover -s tests`).

### Adopting it in a corpus repo
1. `corpus-generate-index --config _meta/corpus.yml`, commit
   `_meta/corpus-index.json`, and add a `--check` step to CI.
2. Add a `siblings:` entry per corpus you cite.
3. Pass `corpus="<sibling id>"` on the schemes in your `citation_module`
   whose citations that sibling owns.

## v1.7.0 — graph tools stop lying, and a tag starts meaning something

Five defects found by a Product Operating Model baseline measurement of the
platform on 2026-07-28 (corpus-toolkit#3–#8). Four of them share one shape —
**code that reports success, or reports confidently, without having done its
job** — which is why every fix here ships with a test that was run against the
broken behaviour first.

- [x] **`graph_neighbors` no longer raises on an external edge target**
      (#4). `nodes[t]` assumed every edge target was a local node.
      `oregon-records-retention` reports `n_edges: 440, n_edges_external: 440`
      — every edge is an OAR citation held by a sibling — so the tool raised
      `KeyError` for **every document in that corpus**, surfacing to the caller
      as a tool error whose entire message was the citation string. A
      non-local target is now `{citation, external: true}`, enriched to
      `{id, title, doc_type, corpus, url, resolved_via}` through the same
      sibling index `resolve_citation` already uses (one index load per tool
      call, grouped per sibling — not one per edge), and marked
      `sibling_unavailable` when the sibling cannot be consulted.
      **`authority_chain` had the identical unguarded lookup** and raised the
      same way whenever an `implements`/`implemented_by` edge crossed corpora,
      which is precisely what a cross-corpus authority chain is; the issue
      named only `graph_neighbors`.
- [x] **The graph tools stop reporting a missing GRAPH as a missing
      DOCUMENT** (#5). `graph()` degrades to `({}, {})` when `graph_path` is
      absent, so `doc_id not in nodes` was true for every id in the corpus and
      both tools answered `"no document with id X"` about documents the same
      server was serving with full provenance. Three conditions are now
      distinct: `no_graph` (the corpus has no graph), `not_in_graph` (the graph
      is stale relative to the corpus — rebuild it), and the genuine
      `"no document with id X"`. Reported via `_graph_lookup`, which consults
      `backend.exists()` for the middle case exactly as `resolve_citation`
      already did for the same class of false statement.

      Considered and **rejected**: not registering the graph tools when
      `graph_path` is absent. `graph_neighbors` is a mandatory core tool for
      all archetypes and the contract says servers must not remove core tools;
      an agent would get "no such tool" where the truthful answer is "no
      relationships recorded". (`oregon-legislature/_meta/corpus.yml` asserts
      these tools are "NOT OFFERED for this archetype" — that comment is
      wrong and is filed against that repo.)
- [x] **`corpus.authoritative_source`** (#6). New optional key in the
      `corpus:` block, read by `config.py`, emitted on **every** object-shaped
      response including errors. `get_document` still prefers the document's
      own `source_url`, which answers the same question more precisely. A
      corpus that declares none gets an explicit `null` plus a
      `config_warning` on `corpus_overview` and a warning from
      `corpus-validate-frontmatter` — an absent key would read as "the server
      did not look". `search_corpus` cannot comply (it returns a bare list);
      that is stated in the contract and tracked as #10 for v2.
- [x] **`joins[].document_id` is resolved** (#3). The frontmatter schema
      validated the *shape* of a `joins:` entry and nothing anywhere read it,
      so a corpus could ship joins pointing at documents that do not exist with
      every gate green. `corpus-validate-frontmatter` now resolves each
      `document_id` against the same corpus-wide universe relationships use,
      on both the full and `--check-relationships` paths. `{dataset, key}` is
      explicitly **not** checked and cannot be — only the corpus knows what one
      of its dataset keys means — and `docs/provenance-schema-v1.md` now says
      so instead of claiming "CI checks referential integrity".
- [x] **`docs/mcp-interface-contract.md` re-audited against the shipped
      implementation** (#7). `agency_profile(agency)` was documented and has
      never existed; `issuing_body_profile(slug_or_query)` is what ships, is
      the term the whole platform uses (`issuing_body` frontmatter,
      `issuing_body_registry` config, the `issuing_body` search filter), and is
      the correct word — the SoS Archives Division and the Legislature issue
      documents and are not agencies. **The contract was stale, not the
      implementation.** Also corrected while there: `search_corpus`'s
      documented `status`/`tags` filters do not exist (its real parameters are
      `doc_type`, `issuing_body`, `limit`, `mode`); `get_document` takes a
      `part` argument and pages documents over 50 KB; `corpus_overview`'s field
      list was aspirational. Documentation-only, so still contract v1.
- [x] **A release gate that drives a real corpus** (#8).
      `.github/scripts/contract_smoke.py` instantiates `corpus-template`,
      writes a verbatim document plus its snapshot, runs
      `build_graph`/`validate-frontmatter`/`verify-provenance`/
      `generate-index --check`, then builds the MCP server and **calls** every
      mandatory core tool, asserting the answers. `.github/workflows/
      release-gate.yml` runs it on every PR and push to main, and on a tag
      deletes the tag if it fails — GitHub has no pre-tag hook, so the only
      way a tag can mean something is a continuously verified main plus a
      backstop that unpublishes a bad ref before four corpora pin it.

      Verified by reintroducing each defect and watching the gate: #4's
      `nodes[t]` reproduces the original error verbatim
      (`Error executing tool graph_neighbors: 'OAR 166-300-0015'`), an
      unregistered core tool, a search that silently returns nothing, and a
      `get_document` that returns metadata without a body all fail it.

### Adopting it in a corpus repo
1. Bump both pins (`uses:` **and** `toolkit-ref:`) to `v1.7.0`.
2. Add one line to `_meta/corpus.yml` under `corpus:`:
   `authoritative_source: "<URL of the official text>"`. Until you do,
   validation warns and `corpus_overview` carries `authoritative_source: null`
   with a `config_warning`; nothing fails.
3. If this corpus ships `joins:`, run `corpus-validate-frontmatter` locally
   before bumping — a dangling `document_id` is now an **error**. `{dataset,
   key}` integrity remains yours; wire your own `--check` into the `generated`
   CI job.
4. Nothing else changes. Graph tool responses gained fields
   (`corpus`/`archetype`/`authoritative_source`, and `external`/`corpus`/`url`
   on non-local neighbours) and lost none.

## v1.8.0 — survive the MCP SDK's major version, in both directions

corpus-toolkit#13. The `mcp` extra was an unbounded `mcp[cli]`, so it floated onto
**mcp 2.0.0** the day that shipped — and 2.0.0 deleted `mcp.server.fastmcp` outright
(no alias, no deprecation shim, the name does not appear in the wheel). `server.py`
imported `FastMCP` at module scope.

**Where the failure landed is the point.** A corpus image does:

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt        # -> mcp 2.0.0
RUN python3 -c "...corpus_toolkit.mcp.framework import CorpusFramework...ensure_index()"
```

The build's own smoke step imports `framework`, which is stdlib-only and imports fine on
both majors. The container runs `server`, which did not. So **the image built green and
could not start** — measured, not inferred, by installing exactly what
`oregon-budget/requirements.txt` pins:

```
$ pip install --target ./v160 "corpus-toolkit[mcp] @ git+...@v1.6.0"
mcp resolved to: 2.0.0
$ python3 -c "from corpus_toolkit.mcp.framework import CorpusFramework"   # exit 0
$ python3 -c "import corpus_toolkit.mcp.server"                            # exit 1
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

- [x] **`corpus_toolkit/mcp/_sdk.py`** — the one place that knows which SDK major is
      installed. `server.py` and `.github/scripts/contract_smoke.py` go through it and
      contain no version branching of their own.
- [x] **Both majors supported, not one picked.** A corpus pins a toolkit TAG and never
      pins the SDK, so the SDK floats at image-build time however carefully the toolkit
      pin is managed — which is exactly how this happened. Supporting one major and
      bounding the other just moves the cliff to `mcp` 3.0.
- [x] **`mcp = ["mcp[cli]>=1.28,<3"]`** — the bound is exactly what has been run, no
      wider. Verified against **1.28.1 and 2.0.0** by running the full suite and the
      release gate under each, not by reading a changelog.
- [x] **CI matrixed over `mcp-spec`** in `tests.yml` (both `pytest` and `entrypoints`)
      and in `release-gate.yml`. Ranges (`>=1.28,<2`, `>=2,<3`) rather than exact pins,
      so the next breaking change *within* a major surfaces as a red build instead of a
      crash loop. The `pytest` leg asserts the SDK major it actually resolved matches the
      one the leg intended — two legs silently resolving to the same SDK would pass twice
      and prove once.
- [x] **The startup guards now assert behaviour, not SDK internals.** `main()` checked
      `hasattr(settings, "streamable_http_path")`; 2.0 moved the mount to a `run()` kwarg
      and deleted the setting, so that guard would have refused to start on a perfectly
      working SDK while a genuinely wrong mount went undetected. It now builds the app and
      asks whether it answers at the routed path.
- [x] **One `http_kwargs` dict feeds both the verification build and `run()`.** Not
      tidiness: on 2.x `run()` constructs its own app from its own arguments, so a mount
      verified from a separately-argued build would be a check on a different object than
      the one served.

### What actually differs between the majors (measured)

| | 1.28.1 | 2.0.0 |
|---|---|---|
| class | `mcp.server.fastmcp.FastMCP` | `mcp.server.mcpserver.MCPServer` |
| `@x.tool()`, `@x.resource()` | identical | identical |
| `_tool_manager.list_tools()` | sync | sync |
| `_tool_manager.call_tool()` | `context` optional | `context` **required** |
| host/port/path/security | mutable `settings.*` | kwargs on `run()`/`streamable_http_app()` |
| session manager | `_session_manager` | `session_manager` (public) |
| `TransportSecuritySettings` | `mcp.server.transport_security` | same path |
| `requires-python` | `>=3.10` | `>=3.10` |

The settings→kwargs move is an improvement: on 1.x the session manager captured
`settings.transport_security` at the FIRST `streamable_http_app()` call and cached it for
the process, so anything set afterwards was silently ignored — the bug that made every
tunnelled request 421 in v1.5.0. On 2.0 each build honours its own kwargs (verified: two
builds with different settings each take effect). The guard stays regardless; its purpose
was never to describe one SDK's caching.

### Adopting it in a corpus repo
1. Bump both pins to `v1.8.0`. Nothing else changes — no config, no response shape.
2. **If your image was built from a toolkit tag earlier than v1.7.0 and cannot start**,
   this is why. Either bump the pin, or add `mcp<2` to the corpus's own
   `requirements.txt` as an immediate mitigation — an old tag's unbounded dependency
   cannot be fixed retroactively from here.

## v1.9.0 → v1.23.0 — the ten releases this file skipped

Back-filled 2026-08-04 (corpus-toolkit#41). This file stopped at v1.8.0 while the platform
shipped fifteen more tags, so a corpus deciding whether to move a pin had only `git log`.
Per-release detail now lives in `CHANGELOG.md`; what follows is the part that matters when
you actually move a pin — **what can break you, and what you must do**.

### Nothing here requires a config change except v1.19.0

Bumps from v1.9.0 through v1.23.0 are drop-in for a corpus that is already valid, with one
exception and two conditionals below.

### v1.19.0 — the one that can fail your CI

**`toolkit-ref` became required** on the reusable workflows, and **archetype is enforced**.
A corpus calling a reusable workflow without `toolkit-ref:` fails at the call, and one whose
`corpus.archetype` is missing or not one of `document`/`api`/`hybrid` fails validation.

This is also the release that made `schema.doc_types` a corpus-local declaration
(corpus-toolkit#40), so a new instrument family no longer costs a toolkit release plus an
org-wide pin bump. Declaring a type with `verbatim: true` unions it into the set
`corpus-verify-provenance` requires full text for — which means **a corpus that declares a
type and then ships summaries will start failing provenance**. That is the intended
behaviour and the reason the mechanism exists; it is listed here because it is the one way a
`doc_types` block turns a passing corpus red.

### v1.21.0 — required if your documents carry `### ` anchors

Serving the inside of big documents — `### ` subsections, chunk paging, chunk-aware search
hits — arrived here. Below v1.21.0, `get_document(part="ARTICLE 21…")` cannot reach a
subsection at all, so **anchors in a corpus pinned below this version are inert**: they sit
in the text, `_subheadings()` does not exist to list them, and nothing errors.

If your corpus anchors large documents (federal-reference, oregon-collective-bargaining),
this is the floor.

### v1.22.0 — new tools, no forced adoption

`corpus-verify-extraction`, `source_data_file` provenance, fetch-failure tolerance in
`corpus-detect-changes`, and `corpus-verify` — the only tool that writes
`last_verified`/`verified_by`, which until then nothing on the platform could set. Adopt
per corpus; nothing is mandatory.

Note the tolerance change alters an **exit code**: before v1.22.0 any fetch failure failed
the drift run; after, isolated failures are tolerated and only >20% is systemic (or pass
`--strict` for the old behaviour). A scheduled job that was reliably red for a reason you
had stopped reading may now go green — check what it was failing on before assuming it was
noise.

### v1.23.0 — required for any repo that is an MCP *client*

`_sdk` gained the client half of the mcp 1.x/2.x seam. If you write code that CONNECTS to a
corpus (a gateway, a chat app), import `open_client_streams`, `tool_input_schema` and
`result_is_error` from `corpus_toolkit.mcp._sdk` rather than the SDK directly — six
client-side APIs differ between majors, two of them renamed silently, so code that works
today breaks on the day someone bumps `mcp`.

Corpus repos that only SERVE are unaffected.

### v1.25.0 — safe for every corpus; rebuild if you run semantic search

No config changes, no schema change, no MCP contract change. Bump both pins and re-run CI as
usual.

**The one thing to know**: a corpus with `plugins.semantic_search_module` set now resolves
its embeddings artifact from `config.root` rather than from the process's working directory.
In the containers WORKDIR *is* the repo root, so the resolved path is identical, and
`CORPUS_SEMANTIC_DIR` still wins over both — a normal deployment sees no change. But this is
the path every semantic query takes, so rebuild the image deliberately rather than letting it
ride along on an unrelated build, and assert `available()` in your healthcheck afterwards.
That assertion is worth adding whether or not you take this bump: the semantic arm degrades
to keyword-only **silently** by design, so a wrong path has always looked like working search
with worse results.

Unchanged and still true: a corpus that builds its artifact somewhere other than
`_meta/embeddings` (via `--out`) must set `CORPUS_SEMANTIC_DIR` at serve time. The builder's
flag and the reader's environment variable are two ways to say one thing and are tracked
separately.

If your corpus supplies its own `plugins.retrieval_module`, it keeps working untouched —
`holdings_for` is optional. Implement it if you want that backend to serve
`issuing_body_profile`, which before this release it could not do at any price.

### Adopting any of these in a corpus repo

1. Bump **both** pins (`uses:` and `toolkit-ref:`) — and the third, if the repo installs the
   toolkit in a job of its own. They are separate knobs and drift silently
   (corpus-toolkit#9).
2. If the bump crosses v1.19.0, confirm `corpus.archetype` is set and every reusable
   workflow call passes `toolkit-ref:`.
3. Re-run the full CI, not just the changed job. The gates that catch a bad bump —
   provenance, frontmatter, generated artifacts — are not the ones a pin change looks like
   it touches.
