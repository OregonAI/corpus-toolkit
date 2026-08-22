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
   with a `config_warning`; nothing fails. **(Superseded — that warning is an
   ERROR since corpus-toolkit#11, together with a placeholder value under an
   RFC 2606 reserved host. Every live corpus declares one; see the CHANGELOG
   entry for the version you are pinning.)**
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

### v1.26.0 — REQUIRED action if your manifest has empty `sha256` values

Two exit codes change in `corpus-detect-changes`, and a corpus whose baselines were never
recorded will go **red** on its next scheduled run instead of green. That is the point: it
was already inert, and now it says so (corpus-toolkit#67, #68).

**Do not grep for it.** An unrecorded baseline has four spellings — `sha256: ""`,
`sha256: ''`, `sha256:` and `sha256: ` — so `grep -c 'sha256: ""'` reports 1 on a file the
tool counts as 4, and a corpus using single quotes reads 0, skips this section and goes red
on the next cron. **The tool counts them itself**: every run now prints
`N with no recorded baseline` in its summary and marks the affected groups in the per-group
breakdown (`counties 2/2 [2 unseeded]`).

**Seed before you bump the pin.** The seeding command is safe to run first and read after —
it writes only into empty baselines, reports `0 baseline(s) recorded` if there are none, and
commits nothing. From a checkout of the corpus, with the new toolkit installed:

```bash
corpus-detect-changes --config _meta/corpus.yml --record-baseline
git diff _meta/                       # curated data — read the diff
```

Bare `--record-baseline` fills only sources with **no** recorded baseline; a recorded one is
left alone and reported, because replacing it is accepting an upstream change you have not
read. `--record-baseline=refresh` does that second thing when you mean it. Sources whose
fetch failed are never written. Nothing is committed — open a PR with the manifest diff as
usual. It refuses to run alongside `--open-issues`.

Known affected, both tracked in their own repos: `oregon-counties` (3,447 sources, 27
manifests) and `oregon-kpm` (789). `executive-regulatory-frameworks` has a complete baseline
and needs none of this.

**What else changes for the corpus tier:**

- A **capped** run (more than 25 changed sources) now exits 1 and emits a workflow warning
  annotation. Uncapped runs are unchanged. If your corpus caps regularly, read the new
  per-group breakdown line before raising anything — a group at or near 100% has been a
  template change or a broken fetch far more often than it has been real revisions.
- If you call `detect-upstream-changes.yml`, take the new revision: its STATUS.md steps run
  with `if: always()` so a red drift step no longer skips them. Do **not** add
  `continue-on-error` to the drift step; that restores the green check this change exists to
  remove.
- The summary line gained a field (`N with no recorded baseline`) and a per-group breakdown
  line. A corpus grepping the drift log in its own CI should expect both.
- Three more runs now exit 1, all of them cases that used to be green while nothing was
  checked or nothing was written: a run whose scope came out **empty** (a typo'd `--group`
  checks 0 sources), a `--record-baseline` run that **refused** a rewrite it could not
  account for, and the inert case above. `--github-output` gains `unseeded=N`, and no longer
  reports `changed=true` on an inert run — every source "changed" against an empty baseline
  is not a finding, and it should not trigger downstream steps.

**Optional, and only if your sources embed a volatile token** (session id, CDN token,
footer build version): declare it in `_meta/corpus.yml`.

```yaml
volatile_patterns:
  - ";JSESSIONID_OARD=[^?'\" >]*"
  - "OARD Application version v[0-9.]+"
```

Regexes over the **raw bytes**, applied on the HTML/XML path only, compiled at config load —
an invalid or empty one fails the load by name rather than becoming a silent no-op. Declaring
none changes nothing: hashes are byte-identical to v1.25.0.

**Keep them narrow, and read the breadth line in the run output.** Every run reports how much
each pattern removed, in bytes and as a share of what it fetched, and warns above 10%. A
pattern wide enough to swallow the body — `<main>.*</main>` — deletes content before hashing,
so two versions differing only inside it hash identically and those documents can never report
drift again. The toolkit measures and says so rather than refusing: how much of your pages is
genuinely volatile is your call, but it should be a call somebody made in a PR rather than a
side effect nobody noticed.

**Adding a pattern invalidates the baseline of every page it matches.** Do it in one PR:
add the pattern, run `corpus-detect-changes --config _meta/corpus.yml --record-baseline=refresh`,
review the manifest diff, commit both. Otherwise the next cron reports the whole group as
drift, which is the failure the key exists to prevent. ERF's own `src/repo_lib.py` patterns
are the ones to move here — the two hashers can disagree about the same bytes until they do.

### Unreleased — `search_corpus`'s `issuing_body` takes a registry slug too (corpus-toolkit#131)

**Nothing to do, no rebuild, and no config key.** The filter still matches the free-text
`issuing_body` frontmatter field for every value that is not a registry slug, so a caller
passing a frontmatter string sees exactly what it saw before, and a search that passes no
`issuing_body` is byte-identical. What changes is that a value naming an entry in your
`plugins.issuing_body_registry` is now filtered on the RESOLVED slug — the same attribution
`documents_by_agency` answers from — instead of matching nothing.

**Two response changes to know about if you parse hits.**

1. A hit gains `issuing_body_filter` (`{value, matched, registry_checked, note?}`) whenever
   the parameter is used, naming the column that matched.
2. A body-filtered search with NO matches returns one record that is not a hit —
   `no_hits: true`, no `id`/`path`/`snippet` — instead of `[]`. A client that renders hits
   should branch on `no_hits`; one that counts `len()` will see 1 where it used to see 0, on
   that path only. An unfiltered search that matches nothing still returns `[]`.

**If your corpus supplies `plugins.retrieval_module`, read this.** `RetrievalBackend.search`
may now accept a keyword-only `issuing_body_slug: str | None = None` and filter on the
resolved registry slug. It is OPTIONAL and detected from your signature, so an adapter that
does not name the parameter is never handed it and keeps working untouched — its answer is
reported as a frontmatter answer with a note naming your backend, never relabelled a slug
answer.

**`**kwargs` does not count as accepting it.** If your `search` takes `**kwargs` and ignores
unknown keys, the framework will NOT ask it for a slug filter — deliberately, because a
backend that swallows the keyword returns an unfiltered result, and an unfiltered result
labelled "filtered by slug" is a wrong answer rather than a missing one. Name the parameter
explicitly to serve slug filtering:

```python
def search(self, query, *, doc_type=None, issuing_body=None, limit=10,
           mode="hybrid", issuing_body_slug=None):
```

Corpora with no `issuing_body_registry` (`oregon-kpm`, `oregon-audits`) resolve nothing and
say so: the filter block reports `registry_checked: false`, which means the slug question was
never asked — not that the value is not a slug.

### Unreleased — a corpus declares which registry fields carry a name (corpus-toolkit#128)

**No index rebuild, no schema-version bump, and no change to which bodies you match unless
you opt in.** `issuing_body_profile`'s free-text fallback now matches the registry fields
listed in `plugins.issuing_body_name_fields`, which defaults to `["name"]` — the one field
it always matched.

**Optional, and only if your readers know a body under more than one name:**

```yaml
plugins:
  issuing_body_registry: _meta/agency-registry.yml
  issuing_body_name_fields: ["name", "oar_name", "aliases"]
```

Order is the order they are tried, and it decides which field a candidate reports as the one
that matched. A field's value may be a string **or a list of strings** (matched element-wise),
so an alias list needs no second key. A field naming no column in your registry does not
fail the load — a corpus mid-migration declares the column its registry is about to grow —
but `corpus-validate-frontmatter` now warns, naming the field and the registry it was
checked against (corpus-toolkit#129), so a typo shows up in CI rather than as a body nobody
can find.

**Two things change for every corpus, whether or not you declare anything.**

1. **Candidates gain two keys.** An ambiguous or unmatched query returns
   `{slug, name, matched_field, matched_name}` per candidate instead of `{slug, name}`.
   `name` is unchanged. `ResponseEnvelope` is open (`extra="allow"`), so nothing drops them;
   a client that renders candidates should show `matched_name`, which is the string the
   reader's query actually hit.
2. **A malformed registry cell no longer takes the tool down.** An entry whose `name` is
   null, numeric or a list previously raised `AttributeError: 'NoneType' object has no
   attribute 'lower'` on *every* free-text query against that registry. Those cells are now
   skipped, and a list-valued `name` is matched element-wise — so a corpus with one of those
   finds a body it could not find before. Check your registry if you are relying on the
   crash, which nobody is.

**Sequencing note for `executive-regulatory-frameworks` (ERF#168).** Land the
`issuing_body_name_fields` declaration in the same PR as the `name` promotion, or before it.
The promotion moves the OAR chapter title out of `name`, and until `oar_name` is declared,
every body in the 189-row registry is unfindable by the name printed on its OAR citations —
which is the defect this key exists to prevent, arriving through the migration that motivated
it.

### Unreleased — a JSON source can declare which paths it watches (corpus-toolkit#72)

**Nothing changes unless you opt in.** A `format: json` source with no `watch` hashes exactly
as before, so no committed `sha256` moves. Verified across the platform: 1,116 sources in 8
manifests, 3 of them `format: json`, none declaring `watch`.

**The problem, if you watch a Socrata metadata document.** Your manifest points `url` at
`/api/views/<id>.json` rather than at the rows — deliberately, because hashing 668,906 rows
reports a change every run and tells you nothing. But that metadata carries counters that
move on their own:

```
downloadCount   35632
viewCount       13812
rowsUpdatedAt   1765475245   (the data itself, unchanged for eight months)
```

So the hash changed every week while the data sat still. `oregon-budget` produced six
distinct hashes across two consecutive runs in a week its `live-reconciliation` job passed.

**Declare what you actually watch:**

```yaml
sources:
  - id: agency-expenditures
    url: "https://data.oregon.gov/api/views/y9g9-xsxs.json"
    format: json
    watch:
      - rowsUpdatedAt
      - columns[].name
      - columns[].dataTypeName
```

The hash then covers only those paths, canonicalised — so upstream re-ordering its KEYS or
re-indenting its JSON is not a change. Array element order is deliberately *not* normalised:
`columns[].name` coming back in a different order is a schema change worth reporting.

**An allowlist, not a blocklist, and that is the point.** A new Socrata counter is inert by
construction. Listing what to *ignore* instead would make every new counter a fresh false
positive until somebody noticed and extended the list — the same failure arriving slower.

**A declared path the document does not contain is an ERROR**, reported on stderr as
`WATCH PATH MISSING` with a run annotation — *not* a fetch failure. Two documents that both
lack a watched path would otherwise hash equal and read as "unchanged", the corpus reporting
stability exactly when upstream removed the field it was watching. If you see it, upstream
changed shape, which is what you wanted to know.

It **exits non-zero regardless of `--strict`**, unlike a fetch failure. Upstream being
briefly unreachable is ordinary; a source that was fetched successfully and still could not
be compared is not, and it stays uncompared on every subsequent run.

A body that will not parse as json is reported separately again, as
`WATCH BODY UNREADABLE` — an error page served with a 200 is a fact about the response, not
about your `watch` list, and pointing you at the list would waste the trip.

**Only one `[]` per path**, and the list is checked before the first request rather than
mid-crawl. These authoring shapes are handled by name rather than silently doing something
else — everything except the last row is refused, naming the source:

| Written | Was | Now |
|---|---|---|
| `watch: rowsUpdatedAt` | iterated character by character → `watched path 'r' is not present` | refused, naming the source |
| `watch:` (no value) | reverted to hashing the whole document, silently | refused |
| `watch: []` | digests to a constant → `unchanged` forever | refused |
| `columns[]name` | reported as a path upstream does not have | refused, suggesting `columns[].name` |
| `columns[ ].name` | looked up as a literal key, reported missing | refused |
| `columns[].` or `[].name` | a projection with no key | refused |
| `watch: [a[].b[]]` | two documents could digest equal | refused |
| `- " columns[] . name "` | looked up with the padding, reported missing | **trimmed, not refused** |

The second row is the one to check for if you hand-edit a manifest: `watch:` with nothing
under it is one bad indent away from a source that declares `watch` and does not use it.

Only sources in the groups a run actually checks are validated, so a typo in one group does
not abort another group's cron or `--check-robots`.

**A `watch` source must be json, and that is checked before the crawl.** `format: json` (or
`geojson`) is believed; with no `format:` key the url's extension decides, so
`.../y9g9-xsxs.json` is fine and `.../feed.xml`, `.../doc.pdf`, `.../policy.html` and a url
with no extension at all are refused, naming the source. Add `format: json` for a REST
endpoint whose url does not end in `.json`. Without this the body would not parse and the
run would report an unreadable *response* — blaming upstream for what the manifest says.

**A BOM is fine.** Bodies are decoded `utf-8-sig`, so the byte order mark IIS/.NET-backed
endpoints prepend does not make a source permanently uncomparable.



**Grammar is deliberately small:** dot-separated keys, with `[]` projecting over an array.
`rowsUpdatedAt`, `columns[].name`, `columns[].cachedContents`. No JSONPath.

**Adopting it re-baselines that source.** Its hash changes the moment you add `watch`, so run

```bash
corpus-detect-changes --config _meta/corpus.yml --record-baseline=refresh
```

in the same PR and land it on its own, rather than folded into a pin bump.

### v1.27.0 — five relation names are reserved in `_meta/graph.json` (corpus-toolkit#105)

**Check your graph's relation types before bumping the pin. Nothing else will tell you.**

`graph_neighbors` returns each edge type under its own response key, so a relation name
becomes a response key verbatim. Five are now reserved: `corpus`, `archetype`,
`authoritative_source` (convention 1's envelope) plus the tool's own `id` and `title`.

```bash
python3 -c "import json,sys; g=json.load(open('_meta/graph.json')); \
  bad=sorted({e['type'] for e in g['edges']} & {'corpus','archetype','authoritative_source','id','title'}); \
  print('COLLIDES:', bad) if bad else print('clear')"
```

**No corpus on the platform collides** — the types in use are `references_external`,
`related`, `supersedes`, `implements` and `implemented_by` — so this is expected to be a
no-op for everyone. It is worth thirty seconds anyway, because the failure is *lazy*: the
graph is parsed on the first graph-tool call, not at boot, so a collision is first seen in
production rather than at deploy. `corpus-validate-frontmatter`, your own
`build_graph --check` and the toolkit's release gate all stay green, since the frontmatter
schema's `relationships` block is closed over the five live names — only your graph builder
can introduce a collision.

**If you do collide**, `graph_neighbors` returns an explicit error naming the relation, the
reserved set and the graph file, and you rename the relation and rebuild. **Only that tool
is affected**: `corpus_overview`, `resolve_citation` and `authority_chain` share the graph
loader but cannot have a key displaced by a relation name, so they keep answering.

**Why an error rather than quietly ignoring the colliding relation.** The two entries below
fix the same class for a *backend's* mapping by letting the framework's keys win silently —
the backend had no business setting them. A graph relation is your own declared edge, and
dropping it without a word would mean a relationship stopped being served and you never
found out.

### v1.27.0 — a corpus's own tools can satisfy response convention 1 (corpus-toolkit#96)

**Nothing breaks and nothing is required.** Extension tools registered through
`plugins.tools_module` keep working exactly as they do now. This adds the means to fix a
gap, and the fix itself is a follow-up in each corpus repo.

**The gap.** Every extension tool on the platform is annotated bare `-> dict`, which makes
the SDK emit **no output schema and no structured content at all**. The answer is in the
JSON text block, which is why nobody has noticed — but a client reading structured content
(corpus-gateway's fan-out, a schema-driven validator) sees a hybrid corpus as having half a
tool surface, with no error anywhere. Five live tools: `list_datasets` and `query_dataset`
in `oregon-legislature`; `join_lookup`, `list_datasets` and `query_dataset` in
`oregon-budget`.

**What to change, when you get to it:**

```python
from corpus_toolkit.mcp.responses import ResponseEnvelope

@mcp.tool()
def list_datasets() -> ResponseEnvelope:                     # was: -> dict
    return framework.with_envelope({"datasets": [...]})      # payload unchanged
```

`with_envelope` MERGES THE ENVELOPE OVER YOUR PAYLOAD, deliberately. If a key of yours
collides with `corpus`, `archetype` or `authoritative_source`, the envelope wins — those
three say who is answering, and that is not a tool's to decide. A `join_lookup` relating
your corpus to a sibling is the realistic collision: name the other corpus under a key of
your own.

**BOTH LINES OR NEITHER.** The three envelope fields are required with no defaults, so
annotating the return type while leaving the payload alone does not degrade the response —
it is a hard `ToolError` and the tool stops answering:

```
ToolError: 3 validation errors for ResponseEnvelope
  corpus                Field required
  archetype             Field required
  authoritative_source  Field required
```

None of the five live tools currently emits any of the three, so an annotation-only sweep
would take out both hybrid corpora. Change the payload in the same commit, and call the tool
once before merging.

**A LIST-shaped extension tool needs no change.** `-> list[dict]` already produces a schema
and one content block per item, and is exempt from convention 1 for exactly the reason
`search_corpus` is — the exemption is about shape, not about which module registered it.

**Do not reach for a TypedDict.** That is corpus-toolkit#61: it took all four live corpora
down in a single deploy, because the generated model is closed and silently drops every key
it does not declare. `ResponseEnvelope` is open (`extra="allow"`) precisely so your payload
travels intact.

### v1.27.0 — declarable issuing-body sentinels (corpus-toolkit#94)

**Every corpus rebuilds its FTS cache once.** The index schema version goes to 4 for exactly
the reason it went to 3: same column, different values, which no content-key check can
notice. A corpus that declares no sentinels indexes identically and pays only the rebuild.

**Rebuild it deliberately — the operational note from the 3 bump applies unchanged.** A
corpus that BAKES its index into the image (ERF's Dockerfile pre-builds it, ~70s at 76k
documents) needs a fresh image: `./scripts/deploy.sh <corpus> <ref> --rebuild-image`. A
MOUNTED corpus rebuilds out of band in `deploy_mounted()`, stopping the service for roughly
8 minutes while it warms. Only a plain local run rebuilds silently on first use. Deploying
without the rebuild forces a 70s rebuild inside the container on the first request. After
deploying, check the document count — `deploy.sh` aborts below `MIN_DOCS`.

> **CORRECTION (corpus-toolkit#114): `--rebuild-image` is not needed, and does nothing
> today.** The flag is guarded on a corpus being MOUNTED, and `platform-deploy` mounts none
> — `deploy.sh` declares an empty mounted set and prints a notice if you pass it anyway.
> Because nothing is mounted, an ordinary `deploy.sh <corpus> <ref>` builds the image every
> time, so the index is re-baked as a matter of course.
>
> Measured on the v1.27.0 pin wave: ERF's image was rebuilt by the ordinary
> reconcile-triggered deploy with no flag passed, and the corpus answered on the new commit
> with all 75,905 documents.
>
> **The `MIN_DOCS` sentence above has the same defect.** `deploy.sh`'s document-count abort
> lives inside `deploy_mounted()`, and its own comment calls it "the replacement for the
> build-time content gate" — it exists *because* a mounted corpus has no Dockerfile bake to
> guard it. A BAKED corpus gets no post-deploy count check; its guard is the Dockerfile's
> build-time `RUN … ensure_index()`, which fails the image build instead. So the safety net
> is real but it is a different one, and checking the count after deploying is still worth
> doing by hand.
>
> **What remains true:** an FTS schema bump does require a rebuild, and for a baked corpus
> that rebuild happens at image build. Both the flag and the `MIN_DOCS` abort become live
> again the day a corpus is mounted, which is why this is a correction rather than a
> deletion.

**Nothing else is required.** No corpus declares `issuing_body_slug_field` today, so nothing
on the platform changes until one adopts the two keys below.

**If some of your slug values deliberately mean "no issuing body", declare them:**

```yaml
plugins:
  issuing_body_slug_field: "agency"
  issuing_body_slug_sentinels: ["statewide"]   # NEW: values meaning "attributed to no body"
```

Two things change for that corpus, and the second is the one to plan for.

`attribution.complete` can finally be `true`. ERF's 37,991 `agency: statewide` documents were
indistinguishable from typos, so every per-agency count was labelled a lower bound
permanently, for a reason that was 99.997% legitimate.

**Declared values are now VALIDATED, and that is the half that can fail your CI.**
`corpus-validate-frontmatter` errors when a declared slug is neither a registry entry nor a
declared sentinel — the check the path-derived half of this join has always had. Run it
locally before you adopt the declaration: the errors are the point, but you want them on
your terms rather than in the middle of a pin bump. ERF's single `external` document is
exactly this case and needs a decision — sentinel, or a data fix.

**A sentinel no longer falls back to the path-derived slug.** It is the corpus asserting "no
body", so re-attributing such a document by its directory would contradict the corpus. This
is the value change behind the schema bump. The fallback for a genuine **typo** is
unchanged — an unchecked value still never displaces the CI-validated path slug.

**If you supply `plugins.retrieval_module` and implement `holdings_for`**, add the
`declared_no_body` count to your `coverage` mapping *if your corpus declares sentinels*.
Without it, your backend has classified every sentinel document as `no_registry_entry` — its
split is wrong rather than merely incomplete — and the toolkit reports coverage as unknown
instead of serving a `complete: false` you would have to disbelieve. A backend serving a
corpus with no sentinels needs no change.

### v1.26.0 — `issuing_body_profile`'s counts, and one optional declaration

**Every corpus rebuilds its FTS cache once.** The index schema version goes to 3 because
`issuing_body_slug` now holds a resolved slug rather than a path-derived one — same column,
different values, which no content-key check can notice.

**Rebuild it deliberately; it does not always happen on its own where you think.** A corpus
that BAKES its index into the image (ERF's Dockerfile pre-builds it, ~70s at 76k documents)
needs a fresh image: `./scripts/deploy.sh <corpus> <ref> --rebuild-image`, which
`platform-deploy/scripts/deploy.sh` documents as "needed after any toolkit release that
changes the FTS cache schema" — this release is one. A MOUNTED corpus rebuilds out of band
in `deploy_mounted()`, which stops the service for roughly 8 minutes while it warms. Only a
plain local run rebuilds silently on first use. After deploying, check the document count:
`deploy.sh` aborts below `MIN_DOCS`, and that abort is the thing standing between you and an
empty index served green.

> **CORRECTION (corpus-toolkit#114): `--rebuild-image` is not needed, and does nothing
> today.** The flag is guarded on a corpus being MOUNTED, and `platform-deploy` mounts none
> — `deploy.sh` declares an empty mounted set and prints a notice if you pass it anyway.
> Because nothing is mounted, an ordinary `deploy.sh <corpus> <ref>` builds the image every
> time, so the index is re-baked as a matter of course.
>
> Measured on the v1.27.0 pin wave: ERF's image was rebuilt by the ordinary
> reconcile-triggered deploy with no flag passed, and the corpus answered on the new commit
> with all 75,905 documents.
>
> **The `MIN_DOCS` sentence above has the same defect.** `deploy.sh`'s document-count abort
> lives inside `deploy_mounted()`, and its own comment calls it "the replacement for the
> build-time content gate" — it exists *because* a mounted corpus has no Dockerfile bake to
> guard it. A BAKED corpus gets no post-deploy count check; its guard is the Dockerfile's
> build-time `RUN … ensure_index()`, which fails the image build instead. So the safety net
> is real but it is a different one, and checking the count after deploying is still worth
> doing by hand.
>
> **What remains true:** an FTS schema bump does require a rebuild, and for a baked corpus
> that rebuild happens at image build. Both the flag and the `MIN_DOCS` abort become live
> again the day a corpus is mounted, which is why this is a correction rather than a
> deletion.

**Nothing else is required, and a corpus that changes nothing keeps its current counts.**

**If your documents carry the registry slug in frontmatter, declare which key** — this is
the fix for corpus-toolkit#71 and it is opt-in per corpus:

```yaml
plugins:
  issuing_body_registry: "_meta/catalog/agencies.yml"
  issuing_body_slug_field: "agency"      # NEW: the key carrying the registry slug
```

Expect the reported numbers to jump when you do — `executive-regulatory-frameworks`,
measured 2026-08-18: documents counted for a registry body go from 960 of 75,905 (1.3%) to
37,913 (49.95%), and `department-of-environmental-quality` from 53 documents to 1,929. That
is correct and it is also a number people may have quoted, so land the declaration in its
own PR with the measurement in the body rather than folding it into a routine pin bump.

**No count goes down**, in that PR or this bump: a declared value only wins where it names a
registry entry, so a typo cannot displace a path-derived slug that CI has already validated.

**Expect `attribution.complete: false` afterwards if you use sentinel values.** ERF's other
37,992 documents (50.05%) carry `agency: statewide` (37,991) or `agency: external` (1),
which the registry does not contain. The toolkit cannot tell a deliberate sentinel from a
typo, so it reports them as counted for no body rather than assuming they are fine — honest,
and noisy until corpus-toolkit#94 adds the sentinel declaration. `external` on ERF is one
document and wants a decision either way.

Declare the key that holds a **registry slug**, not the free-text `issuing_body` descriptor
— on ERF that field is a sub-unit name ("DAS Enterprise Information Strategy and Policy
Division") and matches no registry entry.

**If your corpus supplies `plugins.retrieval_module`**, its `holdings_for(slug)` may now
return `{"counts": {content_mode: n}, "coverage": {"documents": n, "in_registry": n,
"no_registry_entry": n, "unattributed": n, "basis": str}}`. The old bare
`{content_mode: n}` still works and is not deprecated — a backend returning it, or returning
coverage without those counts, reports `attribution.complete: null`, because half a
measurement is not a measurement and "did not check" is not "nothing is missing". Report the
buckets if you can classify against the registry; omit them if you cannot.

### v1.27.0 — object-shaped tools declare response convention 1

**No action for a corpus using the built-in file backend, which is all eight live corpora.**
Response bodies are unchanged; only the declared output schema moved, from
`{"additionalProperties": true}` to the same thing plus `corpus`, `archetype` and
`authoritative_source` as named, required properties with `authoritative_source` typed
`string | null` (corpus-toolkit#15). Verified whole-payload equal for every registered tool
on both SDK majors before shipping — this is the change that must not repeat v1.24.0, and
`tests/test_result_marshalling.py` is what proves it did not.

**One thing to check if your corpus supplies `plugins.retrieval_module`.** The three fields
are now *required* by the declared schema, so a response missing any of them raises at
serialization instead of going out quietly non-conforming. The toolkit builds them in
`CorpusFramework._envelope()` on every path, so the only way to lose one is a backend record
that overrides it: `get_document` merges the backend's record **over** the envelope, so a
record carrying `corpus`, `archetype` or `authoritative_source` with a non-string value
(`None` for the first two, a list, a number) now fails the call. A record that simply omits
them is fine — the envelope supplies them.

Grep your backend's `get()` for those three keys. Returning `authoritative_source` as a
document's `source_url` string is the supported case and is what `FileBackend` does.

> **Superseded by corpus-toolkit#102 and #104 — the advice above is now only about
> `get_document`'s success branch.** The warning was written when a backend mapping could
> displace the envelope, and it was also short of the mark in two ways: it named only
> `get()`, while `corpus_overview` merged `overview()` over the envelope in exactly the same
> way, and it told you to check for a *non-string* while a plausible **string** — a proxy
> backend naming its upstream feed under `corpus` — was served silently and misreported
> which corpus was answering.
>
> Both sites now re-assert the envelope after the backend's mapping, so on **those two
> paths** — `get_document`'s not-found branch and `corpus_overview` — returning any of the
> three keys is harmless: the toolkit ignores them rather than obeying or erroring on them.
> `corpus_overview` also protects `disclaimer` and `jurisdiction` the same way.
>
> **Keep grepping `get()`, for its SUCCESS return.** That branch is deliberately unchanged:
> a record's `authoritative_source` still wins, because a document's own `source_url` is the
> more precise answer to "where is the official text" and that is what `FileBackend` relies
> on. So the original warning still applies there in full — a **non-string**
> `authoritative_source` on a successful record (a list, a number) is truthy, never falls
> back to the config value, and is a hard `ValidationError` at serialization. Returning a
> `source_url` **string** is the supported case; returning anything else is not.
>
> So the check to keep is narrower than the one above, not gone: `get()`'s success return
> may carry `authoritative_source` and it must be a string or absent; it should not carry
> `corpus` or `archetype` at all, and if it does they are now ignored rather than obeyed.
>
> **And `source_url` joined that checklist in corpus-toolkit#90.** On the success branch the
> slot is now resolved by precedence — the record's `authoritative_source`, then its
> `source_url`, then the corpus front door — so `source_url` decides what an agent is told
> to verify against whenever the record carries no explicit `authoritative_source`. Two
> things follow for a custom backend:
>
> * **It must be a string, or it is ignored.** A non-string `source_url` (a list of mirrors,
>   a `Path`, an int id) is declined and the resolution falls through to the front door,
>   rather than being promoted into a field declared `str | None` and failing the call. So
>   this one cannot break you — but it will quietly not do what you meant.
> * **It should be where a READER verifies the text, not where your backend fetched it.**
>   For an `api` or `hybrid` corpus those can differ: if `source_url` holds an authenticated
>   JSON endpoint, that endpoint is now what the response cites. Put the human-readable
>   official-text URL there and keep the fetch endpoint under a key of your own.
>
> `RetrievalBackend.get()`'s contract is otherwise unchanged and `authoritative_source`
> stays optional on a record — that optionality is exactly why the precedence lives in the
> framework rather than in every backend's memory.

**Why required rather than optional.** `authoritative_source: null` means this corpus
declares no front door; an absent key means nobody answered. Those are opposite answers and
the platform does not let them collapse — a default would have injected the first for a tool
that gave neither.

**Extension tools are unaffected and still declare nothing.** A `tools_module` tool
annotated bare `-> dict` emits no output schema and no structured content on either major;
that is corpus-toolkit#96. If you want yours to advertise the convention, annotate it
`-> ResponseEnvelope` from `corpus_toolkit.mcp.responses` — the same open model the built-ins
use — and keep returning a plain dict.

> **The last sentence is WRONG and corpus-toolkit#96 corrects it — see the v1.27.0 entry
> above.** "Keep returning a plain dict" reads as "change the annotation and nothing else",
> which is the one thing that does not work: the three envelope fields are required with no
> defaults, so a tool annotated `-> ResponseEnvelope` whose payload lacks them is a hard
> `ToolError` on every call, not a tool that advertises more. It has to return a dict
> CARRYING THE ENVELOPE, which is what `framework.with_envelope(payload)` is for. Left in
> place rather than deleted because this is the section a maintainer lands on when they
> search for `ResponseEnvelope`, and a silently-removed instruction teaches nothing.

### Adopting any of these in a corpus repo

1. Bump **both** pins (`uses:` and `toolkit-ref:`) — and the third, if the repo installs the
   toolkit in a job of its own. They are separate knobs and drift silently
   (corpus-toolkit#9).
2. If the bump crosses v1.19.0, confirm `corpus.archetype` is set and every reusable
   workflow call passes `toolkit-ref:`.
3. Re-run the full CI, not just the changed job. The gates that catch a bad bump —
   provenance, frontmatter, generated artifacts — are not the ones a pin change looks like
   it touches.
4. If the bump crosses corpus-toolkit#105, run the reserved-relation check in that entry
   against `_meta/graph.json`. It is the one thing here that **no** gate covers: the
   frontmatter schema cannot see it, your `build_graph --check` cannot see it, and the
   toolkit's own release gate cannot see it — so a collision is first observed in
   production, on a graph-tool call.
