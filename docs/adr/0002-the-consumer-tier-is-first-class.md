# ADR-0002 — The consumer tier is a peer of the corpus tier

**Status:** accepted · **Date:** 2026-08-12

## Context

`corpus-gateway` and `corpus-chat` were created 2026-08-03, after the platform review that
still frames most planning. They consume the platform rather than publish to it, and at the
time of this decision they sat outside every piece of shared machinery: outside
`corpus-template`, the reusable workflows, `propagate-pin.yml`, and `platform-deploy`'s
compose. Neither had any CI at all (corpus-gateway#2, corpus-chat#2), and
`platform-deploy`'s scripts contained no reference to either (platform-deploy#23).

Meanwhile `corpus-chat` had moved *all* of its traffic through the gateway — replacing one MCP
session per corpus and 56 prefixed tools with eight corpus-parameterised ones. So the gateway
became the single path for every conversation to every corpus, and was simultaneously the least
governed component on the host.

## Decision

Name the consumer tier and treat it as a peer of the corpus tier: it gets CI, probe coverage,
pin propagation, and a versioned contract. It is not a pair of apps that happen to exist.

## Consequences

Four decisions hang off this one and would be incoherent without it:
[ADR-0004](0004-two-kinds-of-corpus-list.md) (its runtime lists are legitimate, not drift),
[ADR-0005](0005-version-the-gateway-contract.md) (its surface is a contract),
[ADR-0006](0006-one-package-public-client-seam.md) (it may depend on a public name), and
[ADR-0007](0007-independent-deploys-centralised-probing.md) (it gets watched).

The rejected alternative was to treat both as one-offs borrowing a compat shim. That is
internally coherent and was seriously considered; it fails on the observation that one of them
is now a single point of failure in front of the entire platform for every human user.
