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

**Client seam** — `corpus_toolkit.mcp.sdk` (historically `_sdk`), the one place that knows
whether `mcp` 1.x or 2.x is installed. Used on both sides: by corpus servers, and by the
consumer tier — see [ADR-0006](docs/adr/0006-one-package-public-client-seam.md).

## Seams and adapters

Used as in `docs/agents/` and throughout `corpus_toolkit/mcp/`: a **seam** is where a module's
interface lives and where behaviour can be swapped without editing in place; an **adapter**
satisfies one. The retrieval seam (`RetrievalBackend`) and the semantic seam
(`semantic_search_module`) are the two a corpus can supply its own adapter for.

_Avoid_: "boundary" — overloaded with DDD's bounded context.

## The rule that outranks the vocabulary

**"Could not check" is never reported as "is not there."** It appears as response convention 5,
as `sibling_unavailable`, as `no_graph` vs `not_in_graph`, as `status: ""` meaning unknown and
never `current`, and as the reason a healthcheck that cannot fail is treated as worse than no
healthcheck. If a new mechanism collapses those two answers, it is wrong regardless of what it
is called.
