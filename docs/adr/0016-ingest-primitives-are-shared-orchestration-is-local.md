# ADR-0016 — Ingest primitives are shared; ingest orchestration stays in the corpus

**Status:** accepted · **Date:** 2026-09-04

## Context

The platform's nine corpora each wrote their own ingest pipeline, and by September 2026 the
census read: nine fetchers (one of them, oregon-counties' `src/fetch.py`, handling 429s,
`Retry-After`, bot challenges and per-host politeness; the other eight a bare `urllib` call
with a `sleep(2)`), eight User-Agent strings including two that impersonate a browser, nine
frontmatter assemblers (five f-string templates in ERF alone) that found out from CI what
they had forgotten, a 45-line name-normalisation block copied verbatim into three repos, and
two of nine ingesters moving the drift baseline at ingest while the other seven left the next
drift run to report every fresh document as changed. ERF's own comment records the failure
class: platform-required fields "silently lacked" from generated documents because no shared
assembler existed. Until now the boundary between toolkit and corpus was practice and
comment — `changes.py` declined to port the SharePoint listing diff, and the template told a
new corpus to "write an ingester" — never a decision.

The operator's answer to whether ingest continues: "the majority of new ingestions are done,
but we will still grow." Payback is therefore per new ingest, not per line migrated.

## Decision

**Shared, in `corpus-toolkit`:** the mechanics every ingest performs identically —

- **fetching** (`corpus_toolkit.sources.fetch.Fetcher`): HTTP/2, one honest User-Agent
  naming the platform, the toolkit version and the corpus repository, per-host politeness,
  429 backoff honouring `Retry-After`, refusals and bot challenges raised as `Refused` /
  `Challenge` rather than returned as bodies, TLS chain supplements via ADR-0012, robots.txt
  reported and enforced only on the corpus's explicit opt-in (AGENTS.md's rule stands);
- **snapshot recording** (`corpus_toolkit.sources.snapshots.record_snapshot`): the raw and
  text files under `snapshot_dir`, `hash_snapshot` for the document's `source_sha256`,
  `content_hash` for the drift baseline, a `fresh` flag that alone may advance `retrieved`;
- **baseline recording** (`corpus_toolkit.sources.manifest.record_baseline`): the one writer
  of a source's `sha256` in the source manifest, used by the drift detector and by ingest.
  **Ingest moves the baseline**, because a baseline is what the mirror holds (the lesson of
  the 2026-09 repo_lib work: accepting an upstream hash the mirror does not hold makes the
  drift report silent about a page it has not got);
- **document writing** (`corpus_toolkit.documents.write_document`): frontmatter in the
  schema's key order, platform defaults, `last_verified`/`verified_by` left empty (a human
  act, `docs/verification-loop.md`), validated against the schema and the corpus's declared
  doc_types before anything is written;
- **hashing** and **frontmatter parsing**, which were already shared.

**Local, in the corpus:** what to fetch (source manifests, discovery, profiles), how to turn
bytes into text (PDF cleanup, page furniture, HTML slicing), what the body says, which
relationships hold, and any decision to enforce robots.txt or to override the User-Agent —
the last two recorded in the source manifest so they are reviewable.

**Adoption is on touch.** corpus-template ships an example ingester built on the shared
primitives and its setup step says to use them; oregon-counties (the fetcher's origin) and
oregon-collective-bargaining adopt now as the proof; every other ingest script switches when
it is next edited. Nothing forces a working script to move.

**The drift detector keeps its own transport.** Its monthly sweep of 8,105 sources runs in an
hour with no per-host interval, and its User-Agent token is what robots.txt directives are
matched against. It shares the baseline recorder and nothing else.

## Consequences

- A new corpus's ingester is orchestration only: read the manifest, `Fetcher.get`,
  `sniff`, convert, `record_snapshot`, `write_document`. The failure modes counties met once
  are met once.
- Documents from different corpora read the same way, and a missing platform field is a
  refusal at write time rather than a red CI job later.
- The next drift run after an ingest reports only what changed upstream since, in every
  corpus, not just the two that remembered to move the baseline.
- Browser-spoofing User-Agents leave the platform as scripts are touched; a host that then
  refuses the honest agent is recorded as refusing, which is the truthful state.
- Everything reachable from a corpus is public surface (AGENTS.md); these names are a
  compatibility commitment from v1.36.0.
