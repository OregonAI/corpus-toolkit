# CONTEXT — the civic corpus platform

The vocabulary the OregonAI platform uses about itself. It lives in `corpus-toolkit` because
this is the one repo every other repo already depends on; see [ADR-0008](docs/adr/0008-manifest-lives-in-the-toolkit.md)
for why that reasoning also decides where the corpora manifest goes.

Terms are here because they are **load-bearing** — each one names a distinction that has been
got wrong at least once, or that decides where a file belongs. Words nobody has misused do not
need a glossary entry.

## Tiers

**Corpus tier** — the repos that publish civic content. Eight live: `executive-regulatory-frameworks`,
`oregon-legislature`, `oregon-budget`, `oregon-audits`, `oregon-kpm`, `oregon-counties`,
`oregon-collective-bargaining`, `federal-reference`. Each instantiates one skeleton from
`corpus-template`, pins a `corpus-toolkit` tag, and serves the seven-tool contract in
`docs/mcp-interface-contract.md`. One corpus is one repo — see [ADR-0001](docs/adr/0001-one-corpus-one-repo.md).

**Consumer tier** — the repos that *read* the platform rather than publish to it:
`corpus-gateway` (one MCP endpoint fronting all eight corpora) and `corpus-chat` (the web
client, which reaches the corpora only through the gateway). A peer of the corpus tier, not an
afterthought — see [ADR-0002](docs/adr/0002-the-consumer-tier-is-first-class.md). `oregon-stories`
also consumes, but statically at build time rather than over MCP.

**Platform tier** — `corpus-toolkit` (framework, specs, client seam), `corpus-template`,
`platform-deploy`, `OregonAI.github.io`.

_Avoid_: "downstream", which has been used for both a consuming app and a corpus that pins a
toolkit tag. Say which tier.

## Kinds of repository content

**Corpus** — a repo that mirrors an authoritative external source and proves it did so
faithfully: snapshot hashes, verbatim line-order checks against the source, a coverage floor,
`last_verified`. The whole discipline is provenance *against an upstream*.

**Reference data** — a curated artifact with no upstream to mirror, because it is **our own
editorial assertion** rather than a copy of anyone's text. The agency crosswalk is the
canonical instance: asserting that `ADMINISTRATIVE SRVCS, DEPT OF` and
`Administrative Services, Department of` denote one body is a judgement we make, not a fact we
transcribe. Reference data has a schema and CI but no MCP server, and does not appear in
`list_corpora` — see [ADR-0009](docs/adr/0009-the-crosswalk-is-reference-data.md).

The distinction matters because running reference data through corpus machinery would either
leave that machinery unused or manufacture a provenance claim nothing supports.

## Lists of corpora

Nine files across four repos name "which corpora exist". They are **two kinds**, and only one
kind should be unified — see [ADR-0004](docs/adr/0004-two-kinds-of-corpus-list.md).

**Runtime list** — a list a deployed service reads to do its job: `corpus-gateway/src/registry.py`,
`corpus-chat/src/corpora.py`. Duplicated **deliberately**, so two independently deployed
services can ship without each other. Checked against the manifest in CI; never imported from it.

**Tooling list** — a list that exists to drive automation: the `propagate-pin.yml` matrix,
`platform-deploy`'s compose, `deploy.sh`, tunnel config, `deployed.txt`, READMEs. Generated
from the manifest, because duplication here buys nothing and has already produced a
propagation matrix targeting a repository that does not exist (corpus-toolkit#83).

**Corpora manifest** — the one machine-readable source the tooling lists generate from and the
runtime lists are checked against.

**Group drift finding** — one drift issue naming a source group in which EVERY compared source
changed in a single run. It states that they changed together and asserts nothing about why:
the tool observes bytes, not causes, and one whole-group event on record was an inert run with
empty baselines rather than a change at all (ADR 0010). It accompanies the individual source
tickets and never replaces them.
_Avoid_: bulk finding, group issue — both read as a diagnosis of the group

**Source outcome** — the classification `source-outcomes.json` records for every source a
drift run had in scope: `changed`, `unchanged`, `no_baseline`, `fetch_failed`,
`unreadable_json`, or `watch_path_missing`. Introduced because `changed-sources.tsv`
reports only `changed` — a source that was compared and held still, one whose fetch
failed, and one out of this run's scope were all equally absent from any artifact, so a
run in which every fetch failed and a run with no drift wrote the same empty file
(corpus-toolkit#160). `no_baseline` is its own outcome rather than `changed` or
`unchanged` for the same reason ADR 0010 excludes an unseeded source from a group drift
finding: there was no recorded hash to compare the fetched bytes against, so neither word
would be true of it.
_Avoid_: "not compared" as a synonym for any single one of the non-`changed` outcomes —
four of the six describe a source nothing compared, for four different reasons with four
different remedies, and the whole point of naming them is that a consumer no longer has to
guess which one applied.

**Persistent access failure** — a source whose `fetch_failed` outcome has recurred on
`ACCESS_FAILURE_STREAK_THRESHOLD` or more CONSECUTIVE scheduled runs, tracked in
`access-failures.json` across separate invocations rather than within one (corpus-toolkit#166).
Named apart from a `Source changed:` ticket because it is a different claim: a source that
fails to fetch was never compared to anything, so it cannot be drift, and the ticket says only
that OUR ACCESS to the URL has failed repeatedly — never that upstream removed, moved, or
changed anything, which nothing this tool observes can tell. The gap it closes sits between
`--strict` (fails a run on any single failure) and the 20% systemic guard (fails a run whose
failures are widespread): a source failing every run forever, at low volume, previously
produced identical output — `FETCH FAILED` on stdout — on its first failure and its thirtieth.
_Avoid_: describing it as drift, or folding it into `Source changed:` — the whole point is that
these are sources nothing has been compared for, the same reason `no_baseline` is its own
source outcome rather than `changed`.

## Contracts

**Corpus contract** — `docs/mcp-interface-contract.md`. Seven tools, the response conventions,
`contract_version` on every response, "servers must not remove core tools".

**Gateway contract** — the surface `corpus-gateway` exposes, which is *not* the corpus
contract: `search` rather than `search_corpus`, every tool corpus-scoped, plus `list_corpora`
and a `call(corpus, tool, arguments)` passthrough. Versioned in the gateway's own repo — see
[ADR-0005](docs/adr/0005-version-the-gateway-contract.md).

Two contracts, deliberately. The gateway does not re-implement the corpus contract; it reads
each server's tool inventory live from `tools/list`, so it cannot drift from what a corpus
actually registers.

**Consumed surface** — everything a corpus repo reaches into the toolkit for that is *not* an
MCP tool: `CorpusFramework.ensure_index` in a Dockerfile's build step, `corpus_toolkit.repo`
helpers in a corpus's own scripts, the `corpus-*` console scripts, the argv in a corpus
Dockerfile's `CMD`, and **the extras named in a corpus's `requirements.txt`** —
`corpus-toolkit[mcp,semantic]` means `semantic` is a name a corpus depends on. Renaming it
makes pip emit a warning, the image build succeed, and every corpus lose numpy while
reporting healthy. It is a contract as real as
the corpus contract and, until corpus-toolkit#100, nothing in this repo executed any of it.

**Anything reachable from a corpus repo is public, whether or not this repo calls it.**
corpus-toolkit#75 deleted `ensure_index` after searching `corpus_toolkit/` and `tests/` and
finding no caller. `corpus-template`'s Dockerfile called it, every corpus image build failed
for two releases, and the reviewer, the gate and the author all read "no caller here" as "no
caller". Absence of a local caller is not evidence; the corpus repos are where the callers
live.

_Avoid_: "internal" for anything importable. If a corpus can reach it, it is consumed surface
no matter what it is named.

**Client seam** — `corpus_toolkit.mcp.sdk` (historically `_sdk`), the one place that knows
whether `mcp` 1.x or 2.x is installed. Used on both sides: by corpus servers, and by the
consumer tier — see [ADR-0006](docs/adr/0006-one-package-public-client-seam.md).

**Answerability** — whether a corpus can be asked a question at all, as distinct from what the
answer is. Carried as `attribution.can_answer`: `true` the corpus attributes at least one
document to an issuing body; `false` it attributes none, so a per-agency count of 0 is a fact
about the corpus and not about the agency; `"unknown"` the counts needed to decide were never
measured. Three states that may never fold into two — see
[ADR-0011](docs/adr/0011-a-corpus-that-attributes-nothing-says-so.md). It is read from the
corpus's own counts, never from `basis`, which two corpora share while one can answer and the
other cannot.
_Avoid_: "empty", "no results", "has none" — each describes an ANSWER, and the whole point of
the field is the case where there was never a question this corpus could be asked

## Seams and adapters

Used as in `docs/agents/` and throughout `corpus_toolkit/mcp/`: a **seam** is where a module's
interface lives and where behaviour can be swapped without editing in place; an **adapter**
satisfies one. The retrieval seam (`RetrievalBackend`) and the semantic seam
(`semantic_search_module`) are the two a corpus can supply its own adapter for.

_Avoid_: "boundary" — overloaded with DDD's bounded context.

## The rule that outranks the vocabulary

**"Could not check" is never reported as "is not there."** It appears as response convention 5,
as `sibling_unavailable`, as `no_graph` vs `not_in_graph`, as `status: ""` meaning unknown and
never `current`, as `attribution.can_answer` refusing to let "attributes nothing" wear the
shape of "holds none", as the reason a healthcheck that cannot fail is treated as worse
than no healthcheck, and as a source outcome refusing to let `fetch_failed` wear the shape of
`unchanged` — the two states `changed-sources.tsv` could not tell apart, because a source
absent from it means either one (corpus-toolkit#160). It appears again as
`_update_access_failure_streaks` refusing to reset a source's recorded streak just because a
`--group` run never looked at it this time (corpus-toolkit#166) — an out-of-scope source is
"could not check", not "is failing no longer". If a new mechanism collapses those two
answers, it is wrong regardless of what it is called.
