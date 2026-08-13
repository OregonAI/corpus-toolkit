# ADR-0008 — The corpora manifest ships with the toolkit

**Status:** accepted · **Date:** 2026-08-12

## Context

[ADR-0004](0004-two-kinds-of-corpus-list.md) calls for one machine-readable manifest that the
tooling lists generate from and the runtime lists are checked against. Four homes were
considered: the org `.github` repo, `platform-deploy`, `corpus-toolkit`, or a new repo.

## Decision

`corpus-toolkit`, as a spec artifact beside `corpus_toolkit/schemas/`.

## Consequences

It is the only repo every other repo already depends on, it already ships machine-readable
specs *inside the package* precisely so that installing it is enough to use them, and it has
the strongest release gate in the org — one that instantiates a real corpus and unpublishes a
bad tag.

`AGENTS.md` scopes this repo to "tooling and specs only — never civic content". A list of which
corpora exist is a spec, not civic content, so the manifest is admissible where the agency
crosswalk is not — see [ADR-0009](0009-the-crosswalk-is-reference-data.md).

Rejected:

- **`platform-deploy`** — tempting, since five of the seven tooling sites live there. It is a
  *deployment* repo, and putting the canonical list there would make `propagate-pin.yml` in this
  repo reach into it, inverting a dependency that currently flows one way.
- **A new repo** — a fifth thing to keep current for an artifact measured in tens of lines.
- **`.github`** — org-profile furniture, and the 2026-08-01 review records a registry table
  being deliberately deleted from exactly there.

The consuming repos read it as a published artifact over HTTP, the same shape as
`corpus-index.json`, so a check never depends on a checkout.
