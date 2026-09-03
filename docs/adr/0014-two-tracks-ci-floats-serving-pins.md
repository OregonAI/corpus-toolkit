# Two tracks: CI floats on a canary-gated major tag; serving pins exactly and is bot-bumped

**Status:** accepted · **Date:** 2026-09-03

## Context

Every corpus named the toolkit version in one literal, copied into 9–15 places: the
`uses:` tag of each reusable-workflow call, a `toolkit-ref:` input beside it carrying the
same tag, a `.toolkit` checkout `ref:` in the corpus's own `generated:` job, and the git URL
in `requirements.txt`. Every toolkit tag therefore obliged a PR in every repo. Measured on
2026-09-02 across the fifteen repos: **616 PRs merged in seven weeks, 154 of them pin bumps**
— a quarter of all work — against 32 tags in six weeks, 15 of them in August. Five versions
were live at once; `corpus-template`, the skeleton every new corpus instantiates, sat at
v1.19.0 against v1.32.0 and would have gone red on its first scheduled run (a permission
added by hand in six corpora and never in the template).

Two of the three workflow pins were structurally redundant. The reusable workflows already
took `toolkit-ref` and checked the toolkit out at it; GitHub gives a reusable workflow its
own commit as `github.job_workflow_sha`, so the input was a second copy of the `uses:` tag
that existed only because nothing read the first. The corpus's own `.toolkit` checkout was
a third copy of the same five lines.

Of the 27 releases since v1.9.0, 5 required a corpus to act (v1.19, v1.21, v1.23, v1.26,
v1.32). The other 22 opened the same thirteen PRs for nothing.

The operator's stated goal for the platform is one hour a week of upkeep. The per-tag bump
was a mechanism proposed earlier for propagating toolkit changes, not a requirement.

## Decision

A corpus names the toolkit in **two tracks**, and they move differently.

**CI track** — the reusable workflows and the CLIs they run. Corpora call them at the
**floating major tag** `@v1`. `toolkit-ref` is optional and defaults to the workflow's own
commit, so the workflow file and the code it runs cannot drift. Nothing in a corpus's
workflow files carries a version to bump.

**Serving track** — the exact tag in `requirements*.txt` that the Dockerfile installs into
the served image. Exact, so the commit `deployed.txt` records builds the same image twice.
Moved by `propagate-pin`, which now edits only requirements files, opens one PR per repo
and requests auto-merge; where a repo protects `main` with required checks and allows
auto-merge, no human touches the PR.

**The floating tag moves only when every live corpus is green on it.** The release gate
grew a **canary**: on a main push or a release tag it clones every live public corpus at its
default branch and runs the three CI-track CLIs (full frontmatter, full provenance, the
relationship and configuration check) on the candidate. `v1` advances only after the
template leg, the version check and the canary all pass. A release that would break a
corpus stalls there with the corpus named; the fix lands in that corpus first, then the
tag moves. A red canary unpublishes the tag like any other red gate.

**Strict, with a held escape valve.** A corpus that pins an exact tag in `uses:` instead of
`@v1` is **held**: the canary still runs it and reports it, but its failure does not block
`v1`. Held is read from the corpus's own workflow files (`corpus-manifest
--ci-track-state`), never from a manifest field. A corpus's own `generated:` job installs
from `requirements.txt`, so its own gates run against exactly what it serves.

**`v1` indefinitely.** A CI-track change that breaks a corpus is a canary state and is fixed
corpus-first; a version number is not what protects anyone. `v2` is cut only if the corpus
contract's own `contract_version` changes.

**The corpora manifest exists** (`corpus_toolkit/schemas/corpora.yml`, implementing
ADR-0008). The canary and propagate-pin both read it, and propagate-pin's preflight checks
it against the org on every run.

**Bump PRs that sit open are a finding.** propagate-pin's next run lists any bump PR still
open from an earlier release in that repo and goes red rather than opening a second.

## Considered options

**Exact pins everywhere, fewer releases** — rejected. The release cadence is the platform's
to set, but a floor of thirteen PRs per release makes every fix expensive to ship, and the
history above shows the cadence does not stay low under that pressure.

**`@main` instead of `@v1`** — rejected. A tag the gate moves is a release; a branch head is
whatever merged last. Rollback of `v1` is one `git push --force <sha>:refs/tags/v1`.

**Strict SemVer, a major per action-required release** — rejected. Five majors in six weeks
would have recreated the bump churn under a different name (`@v2`, `@v3`, …), and the
canary already turns "breaking" into a workflow state that is handled before the release
exists.

**A differential canary** (block only on a regression against the current `v1`) — rejected
for now. Every corpus that runs full validation on a schedule is green today, so strict
blocks nothing, and a corpus that later goes red for its own reasons should be visible and
held, not averaged away. Revisit if a persistently red corpus ever has to be tolerated.

**Canary over the private consumer tier too** — rejected. The consumer tier couples to the
toolkit through the Python package, which is the serving track; its proof is its own CI on
the bump PR. That requires the consumer repos to HAVE CI, which corpus-chat did not.

## Consequences

The number of `uses:` lines in a corpus stops mattering for upkeep. The remaining reason to
unify corpus CI into one reusable `corpus-ci.yml` is that template gates silently drop out
of forks — a different problem, judged on its own.

The reusable workflows' YAML is not exercised before `v1` moves; the first corpus run after
the move exercises it. Accepted with the one-line rollback documented in
`release-gate.yml`. A pre-tag dispatch of the template's workflows would close the gap at
the cost of a second mechanism.

`propagate-pin`'s token no longer needs `workflows: write`.

Branch protection becomes the real gate on the corpus tier: required status checks, zero
required reviews, `allow_auto_merge` on. With one human operator and admin enforcement off,
the review requirement was not gating anything; the checks were. The two private consumer
repos cannot carry protection on the current plan and keep human merge for their rare bumps.

Before the sweep that moved the corpora to `@v1`, every corpus read as held, so the first
release under this gate was trivially green. That is the intended opt-in shape.

What this does not settle: a corpus that has never merged a bump PR still serves whatever
its `requirements.txt` last said. Deploy remains a separate, manual step (ADR-0007).
