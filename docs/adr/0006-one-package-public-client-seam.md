# ADR-0006 — One package; the client seam becomes public

**Status:** accepted, implemented 2026-09-04 (v1.35.0) · **Date:** 2026-08-12

## Context

`corpus-toolkit` serves two audiences under one tag. Corpus servers pin it as a framework;
`corpus-gateway` and `corpus-chat` pin it as a **client library** — and import nothing from it
except `corpus_toolkit.mcp._sdk`, pulling in the whole package plus the `mcp` extra for a
~416-line compat shim.

That module has a curated `__all__`, so it has a deliberate public surface, behind a
leading-underscore name that says the opposite. Both consumers pin `v1.23.0` because it is the
first release whose seam spans the *client* side of the mcp 1.x/2.x break, and both record what
that break cost before the seam existed: four renamed APIs, *"two of them silently."*

## Decision

Keep one package. Promote the module to `corpus_toolkit.mcp.sdk`, keep `_sdk` as an alias, and
document it as the supported client seam under the same both-majors compat promise it already
delivers in practice.

## Consequences

The rejected alternative was a separate `corpus-toolkit-client` distribution. It doubles the
release surface of the one component whose entire value is being the *single* place that knows
which SDK major is installed — the opposite of what a split buys. The fat consumer image is
real and is the lesser problem.

The problem actually being fixed is not size: two independently deployed production services
depend on a name that Python convention says may vanish in any release, and the platform's own
release gate would not consider that a breaking change.
