# Architecture Decision Records

Cross-repo decisions for the OregonAI civic corpus platform. They live in `corpus-toolkit`
because it is the one repo every other repo already depends on — the same reasoning as
[ADR-0008](0008-manifest-lives-in-the-toolkit.md). A decision scoped to a single repo belongs
in that repo's own `docs/adr/`.

| ADR | Decision |
|---|---|
| [0001](0001-one-corpus-one-repo.md) | One corpus is one repo |
| [0002](0002-the-consumer-tier-is-first-class.md) | The consumer tier is a peer of the corpus tier |
| [0003](0003-triage-labels-are-the-backlog-mechanism.md) | The existing triage labels are the backlog mechanism |
| [0004](0004-two-kinds-of-corpus-list.md) | Two kinds of corpus list; unify only the tooling kind |
| [0005](0005-version-the-gateway-contract.md) | The gateway's surface is a versioned contract |
| [0006](0006-one-package-public-client-seam.md) | One package; the client seam becomes public |
| [0007](0007-independent-deploys-centralised-probing.md) | Independent deploys, centralised probing |
| [0008](0008-manifest-lives-in-the-toolkit.md) | The corpora manifest ships with the toolkit |
| [0009](0009-the-crosswalk-is-reference-data.md) | The agency crosswalk is reference data, not a corpus |
| [0010](0010-a-group-drift-finding-reports-correlation-not-cause.md) | A group drift finding reports correlation, not cause |
| [0011](0011-a-corpus-that-attributes-nothing-says-so.md) | A corpus that attributes nothing says so, rather than answering zero |
| [0012](0012-completing-a-chain-is-not-weakening-verification.md) | Completing a chain is not weakening verification |
| [0013](0013-a-persistent-access-failure-escalates-on-runs-or-days.md) | A persistent access failure escalates on runs or elapsed days, whichever comes first |
| [0014](0014-two-tracks-ci-floats-serving-pins.md) | Two tracks: CI floats on a canary-gated major tag; serving pins exactly and is bot-bumped |

0014 was decided on 2026-09-03, grilling the first candidate of the 2026-09-02 whole-platform architecture review (154 of 616 merged PRs were pin bumps). 0013 was decided on 2026-09-02, grilling corpus-toolkit#166. 0012 was decided on 2026-08-27, grilling executive-regulatory-frameworks#264 and #140. 0011 was decided on 2026-08-26, grilling corpus-toolkit#158. 0010 was decided on 2026-08-22, grilling corpus-toolkit#132. The first nine were decided together on 2026-08-12, in a review of the platform's cross-repo shape
following `PLATFORM-REVIEW-2026-08-01.md`. That review's own three named weaknesses had all
been closed by then; these record the shape questions that replaced them — chiefly that the
platform grew a consumer tier while all of its governance was built for the first tier.
