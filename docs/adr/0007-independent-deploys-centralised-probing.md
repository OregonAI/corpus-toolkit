# ADR-0007 — Independent deploys, centralised probing

**Status:** accepted · **Date:** 2026-08-12

## Context

`corpus-gateway` and `corpus-chat` each carry their own `Dockerfile`, `docker-compose.yml` and
`cloudflared/`, separate from `platform-deploy`. The gateway joins `platform-deploy`'s network
with `external: true`, and its own comment notes that repo *"declares no `networks:` block, so
compose puts its services on an implicit"* one — a name derived from the project directory.

The 2026-08-01 review's Milestone 1 exit test was: *"kill any container or wedge the tunnel →
something turns red within minutes."* That was satisfied for the corpus tier, before this tier
existed. It is false for both consumer services (platform-deploy#23), including the one every
conversation now flows through.

## Decision

Keep the deploys independent. Centralise only the probing.

- One synthetic probe, owned by `platform-deploy`, POSTs every corpus MCP endpoint **and** the
  gateway's **and** chat's health route.
- `check_compose.py` gains one assertion: the external network exists under the name the gateway
  expects.
- `platform-deploy` declares that network **explicitly**, under either answer, because the
  implicit directory-derived name is the fragile part.

## Consequences

The rejected alternative was folding both services into `platform-deploy`'s compose — one
network, one `deployed.txt`, one checker. It is tidier and it gives up the property the gateway
was deliberately built for: chat and the gateway can ship without each other.

Observability is the one thing that must **not** be per-tier, because the failure it exists to
catch is precisely "this tier is down and its own monitor is down with it". That asymmetry —
decentralised deploys, centralised watching — is the whole decision.

This also makes the seam between two independently deployed repos an asserted thing rather than
an assumed one, which is the same reasoning as `check_compose.py` existing at all.
