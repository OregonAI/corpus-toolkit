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

**Corpora manifest** — `corpus_toolkit/schemas/corpora.yml`, the one machine-readable source the
tooling lists generate from and the runtime lists are checked against. Read with
`corpus-manifest`; checked against the org by propagate-pin's preflight on every run.

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

**Access failure** — a source whose fetch has failed for `access-failures.json`'s two
persisted counts, `consecutive_failures` and `first_failed_at`, to reach the operator's
threshold: 2 consecutive failed runs, or 14 elapsed days since the first of those
failures, whichever comes first (corpus-toolkit#166). It is marked **escalated** in `DRIFT.md`
(ADR 0015; until v1.34.0 it filed one `Access failure:` issue) — a section of its own, never
mixed into the changed-since-baseline list, because it is a fact about OUR access to a
source and not about what changed. `fetch_failed` (a `Source outcome`, above) is the per-run observation; an
access failure is what a streak of that observation becomes once it crosses the threshold
and stops being noise. The state artifact holds a source only while its MOST RECENT
observation was `fetch_failed`; any other outcome clears it, and a source or a whole group
retired from the manifest is pruned from it rather than kept asserting a dead source is
still failing.
_Avoid_: "drift", "changed" for this — an access failure asserts nothing about upstream,
only that the tool's own fetches have not been arriving; conflating the two is the exact
confusion the separate label exists to prevent.

**Drift report** — `DRIFT.md` at a corpus root, rendered every drift run from
`drift-state.json` (ADR 0015): every source whose upstream differs from the baseline the
manifest records and since when, the groups whose every compared source changed, the
access-failure streaks, what the run seeded. Rolling by construction — a changed source
stays listed until re-ingested or its baseline accepted with `--record-baseline=refresh`.
Generated but NOT `--check`-gated: it records observations no local command can reproduce.
_Avoid_: "drift ticket", "Source changed: issue" — a drift run files nothing since v1.34.0,
and the 89 tickets it used to file were closed on 2026-09-03 with a pointer to this file.

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


## Tracks

A corpus names the toolkit in two places that move differently — see [ADR-0014](docs/adr/0014-two-tracks-ci-floats-serving-pins.md).

**CI track** — the reusable workflows a corpus calls (`uses: OregonAI/corpus-toolkit/.github/workflows/<name>.yml@v1`)
and the CLIs they run. Floats on the major tag `v1`, which only the release gate moves.
`toolkit-ref` is optional and defaults to the workflow's own commit. Nothing here carries a
version to bump.

**Serving track** — the exact tag in `requirements*.txt` that a corpus's Dockerfile installs
into the image it serves. Moved by `propagate-pin`, one requirements-only PR per release,
auto-merged on green where the repo allows it. Exact so that `deployed.txt`'s commit builds
the same image twice.

**Canary** — the release-gate job that clones every live public corpus and runs the CI-track
CLIs on the candidate before `v1` advances. A red canary unpublishes the tag.

**Held** — a corpus whose workflows pin an exact tag instead of `@v1`. It has opted out of a
release it cannot yet take; the canary reports it and does not block on it. Read from the
corpus's own workflow files, never from a manifest field.

_Avoid_: "the pin", "bump the toolkit" without naming the track — the two have moved on
different mechanisms since v1.33.0, and "bump" on the CI track means editing a `uses:` line
that is no longer supposed to carry a version.

## Seams and adapters

Used as in `docs/agents/` and throughout `corpus_toolkit/mcp/`: a **seam** is where a module's
interface lives and where behaviour can be swapped without editing in place; an **adapter**
satisfies one. The retrieval seam (`RetrievalBackend`) and the semantic seam
(`semantic_search_module`) are the two a corpus can supply its own adapter for.

_Avoid_: "boundary" — overloaded with DDD's bounded context.

## Attribution and answerability

**Attribution** — a corpus tying one of its documents to an entry in an issuing-body registry.
Some corpora do it from a declared field (`plugins.issuing_body_slug_field`), some derive it
from the path, and three attribute **nothing**: `oregon-budget`, `oregon-legislature` and
`federal-reference` carry no issuing body on any document and never will without new work.

**Answerability** — whether a corpus can answer *at all* for a given agency, which is a
different question from whether it holds anything. Four outcomes, and the middle two are the
ones that get collapsed:

- **documents** — it attributes, and it holds some for this agency.
- **none** — it attributes, and it holds none for this agency. A finding.
- **cannot answer** — it attributes nothing, so zero is not a count, it is the absence of a
  measurement. `attribution.can_answer: false`, and `total` is withheld rather than zeroed.
- **unknown** — the coverage counts themselves are missing, so which of the above holds
  cannot be read.

The discriminator is **the corpus's own counts, not its prose**: `documents_with_no_issuing_body
>= documents_in_corpus` means it cannot answer, whatever the reason — undeclared slug field
today, an unbuilt crosswalk tomorrow. A reader that branches on the `basis` string calls a
genuine *none* a *cannot answer* and deletes a finding; one that reads `total` alone calls a
*cannot answer* a *none* and invents an absence (ADR 0011).

AN EMPTY CORPUS IS *unknown*, NOT *cannot answer*. `documents_with_no_issuing_body >=
documents_in_corpus` is satisfied by `0 >= 0`, and reading that as "attributes nothing" states
a measurement nobody took — the same collapse one field over, and the reading `complete`
already refuses for an identical index (corpus-toolkit#158).
_Avoid_: "empty", "no results", "has none" — each describes an ANSWER, and the case this
vocabulary exists for is the one where there was never a question this corpus could be asked

## The rule that outranks the vocabulary

**"Could not check" is never reported as "is not there."** It appears as response convention 5,
as `sibling_unavailable`, as `no_graph` vs `not_in_graph`, as `status: ""` meaning unknown and
never `current`, as `attribution.can_answer` refusing to let "attributes nothing" wear the
shape of "holds none", as the reason a healthcheck that cannot fail is treated as worse
than no healthcheck, and as a source outcome refusing to let `fetch_failed` wear the shape of
`unchanged` — the two states `changed-sources.tsv` could not tell apart, because a source
absent from it means either one (corpus-toolkit#160). It is also why an access-failure
escalation says only that OUR fetches failed and never that the document changed or was
removed — this tool cannot tell a block from a page that moved and does not guess
(corpus-toolkit#166). If a new mechanism collapses those two answers, it is wrong
regardless of what it is called.
