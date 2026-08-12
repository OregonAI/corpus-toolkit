# ADR-0005 — The gateway's surface is a versioned contract

**Status:** accepted · **Date:** 2026-08-12

## Context

`corpus-gateway` exposes a surface that is **not** the corpus contract: `search` rather than
`search_corpus`, every tool taking `corpus` as a parameter, plus `list_corpora` and a
`call(corpus, tool, arguments)` passthrough. `corpus-chat` depends on it exclusively, and
browser clients can reach it now that CORS has landed. Nothing documented it the way
`docs/mcp-interface-contract.md` documents the corpus surface — no response conventions, no
version integer, no "must not remove" rule.

Worth stating plainly, because it was the initial worry and it is unfounded: the gateway does
**not** re-implement the corpus contract. It reads each server's inventory live from
`tools/list`, with the reason recorded — the toolkit registers tools conditionally, so a
hardcoded capability map "drifts silently on the next corpus deploy". The concern in
`corpus_toolkit/mcp/backends.py` about two implementations of one surface does not apply.

## Decision

Version the gateway's surface as a contract, documented in `corpus-gateway`'s own repo — not in
`corpus-toolkit`, whose `AGENTS.md` scopes it to the shared server platform and its specs.

The `call()` passthrough needs an explicit stated policy: it is an unversioned escape hatch onto
whatever a corpus happens to register, and saying so is the difference between a documented
capability and an accidental one.

## Consequences

Two contracts exist and are named as such in `CONTEXT.md`. The platform's premise is that one
client config works against every server; a second undocumented surface *in front of* every
server is where that discipline would erode first, and it now has somewhere to be written down.
