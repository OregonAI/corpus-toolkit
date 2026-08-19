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

## Found a bug you are not fixing right now? Open an issue. Period.

This is not optional and has no size threshold.

If you discover a defect and do not fix it in the change you are working on, **open a
GitHub issue before you finish the task**. Not a note in the commit message, not a
paragraph in the PR body, not a line in your summary to the user. Those are not a work
queue — nobody greps closed PRs six months later, and the next agent rediscovers the same
bug from scratch, usually the expensive way.

This applies to every one of these, not just crashes:

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
