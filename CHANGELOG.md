# Changelog

Release notes for `corpus-toolkit`, the shared platform every OregonAI corpus pins.

This file exists because `docs/reference-architecture.md` mandates a CHANGELOG in the repo
anatomy every corpus must follow, and `repo.py` hardcodes `CHANGELOG.md` into
`NON_CONTENT_NAMES` on the assumption it exists — while the toolkit itself had none
(corpus-toolkit#41). The de-facto changelog was `git log`, which is good prose but not
something a downstream can read when deciding whether to move a pin across 27 tags.

**Entries before v1.19.0 are back-filled from tag messages and merge commits.** They are
accurate about what shipped and deliberately terse; `MIGRATION.md` carries the upgrade
notes and the reasoning, and remains the file to read before moving a pin.

The audience is a corpus deciding whether a bump is safe, so each entry leads with whether
it can break you.

## Unreleased

### Fixed — a graph relation name can no longer displace a response key

**Can affect you only if your `_meta/graph.json` declares a relation named `corpus`,
`archetype`, `authoritative_source`, `id` or `title`.** None does today — the relation types
in use across the platform are `references_external`, `related`, `supersedes`, `implements`
and `implemented_by` — so this is protective rather than a migration.

If one ever did, **`graph_neighbors` stops answering for that corpus** and returns an error
naming the relation, the reserved set and the graph file. Only that tool:
`corpus_overview`, `resolve_citation` and `authority_chain` share the graph loader but
cannot have a key displaced by a relation name, so they keep working. **The failure is
lazy** — the graph is parsed on the first graph-tool call, not at boot — so a collision is
first observed in production, and no CI gate catches it. MIGRATION.md carries a one-line
check to run before bumping.

`graph_neighbors` writes one response key per edge-relation type, after the envelope, and
nothing constrained those names. A relation named `corpus` overwrote that envelope field
with a list of neighbour records — a hard `ValidationError` since the envelope types it
`str`, so the tool stopped answering for that document. `id` and `title` were worse:
overwritten with **no error at all**, because the envelope model constrains only its own
three fields, so a caller received a list where it expected a document id.

This is the third and last site of the class the two entries below close. `authority_chain`
was audited and is safe — it prefixes every configured relation as `up_{name}`/`down_{name}`,
so a colliding name cannot reach the response.

**The remedy differs from the other two deliberately.** Those merged a *backend's* mapping
over a response and were fixed by re-asserting the framework's keys last: the backend had no
business setting them, so ignoring it costs nothing. A graph relation is the corpus's **own
declared edge**, and silently dropping it would be data loss rather than enforcement — the
author would never learn their relationship had stopped being served.

Detection runs where the graph is parsed, so it costs once per corpus (measured at 0.2s for
ERF's 75,905-node graph); only the reporting lives in the tool. An earlier draft raised from
the loader instead, which took down `corpus_overview` — the tool the server's own
instructions say to call first — along with `resolve_citation` and `authority_chain`, and
reported a condition other than the one that occurred, which convention 5 forbids.

Closes corpus-toolkit#105.

### Fixed — a backend can no longer displace the response envelope

**Can break you only if your corpus supplies `plugins.retrieval_module` AND its `get()` or
`overview()` deliberately sets `corpus`, `archetype` or `authoritative_source`.** Those
values are now ignored rather than obeyed. If you were relying on a backend to set them —
which nothing documented and the envelope exists to prevent — the response will now carry
the config's values instead. `FileBackend` corpora are unaffected.

`CorpusFramework` merges a backend-supplied mapping into a response at exactly two sites,
and at both the mapping won over the envelope: `get_document`'s **not-found** branch merged
the backend's error record over it, and `corpus_overview` merged `overview()` over it. A
wrong string was served silently — misattributing which corpus said "no such document", and
misreporting the corpus's own identity on the tool a client calls first — and since #103 a
non-string was a hard `ValidationError` at serialization. `corpus_overview` also stopped a
backend replacing `disclaimer`, which had let an upstream's terms of use displace the
NON-AUTHORITATIVE warning that response convention 4 names that tool as carrying.

`get_document`'s **success** branch is not touched by this change: a record's
`authoritative_source` still wins, because a document's own `source_url` is the more precise
answer. It is changed by the next entry. See MIGRATION.md — the check to keep on your
backend is narrower than before, not gone.

Closes corpus-toolkit#102 and #104. A third site of the same class, where the mapping comes
from graph data rather than a backend, is corpus-toolkit#105 and is not fixed here.

### Added — a corpus's own tools can satisfy response convention 1

**Nothing breaks and nothing is required.** Extension tools registered through
`plugins.tools_module` keep working unchanged; this adds the means to close a gap, and the
fix is a follow-up in each corpus repo.

Every extension tool on the platform is annotated bare `-> dict`, which makes the SDK emit
**no output schema and no structured content at all**. The answer is in the JSON text block,
which is why nobody has noticed — but a client reading structured content sees a hybrid
corpus as having half a tool surface, with no error anywhere, while every built-in returns a
parsed object. Those responses also carry none of convention 1's three fields, and the two
facts were one problem: the toolkit offered extension tools no supported way to satisfy the
convention.

`CorpusFramework.with_envelope(payload)` is the supported accessor — the same single
assembly point the built-ins use, merged in the same direction they merge it:

```python
@mcp.tool()
def list_datasets() -> ResponseEnvelope:                  # was: -> dict
    return framework.with_envelope({"datasets": [...]})
```

It merges the envelope **over** the payload, so a corpus's own key can never displace the
three fields — the rule the entry above enforces for backends, applied to the tools corpora
write.

**Both lines or neither.** The three fields are required with no defaults, so annotating the
return type while leaving the payload alone is a hard `ToolError` — the tool stops answering
rather than answering weakly. None of the five live extension tools emits any of the three
today, so an annotation-only sweep would take out both hybrid corpora. See MIGRATION.md.

A list-shaped extension tool needs no change: `-> list[dict]` is exempt for the same reason
`search_corpus` is, and the exemption is about shape rather than which module registered the
tool.

Closes corpus-toolkit#96 (the toolkit half). The five annotation changes in
`oregon-legislature` and `oregon-budget` are follow-ups after this release is pinned.

### Added — a corpus can declare which slug values mean "no issuing body"

**Every corpus rebuilds its FTS index once on this bump** (`SCHEMA_VERSION` 3 → 4). A corpus
declaring no sentinels indexes identically to before and pays only the rebuild. **A custom
backend implementing `holdings_for` needs a fourth coverage bucket, `declared_no_body`, only
if its corpus declares sentinels** — without it that backend has counted every sentinel
document as `no_registry_entry`, so it degrades to `complete: null` rather than reporting a
wrong answer. Three-bucket backends serving corpora with no sentinels are unaffected. Note
the coverage key `declared_no_body` is what a backend emits; the response field callers read
is `documents_declared_no_issuing_body`.

`plugins.issuing_body_slug_field` let a corpus name the frontmatter key carrying its registry
slug, but nothing checked the values and nothing let a corpus say which non-registry values
were deliberate. So a misspelling attributed a document to a body that does not exist and
reached no per-agency count silently, while ERF's 37,991 `agency: statewide` documents — 
correct, and carrying no agency by design — were indistinguishable from misspellings. That
made `attribution.complete` report `false` permanently, for a reason that was 99.997%
legitimate, which is the fastest way to teach callers to ignore the field.

```yaml
plugins:
  issuing_body_slug_field: "agency"
  issuing_body_slug_sentinels: ["statewide"]   # values meaning "attributed to no body"
```

Sentinels get their **own** coverage bucket (`documents_declared_no_issuing_body`) and are
never folded into the registry-matched count: "counted for a registry body" and "deliberately
counted for no body" are different answers, and `CONTEXT.md` forbids collapsing two distinct
answers. A corpus where every document either names a registry entry or carries a declared
sentinel now reports `complete: true`.

The declaration is only safe because the values are now **validated**:
`corpus-validate-frontmatter` errors when a declared slug is neither a registry entry nor a
declared sentinel — the check the path-derived half of the same join has always had. Without
it, the sentinel list would be a way to silence the coverage warning rather than answer it.

A sentinel also stops falling through to the path-derived slug. It is the corpus positively
asserting "no body", so re-attributing such a document by its directory contradicts the
corpus about its own document — that is the value change behind the `SCHEMA_VERSION` bump.
The fallback for a genuine **typo** is unchanged: an unchecked value still never displaces
the CI-validated path slug.

Closes corpus-toolkit#94. No corpus declares `issuing_body_slug_field` yet, so nothing
changes on the platform until one adopts both keys.

### Fixed — `corpus.*` string fields are type-checked at load

**Can break you if your `corpus.yml` has one of these wrong** — and if it does, it is
already broken at runtime. `id`, `name`, `jurisdiction` and `authoritative_source` must now
be strings, and a bad one is a `ValueError` naming the field instead of a failure later.
All ten corpus configs on the platform load unchanged; this was verified against each.

`authoritative_source` was stripped without a type check, so a non-string raised
`AttributeError: 'list' object has no attribute 'strip'` — naming neither the file nor the
key, and pre-empting the URL validator downstream whose whole job is to say something useful
about this field.

`id`, `name` and `jurisdiction` had no check at all, so a non-string was accepted in
silence. `id: 90210` loaded as an int; unquoted `id: no` loaded as boolean `False`, because
PyYAML resolves `no`/`off`/`false` and `yes`/`on`/`true` — in any capitalisation — as
booleans. Since the `ResponseEnvelope` entry above types `corpus` as `str` and `config.id`
fills that slot on all six object-shaped tools, that made a single unquoted `id: no` a
`ValidationError` on **every tool call** — at runtime, on a corpus whose config had loaded
cleanly. The error names the trap and tells you to quote **the word you already wrote**:
`corpus.id` is also the MCP server name and how siblings cross-reference this corpus, so
advice that changed the value would quietly rename it.

The `corpus:` block itself is checked too. Present-but-not-a-mapping — an empty block, or
one mis-indented so its fields land elsewhere — used to raise `AttributeError: 'NoneType'
object has no attribute 'get'`, the same shape as the reported bug and a far more common
authoring mistake. An absent `corpus:` key keeps its existing default.

Closes corpus-toolkit#89.

### Fixed — `get_document` cites the document, not the corpus front door, for every backend

**Can change what your responses say only if your corpus supplies
`plugins.retrieval_module`.** If your backend's `get()` returns a `source_url` and no
`authoritative_source`, that slot changes from the corpus front door to the document's own
URL. That is the fix. Nothing breaks, but a response's `authoritative_source` may now differ
from what the same call returned before, so a downstream asserting on it should re-check.
`FileBackend` corpora are unaffected — provably, because both keys come from one column.

The fallback tested the ASSEMBLED RESPONSE's slot rather than the record's `source_url`, and
`_envelope()` has already put the front door there. So for any corpus declaring a front door
the test could never be true and the fallback could never fire. A backend honouring the
documented `get()` contract — "Record metadata + body", which nowhere requires
`authoritative_source` — had the front door stamped over a per-document URL sitting in the
same payload: a wrong answer rather than a missing one, with nothing erroring. It bit hardest
on the `api` and `hybrid` archetypes, the ones that ship a `retrieval_module`.

Resolution is now by precedence, read from the record: its own `authoritative_source`, then
its `source_url` **if that is a string**, then the corpus front door (which may be `null`).
The type check is why a non-string `source_url` — a list of mirrors, say, which the protocol
has never forbidden — stays a harmless extra key instead of becoming a `ValidationError`.

`RetrievalBackend.get()`'s contract is unchanged and `authoritative_source` stays optional on
a record. But `source_url` is now load-bearing on the success path, and the protocol
docstring says so: it should be where a reader verifies the official text, not the endpoint
the record was fetched from.

Closes corpus-toolkit#90.

### Added — object-shaped tools declare response convention 1, openly

The six object-shaped tools are annotated `-> ResponseEnvelope` (new,
`corpus_toolkit/mcp/responses.py`) instead of `-> dict[str, Any]`. Their emitted output
schema goes from this, on both SDK majors:

```
get_document  {"additionalProperties": true, "title": "get_documentDictOutput", "type": "object"}
```

to this:

```
get_document  {"additionalProperties": true, "title": "ResponseEnvelope", "type": "object",
               "required": ["corpus", "archetype", "authoritative_source"],
               "properties": {"corpus": {"type": "string"},
                              "archetype": {"type": "string"},
                              "authoritative_source": {"anyOf": [{"type": "string"},
                                                                 {"type": "null"}]}}}
```

`corpus`, `archetype` and `authoritative_source` were in every response body and named by
no declaration, so a conformance harness, a validating client or a release gate could
assert nothing about the convention beyond string-matching prose (corpus-toolkit#15).
`search_corpus` is untouched — it returns a list and is exempt from the convention.

**Response bodies do not change.** The tools still return the same plain dicts; only the
declared type moved. Verified by `tests/test_result_marshalling.py`, which round-trips
every registered tool's real answer through the SDK's own conversion and asserts
whole-payload equality in both halves, on both majors: 314 passed, 10 subtests on
`mcp[cli]>=1.28,<2` (1.28.1) and `>=2,<3` (2.0.0), up from 312 with the two new tests.

**Why this is not v1.24.0 again.** That release declared a TypedDict, which the SDK turns
into a CLOSED pydantic model: it rejected the documented `authoritative_source: null` and
dropped every undeclared key, so `get_document` returned three envelope fields and no
document body while still reporting success (corpus-toolkit#61). The distinction is
closedness, not declaration — a `-> dict[str, Any]` annotation already builds
`RootModel[dict[str, Any]]` and dumps every response through it, so a pydantic model has
been serializing object responses all along. `ResponseEnvelope` sets `extra="allow"`, and
its output was measured against that RootModel's on both majors — same keys, same values —
for keys shadowing `BaseModel` methods and attributes, leading-underscore and dunder keys,
the empty-string key, non-ASCII keys, non-JSON values, falsy values and deep nesting.

**Key order in structured content changes for one tool.** `model_dump` emits declared
fields before extras, so `resolve_citation` — which merges `**self._envelope()` last — goes
from `['citation','matches','unresolved','corpus',…]` to
`['corpus','archetype','authoritative_source','citation',…]`. JSON objects are unordered,
the content blocks are built from the raw return value and do not move, and every
round-trip test compares mappings — so nothing can observe it. Recorded because the first
draft of this entry called the output byte-identical, which was a stronger claim than the
measurement supported.

Both v1.24.0 failure modes are re-tested directly and pass: a payload carrying
`authoritative_source: null` round-trips as null through every object tool, and
`get_document`'s body survives in both the structured content and the content blocks. The
gate was also re-armed adversarially — re-applying `daff198`'s `ObjectResponse` TypedDict
on top of this change still turns `tests/test_result_marshalling.py` red with
`get_document: keys dropped at serialization: ['body', 'citation', ...]` on both majors.

**One behaviour change, and it can only fire on a non-conforming response.** The three
fields are declared required (and `authoritative_source` nullable with no default), so a
response that omits one is now a `ValidationError` rather than a quietly non-conforming
answer. Every built-in path builds the envelope in `CorpusFramework._envelope()` and
cannot hit this; a corpus supplying its own `plugins.retrieval_module` can — see
MIGRATION.md.

`.github/scripts/contract_smoke.py` gained the matching assertion at step 8: any tool
whose declared schema describes properties at all must name the three. Extension tools
annotated bare `-> dict` declare no schema and are out of scope there, which is
corpus-toolkit#96 and a separate fix.

## v1.26.1 — 2026-08-19

### Fixed — **v1.25.0 and v1.26.0 cannot build a corpus image; take this one**

**If you are pinned to v1.25.0 or v1.26.0, your corpus image build is failing right now.**
Bump the pin. There is no config workaround, and rolling back to v1.24.1 also clears it.

v1.25.0 deleted `CorpusFramework.ensure_index` (corpus-toolkit#75). `corpus-template`'s
Dockerfile — and therefore every corpus built from it — bakes its FTS index at image build by
calling exactly that:

```dockerfile
RUN python3 -c "... CorpusFramework(config_mod.load('_meta/corpus.yml')).ensure_index()" && ...
```

So step 7 of 9 fails with `AttributeError: 'CorpusFramework' object has no attribute
'ensure_index'`, the image never builds, and a pull-based deploy loop re-detects the same drift
forever. Measured on the deploy host: six consecutive ERF deploy attempts in one hour with
`deployed=` never advancing, each rebuilding a 1 GB context, starving every other corpus behind
it and contributing materially to the host reaching 100% disk.

The method is restored as a real method delegating to the backend, not a deprecation — a corpus
is entitled to ask its framework to build the index, and deprecating it would only move the same
breakage to a later release. A backend with no FTS index (the API archetype) still raises, but
with a message naming which backend and why.

**How it passed every gate.** A search of `corpus_toolkit/` and `tests/` found no caller, and
that was the whole of the evidence — the callers live in the eight repositories that pin this
one. The release gate checks out `corpus-template` and runs `contract_smoke.py` against it, but
**never runs its Dockerfile**, so the one artifact representing how a corpus actually starts was
sitting in the job's working directory unexecuted. Tracked as corpus-toolkit#100.

Worse, #75 added `assert not hasattr(f, "ensure_index")` — a test pinning the deletion, which
made the regression read as deliberate to anyone reviewing the suite. That assertion is replaced
by one exercising the call a corpus makes.


## v1.26.0 — 2026-08-19

### Every corpus rebuilds its FTS index on this bump — plan the rollout

`SCHEMA_VERSION` goes 2 to 3, so the cached index is discarded and rebuilt. **This is not
the usual pin bump.** What it costs depends on how your corpus ships:

| | |
|---|---|
| baked image (e.g. ERF) | the rebuild happens at **image build** — `deploy.sh <corpus> main --rebuild-image`, ~70s on 76k documents |
| mounted corpus | a stop-warm-start, **~8 minutes** |
| local checkout / CLI | rebuilt silently on the next command |

A pin bump alone is **inert** for a baked corpus: `requirements.txt` is baked into the
image, so merging the bump changes nothing until the image is rebuilt. `deploy.sh`'s own
help has said `--rebuild-image` is "needed after any toolkit release that changes the FTS
cache schema" — this is such a release, and until now that flag appeared nowhere in the
toolkit's docs.

Rebuilding under live traffic is unlocked and uses a fixed temp filename, so a concurrent
warm and a live rebuild collide (`disk I/O error`). Rebuild deliberately rather than
letting the first request do it.

### One REQUIRED action, for any corpus whose manifest has empty `sha256` values

`corpus-detect-changes` now exits **1** rather than 0 on a run that recorded no baseline or
checked nothing. A corpus that has never seeded its baselines goes red on its next
scheduled run. That is the point — it was reporting 100% drift as a clean result — and the
remedy is one command, `corpus-detect-changes --record-baseline`, reviewed as a PR. See
MIGRATION.md.

### Changed — an outbound User-Agent string

The sibling-index fetcher identifies itself as `corpus-toolkit/<installed version>` instead
of the literal `corpus-toolkit/1.1`, which had been frozen since v1.1 and wrong for
twenty-four releases (corpus-toolkit#82).

**This is externally visible.** It is the only thing a remote host learns about us on a
sibling-index fetch. A publisher who has allow-listed, rate-limited or logged on the exact
string `corpus-toolkit/1.1` stops matching. Nothing on this platform is known to do so, but
it is the kind of thing an upstream does without telling you, and the contact URL and
`sibling-index-fetch` purpose token are unchanged so anything matching on those still works.

`sources/changes.py`'s `corpus-toolkit-change-detector` — the agent that fetches sources
during change detection — is **unchanged**. It is the token matched against robots.txt
directives, so a host's `Disallow` naming it keeps matching exactly as before.

### Documentation — `authoritative_source` is the corpus's front door

**Nothing mechanical changes and no bump is required for this.** The type stays
`str | None`, no response shape moves, and no validation is added or tightened. What
changes is what the field *means*, which had never been written down.

Response convention 1 in `docs/mcp-interface-contract.md` now states it: the corpus-level
`authoritative_source` names where a reader starts for **this corpus's** official text —
one URL, per corpus — and is not a citation for whatever the response carrying it happens
to describe. Per-answer precision already exists and comes from `get_document`, which
returns the document's own `source_url` in that slot and falls back to the corpus URL only
for a document carrying none.

**What this asks of a corpus**: one spanning several publishers declares its best single
entry point rather than leaving the field unset, and that is correct rather than a
compromise. `executive-regulatory-frameworks` — 1,972 sources across 7 hosts, measured
2026-08-11 — was the one holdout with a *reason*, and this removes it. It is not the last
holdout: `oregon-budget` and `oregon-legislature` are also undeclared, and corpus-toolkit#11
still needs all three before its precondition is met. Neither of those two needed this
settled; each has one dominant host.

The message a corpus sees while the field is unset is reworded to match, in both places it
appears: `corpus-validate-frontmatter`'s warning and `corpus_overview`'s `config_warning`.
Both used to say "set it to the URL where the official text lives", which reads as a
promise that every document sits under that URL — the reading that kept a seven-publisher
corpus from declaring anything at all. A corpus asserting on either string in its own CI
should expect it to have changed. (corpus-toolkit#70)

### Fixed — drift detection could not record a baseline, and a truncated run looked clean

**Read this if your corpus runs `detect-upstream-changes.yml`. Two exit codes change, and
one of them will turn some scheduled runs red on purpose.**

Three defects, one shape: the drift report said things about upstream that were really
facts about the corpus, and said them quietly.

- **`corpus-detect-changes` never wrote the baseline it computed** (#68). The manifest's
  `sha256` was documented as "recorded at last ingest/refresh" and nothing in the toolkit
  ever assigned it, so the only route to one was a per-corpus script reimplementing
  `content_hash` — format inference, volatile normalization, `pdftotext -layout`,
  whitespace normalization, the <200-char raw-byte fallback — where any divergence is
  silent and permanent. oregon-counties (3,447 sources) and oregon-kpm (789) ran their
  whole lifetimes with every `sha256: ''`: everything CHANGED every week, 25 spurious
  issues filed, the rest dropped, run concluded `success`.

  **New: `--record-baseline`.** It writes the freshly computed hash into the manifest group
  files, in the working tree only — the manifest is curated data, so the diff goes through
  review like any other, and nothing is committed or pushed. Bare (`seed`) fills sources
  with **no** recorded baseline and leaves recorded ones alone; `--record-baseline=refresh`
  also replaces recorded baselines, which is you accepting the observed change. A source
  whose fetch failed is never written — a 403 must not overwrite a good baseline. Sources
  are located by id and only their `sha256` value is rewritten; the edit is re-parsed and
  compared before anything is written, and a file that does not verify is left untouched
  and named. Comments, key order, and every other key survive. `--record-baseline` refuses
  to run with `--open-issues`: seeding is not a drift report.

  **Do not seed from frontmatter `source_sha256`.** Different hash, different input. The
  two agree only for image-only scans, where both fall back to raw bytes — so a corpus that
  seeds from frontmatter and spot-checks a scan sees a clean result and gets permanent
  drift on every text-layer PDF, now with a populated "previous" hash that reads as a real
  upstream change.

- **`VOLATILE_PATTERNS` shipped empty with no way for a corpus to add to it** (#66), so
  `normalize_volatile()` was an identity function for every consumer and the guarantee its
  comment describes did not hold. One OARD footer bump (`v2.1.7` → `v2.1.8`) turned all 484
  sources in ERF's `oar` group into drift with zero rule text changed.

  **New: optional `volatile_patterns:` in `_meta/corpus.yml`** — a list of regexes, stripped
  from the raw bytes on the HTML/XML path before hashing. Compiled once at load, and a bad
  one fails there rather than mid-crawl: a bare string, a non-string entry, an empty
  pattern, or an invalid regex is refused by name. **The built-in list stays empty**, so a
  corpus that declares nothing hashes byte-identically to v1.25.0 — shipping "universal"
  defaults would have re-hashed existing sources across the platform in a version bump. A
  declared pattern that matches nothing in a run is reported, because a configured pattern
  doing nothing is the bug this key exists to fix. So is the opposite and worse case: every
  run reports how many bytes each pattern removed and what share of the fetched HTML/XML
  that is, and warns above 10%. A pattern wide enough to swallow the body deletes content
  before hashing — two versions differing only inside it hash identically and can never
  report drift again — and that is measured and stated rather than forbidden, since how
  much of a page is genuinely volatile is a corpus's call to make in a PR.

- **A capped run reported as a clean run, and named a cause it had not checked** (#67). The
  truncation notice went to stderr and the run exited 0; the message asserted an empty
  baseline, which was exactly right for oregon-counties and exactly wrong for ERF, whose
  maintainer checked and found zero. Both runs went green either way.

  Every run now prints a **per-group breakdown** (`oar 484/484, oam 2/173`), capped or not,
  with unseeded counts marked — the one line that separates a template change from a stale
  baseline from real revisions. The capped message describes the shape of the drift and
  reports the **measured** unseeded count instead of guessing, including when it is zero.

**Exit-code changes.** A run now exits 1 when the issue cap truncated the report; when no
in-scope source has a recorded baseline (that run cannot detect drift; `--record-baseline`
is the fix, and a recording run exits 0); when the run's scope came out **empty**, e.g. a
typo'd `--group` that checked 0 sources; and when `--record-baseline` **refused** a rewrite
it could not account for, which in CI was otherwise a green run that recorded nothing.
`--github-output` gains `unseeded=N` and stops reporting `changed=true` on an inert run. Under GitHub Actions both also emit a `::warning`
annotation. An uncapped, seeded run is unchanged: drift is still a signal, not an error, and
isolated fetch failures are still tolerated. `detect-upstream-changes.yml` now runs its
STATUS.md steps with `if: always()`, so a red drift step no longer skips them.

A corpus with a wholly unseeded manifest also stops having issues filed against it — the
first run against a fresh manifest is a seeding operation, and 25 tickets a week whose
"previous sha256" is empty were noise. Seed, review the diff, then let the cron report.

### Fixed — `issuing_body_profile` counted 1% of a corpus and said nothing about the other 99%

**The number moves, a lot. That is the point of this note.** `in_repo` was counted from an
index column populated only for documents under a `scoped: true` content root. Measured on
`executive-regulatory-frameworks` on 2026-08-18: 960 of 75,905 documents, **1.3%**. Its
Department of Environmental Quality reported **53** documents against the **1,929** that
actually carry it — a **97% under-report, ~36×**, and the same order for every large agency,
because an agency's OAR rules are filed under their chapter and no agency directory can
contain them. Nothing about the response said so: the call succeeded and the field was
populated, so a caller comparing agencies got a ranking of *who has a policy directory*.

Two changes, and a corpus needs the first to see the second.

- **A corpus may declare `plugins.issuing_body_slug_field`** — the frontmatter key carrying
  its registry slugs (`agency` on ERF, present on 100% of documents). It wins **where its
  value names a registry entry**; otherwise the path-derived scope slug, which CI already
  validates, keeps the document. That order is deliberate: nothing checks the frontmatter
  field (corpus-toolkit#94), and letting an unchecked value override a checked one means a
  single typo silently REMOVES a correctly-filed document from a count that was previously
  right. **No count can go down because of this release**, and a corpus that declares
  nothing reports exactly the counts it reported before. The path mechanism is not
  deprecated: a corpus genuinely organised by issuing body is served correctly by it.
- **Every success now carries `attribution`**, saying what the count could see — as three
  buckets, not a boolean, because "has a slug" and "is counted for somebody" are different
  questions. `documents_matched_to_a_registry_entry` are the only ones any per-body count
  can include; `documents_naming_no_registry_entry` and `documents_with_no_issuing_body`
  are counted for nobody. `complete: true` means the first bucket is everything; `false`
  means the count is a **lower bound**, with the numbers; `null` means nobody measured — an
  old-shape backend, coverage reported without its counts, or an empty index — which is
  unknown, not none. `in_repo` for a body with nothing held says which nothing it means:
  the old "no documents ingested for this issuing body yet" is now reserved for a corpus
  where everything reaches a count.

**Expect `complete: false` on ERF, and that is the honest answer.** 37,992 of its 75,905
documents (50.05%) carry `agency: statewide` (37,991) or `agency: external` (1) — values the
registry does not contain. From the toolkit those are indistinguishable from a typo, so they
are reported as counted-for-nobody rather than assumed deliberate. corpus-toolkit#94 adds the
sentinel declaration that lets a corpus say which values mean "no issuing body", after which
ERF reports complete.

**Contract stays v1** — additive fields, `in_repo`'s own shape unchanged,
`docs/mcp-interface-contract.md` updated in the same change. **The FTS schema version is
bumped to 3**, so every corpus rebuilds its `_meta/.cache` index once; a baked image needs
`deploy.sh … --rebuild-image` (see MIGRATION), because without the rebuild the old values
keep being served from a cache nothing else would invalidate.

`RetrievalBackend.holdings_for(slug)` now returns `{"counts": ..., "coverage": ...}`. A
corpus-supplied backend still returning v1.25.0's bare `{content_mode: count}` keeps
working unchanged and reports coverage `null`. (corpus-toolkit#71)

Known gap, tracked as corpus-toolkit#94: nothing checks that a declared slug value names a
registry entry. It can no longer shrink a count — an unregistered value never overrides a
validated path — but for a document no directory attributes, a typo still lands in
`documents_naming_no_registry_entry` and is counted for no body. Live on ERF today at one
document (`agency: external`), alongside 37,991 deliberate `statewide`.

### Internal — what a client RECEIVES is now asserted, for every tool

**Nothing about a corpus's behaviour changes and no bump is required for this.** No
response shape moves, no annotation changes, no validation is added or tightened. It is
test and release-gate coverage, plus two functions on the SDK compat seam.

Every assertion this repo made about a tool — in `tests/`, in the release gate, everywhere
— went through `_sdk.call_tool`, which passes `convert_result=False` and therefore sees the
tool's raw Python return value rather than the response a client is sent. That is
deliberate and stays: the gate asserts that an external graph neighbour comes back
`{citation, external: true}`, and asserting that through the SDK's marshalling would test
the SDK. The gap was that nothing asserted the marshalling either — which is how v1.24.0
shipped an output schema that dropped every document body on the way out, reported success
doing it, and passed the `corpus-end-to-end` gate green (corpus-toolkit#61, #63).

Added, without flipping that flag, so behaviour and marshalling stay separately pinned and
a failure says which one broke:

- `tests/test_result_marshalling.py` round-trips EVERY registered tool's real answer, from
  a real corpus on disk, through the SDK's own conversion and asserts whole-payload
  equality in both halves of the response — the content blocks a client renders and the
  structured content it parses. It covers what `tests/test_output_schemas.py` (which pins
  response convention 1 on the six object-shaped tools) structurally cannot: `search_corpus`,
  whose list answer takes a different conversion path entirely — one content block per hit,
  wrapped as `{"result": [...]}` — and the `tools_module` extension tools a hybrid or api
  corpus registers, which nothing reached at all. Its fixtures declare an
  `issuing_body_registry` so `issuing_body_profile` — config-gated, one of the six tools
  v1.24.0 annotated, and previously round-tripped by nothing anywhere — is actually served,
  and its coverage guard fires in both directions so a listed-but-unregistered tool cannot
  read as covered.
- Step 8 of `.github/scripts/contract_smoke.py` does the same against the corpus the gate
  already builds, on the same calls it already makes, including the hybrid extension tool.
  It also pushes `authoritative_source: null` through each object tool's own converter,
  which is the other half of what #61 broke. Both round trips treat "no structured content"
  as legitimate only when the tool DECLARED no output schema; declaring one and serializing
  nothing is reported as the regression it is.
- `_sdk.serialized_result()` returns both halves of a conversion, `_sdk.tools_by_name()`
  returns the registered tool objects, `_sdk.declares_list_result()` answers whether a tool
  declares the SDK's list wrapper (so a caller keys on the declared shape rather than on a
  tool's name), and `structured_result()` is now the narrow form of the first. A third result shape the seam did not know about is handled: on mcp 1.x a tool
  with no declared output schema converts to a bare list of blocks rather than a
  `(blocks, structured)` tuple. Every extension tool on the platform is in that state, which
  is corpus-toolkit#96.

Verified by re-applying v1.24.0's `TypedDict` annotation: the new coverage goes red on both
SDK majors, naming each dropped key and each rejected null, while the existing behaviour
assertions stay green — the exact green that shipped the incident.

## v1.25.0 — 2026-08-11

### One behaviour change on the serve path, then fixes

**Read this if your corpus sets `plugins.semantic_search_module`.** The shared semantic
module now resolves its embeddings artifact from `config.root`, not from the process's
working directory. In the containers those are the same path (WORKDIR is the repo root),
and `CORPUS_SEMANTIC_DIR` still overrides both, so a normal deployment is unaffected — but
it is the code path every semantic query runs, so rebuild deliberately rather than letting
it ride along on an unrelated image build. A corpus that builds its artifact with
`--out` still needs `CORPUS_SEMANTIC_DIR` set at serve time; that was true before and has
not changed.

Nothing else changes for a corpus. No `_meta/corpus.yml` edits, no schema change, no MCP
contract change. The four items below came out of an architecture review of the retrieval
and plugin seams (corpus-toolkit#73, #74, #75).

### Fixed

- **Citation schemes silently vanished on a second `CorpusFramework` over one corpus**
  (#73). `load_module` caches a corpus's citation module, so the second construction re-ran
  none of its top-level `register_scheme` calls and collected nothing — then fell back to a
  process-wide list the collector had deliberately bypassed, which was therefore empty.
  `resolve_citation` answered *"no citation scheme recognized this format"* about a corpus
  that recognizes it perfectly well, reported `schemes_attempted: []`, and skipped sibling
  resolution entirely — so a sibling citation came back `unresolved` with no
  `sibling_unavailable` marker. That is "could not check" served as "not there".

  **No deployed server hit this**: `server.py` builds one framework per process. It was
  reachable from a corpus's own `tools_module`, a CLI, or any multi-corpus process.

- **The semantic seam had no per-corpus state** (#74). The plugin contract passed no
  corpus, so the module read `Path.cwd()` for its artifact path and kept its loaded index in
  a module global — one installed module object shared by every framework in the process.
  A server started outside the repo root served keyword-only while reporting healthy, and
  two corpora in one process shared whichever index loaded first. The builder never had this
  problem (`semantic/build.py` has always written to `cfg.root/_meta/embeddings`), so the two
  halves of one artifact disagreed about where it lives.

- **`corpus_toolkit.semantic.search.selftest()` crashed** and had for some time. Its
  synthetic fixture was a 5-tuple while the loader had grown to 6 when `rank_chunks` added
  `rows`, so two of its four checks had not executed since — including the one guarding the
  degrade path `backends.py` calls with no `try`/`except`. It was written to run without the
  artifact specifically so CI could run it, and CI never called it. It does now, and `numpy`
  joins the `test` extra so the check runs rather than skipping.

### Added

- **`RetrievalBackend.holdings_for(slug)`** (#75) — optional, and the only optional member
  of the protocol. `issuing_body_profile` used to run raw SQL against `FileBackend`'s `docs`
  table through `ensure_index()`, so the tool was unavailable to any other backend **at any
  price**, and three separate guards existed to keep that from surfacing as a crash. A
  corpus-supplied backend can now serve the tool by implementing one documented method; the
  startup message says so when it does not. `FileBackend` implements it, so a
  document-archetype corpus does nothing.

- **`plugins.load_module(..., force=True)`** — re-executes a module already in
  `sys.modules`, for the case where the import *is* the effect. Keyword-only and off by
  default; only the citation-scheme collector passes it.

- **`corpus_toolkit.semantic.search.make(config)`** — the per-corpus factory
  `CorpusFramework` now prefers. A semantic module without it is duck-typed exactly as
  before.

### Internal

- `extract_section` is a module function in `backends.py`. `CorpusFramework._extract_section`
  called it as `FileBackend._extract_section(self, ...)` — an unbound method of an unrelated
  class handed a `CorpusFramework` as `self`, which held only while the body ignored `self`.
  Both classes keep the name.
- `DOC_*` column constants for `_doc_row`, and the existing `KW_*` applied to the
  `_doc_meta_row` readers that were still positional. `tests/test_row_offsets.py` executes
  the real queries and asserts each constant lands on the column it names, so a reordered
  `SELECT` fails there instead of serving a document with its citation in the `title` field.
- `REQUIRED_BACKEND_METHODS` moved next to the protocol it restates.
- First tests for `issuing_body_profile` — `issuing_body_registry` previously appeared
  nowhere in `tests/`.

Suite: 205 tests → 231.

## v1.24.1 — 2026-08-04

### Fixed — **v1.24.0 is broken; take this one**

**If you are pinned to v1.24.0, every object-shaped tool on your corpus is failing right
now.** Bump to v1.24.1 and rebuild the image; there is no config workaround, and rolling
back the pin to v1.23.x also clears it. A pin-bump PR should have been opened against your
corpus automatically — merging it is not enough on its own, the image must rebuild.

v1.24.0 declared a `TypedDict` output schema on the six object-shaped tools
(`get_document`, `resolve_citation`, `graph_neighbors`, `corpus_overview`,
`authority_chain`, `issuing_body_profile`). That made the SDK serialize every response
through a pydantic model, which broke two things at once (corpus-toolkit#61):

- **`authoritative_source: null` was rejected.** It is a documented value for a corpus
  that declares no source, so `corpus_overview`, `resolve_citation` and unknown-id
  `get_document` returned a hard `ValidationError` instead of a response.
- **Document bodies were dropped.** Keys the schema did not declare were discarded on the
  way out, so `get_document` returned its three envelope fields and no content —
  **silently**, with the call still reporting success. This is the dangerous half: an
  agent gets a well-formed answer containing nothing.

`search_corpus` was never affected (it returns a list and was deliberately left alone),
which is also the proof that no corpus content was lost — the failure was entirely in
response serialization.

Object tools now return `dict[str, Any]`: still a real output schema, but one that permits
`null` and strips nothing. Field-level validation of response convention 1 is reopened as
corpus-toolkit#15, and `docs/mcp-interface-contract.md` now records why the obvious
implementation is the one that must not be used.

## v1.24.0 — 2026-08-04

**Rebuild corpus images to pick this up.** The serve path changed: `server.py` now serves
the app it verified rather than letting the SDK build a second one. Behaviour is unchanged
for a server started without `--allowed-origin` — same uvicorn options, and
`forwarded_allow_ips` still defaults from the `FORWARDED_ALLOW_IPS` environment variable —
but it is the code path every corpus runs, so it wants a deliberate rollout rather than
riding along on the next unrelated rebuild.

Nothing here is breaking. No config change is required by any corpus.

### Added

- **CORS** via `--allowed-origin` (repeatable), so a browser MCP client can complete the
  handshake at all. Exposes `mcp-session-id`, without which the preflight passes,
  `initialize` returns 200, and the client dies on `Missing session ID` (#37).
- **`corpus-detect-changes --check-robots`** — report each source host's robots.txt
  position, including hosts that permit our agent while blocking named AI crawlers.
  Reports only; nothing blocks a fetch (#29). Found on its first run:
  `www.yamhillcounty.gov` carries `Content-Signal: ai-train=no` and already supplies five
  documents to oregon-collective-bargaining.
- **Output schemas** on the six object-shaped tools, so response convention 1's three
  fields are visible to schema-driven validation instead of only present in the JSON text
  (#15). Extras are still permitted — a schema that constrained each tool's payload would
  break every corpus.
- **`bump_pins.py`** — move every toolkit pin in a corpus, and `--check` to detect pins
  that disagree. Measured across 10 repos: 126 pin sites, six toolkit versions live at
  once, and one repo running two versions inside one `ci.yml` (#9). A `propagate-pin`
  workflow opens the bump PRs, and needs a `CORPUS_PIN_TOKEN` secret to do anything.
- **`corpus-build-semantic-index`** — the semantic arm was the only CLI without a console
  entry point, which also kept it outside the `entrypoints` CI job (#41).
- **`check-links` gains `exclude-urls`** for hosts quoted inside mirrored third-party text,
  where the link belongs to the source document and cannot be corrected (#51).

### Fixed

- `BIG_DOC_BYTES` was defined twice, 1,200 bytes apart, and `framework.py` shadowed its own
  import of it (#52).
- Drift issue creation discarded every `gh` failure. 618 creations failed silently because
  the `source-change` label did not exist — a hard dependency of `--label` that nothing
  created (#53). Now creates the label, reports failures, and caps a run at 25.
- **`pyproject` version was five releases stale** (1.18.0 against tag v1.23.0), so an
  install reported a version it was not. Now gated: a tag whose version disagrees is
  deleted (#41).

### Documentation

CHANGELOG.md now exists; MIGRATION.md covers v1.9.0–v1.23.0; `docs/semantic.md` documents
~730 LOC that had none; `AGENTS.md` states plainly that nothing enforces robots.txt.

## v1.23.0 — 2026-08-03

`_sdk` spans the **client** side of the mcp 1.x/2.x break, not just the server side.

Six client-side breaks between majors — entry-point name, signature, which httpx it wants,
the arity it yields, and two silently renamed model fields. corpus-gateway crash-looped
four times discovering them one at a time. `open_client_streams`, `tool_input_schema` and
`result_is_error` now hide all six (#49).

## v1.22.0 — 2026-08-02

M4 integrity: `corpus-verify-extraction`, `source_data_file` provenance, fetch-failure
tolerance in `corpus-detect-changes`, and `corpus-verify` — the one tool that writes
`last_verified`/`verified_by`, which until then nothing on the platform could set.

## v1.21.0 — 2026-08-02

**Serve the inside of big documents.** `### ` subsections, chunk paging, chunk-aware search
hits. Before this, `get_document` on a 900 KB body was a glance-or-everything binary.

Corpora with anchored large documents need **at least this version** for those anchors to
be addressable at all — below it, `part=` cannot reach a subsection.

## v1.20.1 — 2026-08-02

`viz` slot guard requires a letter, so a bare `______` is treated as content.

## v1.20.0 — 2026-08-02

`corpus_toolkit.viz`: the shared chart chrome (M5-A).

## v1.19.0 — 2026-08-01

**Possibly breaking.** Archetype is enforced; `toolkit-ref` becomes required on the
reusable workflows. Also: status in index rows, multi-scheme matching, and per-corpus
`schema.doc_types` — a new instrument family no longer costs a toolkit release plus an
org-wide pin bump (#40).

## v1.18.0 — 2026-08-01

Declare `numpy` as the semantic extra, and say why the loader failed instead of degrading
silently.

## v1.17.0 — 2026-08-01

`corpus_toolkit.semantic`: semantic search any corpus can enable.

## v1.16.0 — 2026-08-01

`corpus_toolkit.site`: the shared landing-page shell, replacing eight copies of the same
chrome and the same cross-corpus contracts.

## v1.15.0 — 2026-08-01

Scheme-bug guard: a candidate id that is unresolvable by construction is reported as a
scheme bug rather than as a missing document. The two mean opposite things to a curator.

## v1.14.0 — 2026-08-01

`authority_chain` walks the relations a corpus declares, rather than a hardcoded set.

## v1.13.0 — 2026-07-31

The `performance_report` doc_type.

## v1.12.0 — 2026-07-30

The `federal_instrument` doc_type.

## v1.11.0 — 2026-07-30

Serve corpus-specific frontmatter on `get_document` (`extra_document_fields`).

## v1.10.0 — 2026-07-30

The `audit_report` doc_type.

## v1.9.0 — 2026-07-29

Contentless FTS5. The `fts` table no longer stores text, so reading its columns returns
NULL instead of raising — tests that inspected `fts.body` were asserting on an
implementation detail.

## v1.8.0 and earlier

See `MIGRATION.md`, which covers v1.0.3 through v1.8.0 with full upgrade notes.
