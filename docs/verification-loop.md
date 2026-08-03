# The verification loop — what `last_verified` means, and how it gets written

`last_verified` / `verified_by` record a **human act**, never a machine event.
Every corpus's AGENTS.md rule 6 says so; the M4 correction (2026-08-03) un-stamped
83,728 documents where ingestion dates had been written into the field, because a
fabricated verification stamp is worse than an obviously-empty one.

**The one writer is `corpus-verify`** (this toolkit's CLI). It refuses to stamp
without `--by` (whose handle) and `--attest` (one line: what act was performed),
prints each document's checklist first, and edits the two frontmatter lines
in place. The stamping commit's PR is the review record.

## What counts as a verification act, per content_mode

| content_mode | the act |
|---|---|
| `verbatim` | read the rendered `## Full text` against the live official source; confirm no drift the snapshot hash cannot see (the hash pins OUR rendering, not upstream) |
| `summary`  | open the official link; confirm it is the claimed instrument, that metadata (term, dates, version) matches, and that the summary misstates nothing |
| `mixed`    | both, per section |
| dataset docs | re-fetch or re-derive the artifact; confirm the recorded hash still matches and stated figures reproduce |

## Cadence

`status.reverify_days` in each corpus's config (90/180/365 today) drives the
STATUS.md freshness table — documents past their window surface there. The loop:
STATUS names the overdue → the reviewer verifies with `corpus-verify` → the PR
lands the stamps → STATUS regenerates clean.

## The pilot

federal-reference (43 documents, every content_mode represented, the corpus most
likely to be mistaken for legal advice) is the M4 pilot: a fully-verified corpus
of manageable size that proves the loop end to end.
