# ADR-0007 — Independent deploys, centralised probing

**Status:** accepted, amended 2026-08-18 · **Date:** 2026-08-12

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

- One synthetic probe POSTs every corpus MCP endpoint **and** the gateway's **and** chat's
  health route. **Amended:** it is owned by `OregonAI.github.io`, not `platform-deploy` — see
  the amendment below.
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


## Amendment, 2026-08-18 — the probe does not live in `platform-deploy`

This ADR said the synthetic probe should be *owned by `platform-deploy`*. That was written
without knowing one already existed, and it is wrong on a constraint I did not have.

`OregonAI.github.io/monitor.py` has been running a 15-minute cron since Milestone 1. It POSTs a
real JSON-RPC `initialize` through the **public** URL and asserts the corpus that answers is the
corpus the path names — nine routes, including the retired corpus's tombstone, **and the
gateway**. Its own header records why it lives there:

> Lives in this repo because it is public (free Actions minutes) and because the site is already
> the platform's outward-facing surface; platform-deploy is private.

A 15-minute cron is roughly 2,880 runs a month: free on a public repository, billed against the
account quota on a private one. That reasoning is better than this ADR's, so **the ADR moves, not
the monitor**.

What survives unchanged is the decision this ADR was actually about, and it is the half that
mattered: **deploys stay independent, watching stays centralised.** One probe watches every tier
from outside, because the failure worth catching is "this tier is down and its own monitor is
down with it". Which repository hosts that one probe is an operational detail — free CI minutes
and a public vantage point decide it — not an architectural one.

Two things followed from getting this wrong, both recorded so the error is not re-derived:

- **platform-deploy#23** claimed nothing watches the gateway. It does. Corrected on the issue.
- **corpus-chat#5** is the gap that is real: `/healthz` returns 302 to the Access login through
  the tunnel (measured), so that app cannot be probed from outside without either a service token
  or a Bypass policy. That is an operator decision with a security tradeoff, filed in the repo
  that owns the surface.

The `check_compose.py` half of this ADR was unaffected and shipped as platform-deploy#24.

**The lesson worth keeping**, since this repo files defects rather than absorbing them: this ADR
asserted a fact about another repository without checking that repository. The review it came
from said plainly that Milestones 4 and 5 had not been re-verified — and this decision reached
into exactly that unverified ground without noticing it had.
