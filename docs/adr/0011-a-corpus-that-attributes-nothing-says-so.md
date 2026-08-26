# ADR-0011 — A corpus that attributes nothing says so, rather than answering zero

**Status:** accepted · **Date:** 2026-08-26

## Context

`documents_by_agency(slug)` is registered on every corpus. The gate is a capability check —
`callable(getattr(fw.backend, "documents_for_slug", None))` — and `FileBackend` defines that
method, so every corpus on the platform satisfies it.

Three of the eight live corpora attribute **no document to any issuing body** and always
answer `total: 0`, for every slug that exists:

| corpus | documents | attributed to an issuing body |
|---|---|---|
| oregon-budget | 1,762 | 0 |
| oregon-legislature | 6,137 | 0 |
| federal-reference | 43 | 0 |

The response is honest in full: `attribution.documents_with_no_issuing_body ==
documents_in_corpus` states plainly that this corpus attributes nothing. But `documents` and
`total` — the two fields the tool is named for, and the two most likely to be read alone —
say a clean, confident **zero**.

That is the collapse CONTEXT.md's outranking rule forbids:

> **"Could not check" is never reported as "is not there."** … If a new mechanism collapses
> those two answers, it is wrong regardless of what it is called.

The rule is not conditioned on a consumer currently misreading it. `no_graph` vs
`not_in_graph` and `status: ""` meaning unknown-and-never-`current` both exist whether or not
anyone is misreading them today, and `status: ""` is precedent that the rule governs **data
fields**, not only error paths.

## Decision

**A corpus that cannot attribute any document to any issuing body reports that it cannot
answer, rather than reporting zero matches.**

Two paired fields, the shape the contract already uses for `unresolved` + `sibling_unavailable`
— a state and its reason travelling together:

- `attribution.can_answer: true | false | unknown` — computed where the counts already live.
  `false` when the corpus attributes nothing; `unknown` when the coverage counts are missing,
  so that case is not silently folded into either answer.
- `total: null` when `can_answer` is `false`. The count is withheld rather than set to a
  number that means something else.

**The registration gate does not change.** Every corpus continues to advertise the tool.

## Alternatives rejected

**Unregister the tool for corpora that attribute nothing** — the fix corpus-toolkit#158
originally proposed. Rejected on three pieces of evidence the issue did not have:

- `server.py` already records reasoning against it: mirroring `agency_profile`'s narrower
  gate "would leave this tool unregistered on exactly the corpora it exists to serve".
- `contract_smoke.py` **raises** if the tool is absent on a file-backed corpus, calling it
  "the tool every corpus-gateway agency lookup depends on". Narrowing the gate fails the
  release gate.
- The one consumer that exists rejected participation-by-declaration deliberately:
  *"THE DISCRIMINATOR IS THE CORPUS'S OWN COUNTS, NOT ITS PROSE … it needs no gateway-side
  list of corpus ids."*

A tool that disappears is also a worse answer than one that says it cannot answer: absence is
indistinguishable from a corpus that is down.

**Fix only the prose.** `backends.py` promises that "a backend that cannot answer omits it",
which no corpus can do. That comment is wrong and is corrected — but correcting it alone
documents the collapse instead of ending it, and the outranking rule does not admit a
documentation-only remedy.

**Ship `can_answer` without nulling `total`.** Additive and breaks nothing, but a consumer
reading `total` alone still gets a clean zero. It warns about the trap without closing it.

## Consequences

**The vocabulary gets a home.** `corpus-gateway` names four outcomes — documents, none,
cannot-answer, unknown — and derives them from raw counts in one private function. Every
other consumer would re-derive them, and the naive derivation (`total == 0` means none) is
the wrong one. Those states become the toolkit's words, in CONTEXT.md and the contract, so
the correct reading is the one a consumer gets by default rather than by care.

**Contract impact.** `can_answer` is additive and stays contract v1, the way remote
resolution did at toolkit v1.1.0. `total: null` narrows a field the contract describes as
"the number of matches" — a consumer doing arithmetic on `total` without checking `error`
would break, which is the same discipline the contract already requires, since an `error`
response omits `attribution` entirely. It is called out in the contract text rather than
left to be discovered.

**The three corpora do not change.** Nothing about their content or configuration moves;
what changes is that the platform stops answering a question on their behalf that they never
had an answer to.
