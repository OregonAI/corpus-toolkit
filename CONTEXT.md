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

## The rule that outranks the vocabulary

**"Could not check" is never reported as "is not there."** It appears as response convention 5,
as `sibling_unavailable`, as `no_graph` vs `not_in_graph`, as `status: ""` meaning unknown and
never `current`, and as the reason a healthcheck that cannot fail is treated as worse than no
healthcheck. If a new mechanism collapses those two answers, it is wrong regardless of what it
is called.
