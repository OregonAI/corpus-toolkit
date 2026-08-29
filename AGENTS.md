# AGENTS.md — corpus-toolkit

This repo is the shared platform for the OregonAI civic corpus system. It
contains tooling and specs only — never civic content.

## Rules
- Read `docs/reference-architecture.md` before changing anything structural.
- **Anything reachable from a corpus repo is public surface**, whether or not
  this repo calls it — Dockerfile build steps, corpus scripts, console scripts.
  Grepping `corpus_toolkit/` and `tests/` does not answer "is this used"; the
  callers live in the corpus repos (corpus-toolkit#75, #100). Removing or
  renaming one is a breaking change. The release gate runs the template's
  Dockerfile `RUN` commands, so it catches that surface — it does NOT yet
  cover the `CMD` argv or the extras named in a corpus's `requirements.txt`
  (corpus-toolkit#116). Treat a green gate as covering the build step and
  nothing further.
- Schema or MCP-contract changes require updating the matching doc in the
  same PR; breaking changes bump the major version.
- Reusable workflows must stay corpus-agnostic: all corpus specifics come
  from the calling repo's `_meta/corpus.yml` and manifest.
- Conventional commits. All changes via PR.
- Never weaken a guardrail (validator, diff check, review gate) to make a
  corpus ingest easier; fix the corpus instead.

## Agent skills

### Issue tracker

GitHub Issues on `OregonAI/corpus-toolkit`, via the `gh` CLI. See
`docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` and `docs/adr/` at the repo root. See
`docs/agents/domain.md`.

## Fetching: the toolkit does not enforce robots.txt

Say this plainly because the opposite is the natural assumption. `corpus-detect-changes`
re-fetches every source in the manifest on every scheduled run, and **nothing blocks a
fetch on robots.txt**. There is a checker, and it reports:

```bash
corpus-detect-changes --config _meta/corpus.yml --check-robots   # reports, exits 0
```

Enforcement is a per-corpus policy decision. It is deliberately not a toolkit default,
because arriving as a surprise behaviour change in a version bump is how a corpus silently
stops ingesting — the same reasoning as `corpus-detect-unsourced` reporting rather than
gating.

Two questions the checker keeps apart, and you need both:

* **Does robots.txt permit our user agent?** The literal compliance question, and the only
  one with a mechanical answer.
* **Does the host state a position on AI/agent crawling at all?** A host that
  `Disallow: /`s ClaudeBot and GPTBot has said something about this category of use even
  when our agent is not named and the first answer is "permitted".

That second case is the common one and the easy one to miss. Measured over the 165 hosts in
the oregon-counties survey: 8 block a named AI crawler, 5 name `ClaudeBot` with
`Disallow: /`, and **every commercial code vendor is among them** — Municode, American
Legal and General Code, several carrying `Content-Signal: search=yes, ai-train=no,
use=reference`, an explicit EU DSM Art. 4 rights reservation. The counties whose law is most
machine-readable are precisely the ones whose hosts refuse this use.

`Content-Signal` is surfaced as text, never interpreted — turning it into a boolean would
invent a policy the toolkit has no standing to set.

**An unreachable robots.txt is `unknown`, not permission.** 42 of those 165 hosts served
none. Before pointing a corpus at a new publisher, run the check and record the decision on
the source or group so it is reviewable in a PR and not re-derived on every run.

## Found a defect? Fix it. Filing an issue is the exception, and it has a cost.

**The default is to fix it in the change you are already making.** You are in the file with
the context loaded, which is the cheapest this fix will ever be. Filing an issue converts a
ten-minute fix into a future session that has to rebuild everything you currently know.

**Open an issue only when one of these is true:**

1. **It needs a decision you are not allowed to make** — a judgement about what the corpus
   means, a trade-off with a real cost, anything a grilling session would have put to the
   operator. Label it `ready-for-human`.
2. **It is large enough to need its own review** — if fixing it would make this change's diff
   hard for a reviewer to follow, it is separate work.
3. **It is in a file this change does not touch**, and reaching into it would widen the change
   beyond what its own review covers.

**If none of those is true, fix it now.** "I noticed it while doing something else" is not a
reason to defer; it is the reason it is cheap.

### An issue must name its trigger

Every issue states **what would make this matter** — the condition under which it stops being
latent. "Nothing currently escapes this" with no trigger is not a ticket. It is a comment at
the site, where the next person who can act on it will actually be standing.

**A comment in the code beats a ticket in a queue** whenever the person who would fix it is
the next person reading that code. Reserve the queue for work that has to be found by someone
who is *not* already in that file.

### Review findings are not issues

A code-review finding applied in the same change is already tracked by that review. Do not
also file it. An issue opened and closed within the hour adds a row to the backlog and tells
nobody anything.

### At most two issues per task

If you found more than two things worth another person's attention, the finding is that this
module needs work — and that is **one** issue naming the pattern, not five naming instances.
Ranking is the point: the third-most-important thing you noticed is usually a comment.

### Why this replaced "open an issue, period"

Measured in `executive-regulatory-frameworks` on 2026-08-29: **49 issues opened in two days,
20 closed, the backlog 19 → 48.** Of the 20 closures, 8 were review findings filed and fixed
inside the same hour — tracked already, and pure ceremony. Of the 29 left open, 3 needed a
human decision and roughly 12 were things the agent could have fixed while it was already in
the file.

The old rule's justification was that "nobody greps closed PRs six months later." True — and
nobody greps a 48-issue backlog either. A backlog nobody works is not a record; it is where a
defect goes to be forgotten with a clear conscience, and it buries the few issues that
genuinely need a person.

These all count as a defect, not just crashes:

- a check that passes without checking anything
- a documented command, flag, or path that does not exist or does not work
- a claim in a README, docstring, or catalog note that is no longer true
- data known to be wrong, stale, or incomplete
- a guard that cannot fire, or fires on the wrong condition
- something you worked around instead of fixing

**File it in the repo that owns the fix, which may not be the repo you are in.** A parser
defect here, a registry gap in a sibling corpus, and a validator gap in `corpus-toolkit`
are three different issues in three different repos. Say plainly in each which repo the
work belongs to.

An issue must answer four things, because an issue that only says "X is broken" costs the
next person the whole investigation again:

1. **What is wrong** — the specific behaviour, not a category
2. **How it was found** — the command, the data, the failing case
3. **What it breaks** — who or what gets a wrong answer, and how silently
4. **What would fix it**, or what still needs measuring before anyone can know

Prefer counts and reproductions over adjectives. "126 appropriations unjoined, of which 59
are an extraction gap and 41 are correct" is actionable; "agency matching needs work" is
not, and will be re-derived by someone else.

If you genuinely cannot open one — no network, no permission — say so explicitly in your
final message to the user and hand them the text to file. Silently dropping it is the one
outcome that is never acceptable.
