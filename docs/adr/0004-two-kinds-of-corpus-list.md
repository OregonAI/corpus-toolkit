# ADR-0004 — Two kinds of corpus list; unify only the tooling kind

**Status:** accepted · **Date:** 2026-08-12

## Context

Nine files across four repos name which corpora exist: `propagate-pin.yml`,
`corpus-chat/src/corpora.py`, `corpus-gateway/src/{registry.py,fanout.py}`, and
`platform-deploy/{deployed.txt,README.md,docker-compose.yml,scripts/deploy.sh,cloudflared/oregonai-mcp.yml}`.

At least one is wrong in both directions: the propagation matrix targets `oregon-policy-repo`,
which does not exist, and `oregon-records-retention`, which is archived, while omitting ERF and
the entire consumer tier (corpus-toolkit#83).

But unification is not obviously right, and the platform has twice decided against it.
`corpus-gateway/src/registry.py` argues in writing that duplicating the list into `corpus-chat`
is deliberate: *"a shared package for an eight-line list would couple two independently deployed
services so that neither could be updated without the other."* And the 2026-08-01 review records
that an org-profile registry table was deliberately deleted.

Measured evidence for that argument: nine days after both were written, the two runtime
registries **agreed** — eight entries each, the same deliberate exclusion of the retired corpus,
the same recorded reason. The duplication had not drifted.

## Decision

Split the nine sites by kind.

- **Runtime lists** (gateway `registry.py`, chat `corpora.py`) stay duplicated. They are
  **checked** against the manifest in CI and never import from it.
- **Tooling lists** (the other seven) are **generated** from one machine-readable corpora
  manifest.

## Consequences

Drift detection without runtime coupling — the property both prior decisions were protecting.
A check that fails in CI cannot take a deployed service down with it, which importing could.

The manifest's home is [ADR-0008](0008-manifest-lives-in-the-toolkit.md).

Note what this does *not* fix: `propagate-pin.yml` has never opened a PR, because
`CORPUS_PIN_TOKEN` was never provisioned — every run is report-only and reports success
(corpus-toolkit#83). Generating its matrix from a correct manifest makes a correct list of
targets for a job that still does nothing. Provisioning the token, or deleting the workflow and
reopening corpus-toolkit#9, is the prior fix.
