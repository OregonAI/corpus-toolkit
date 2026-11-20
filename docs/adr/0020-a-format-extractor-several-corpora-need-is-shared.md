# ADR-0020 — A format extractor that several corpora need is shared; cleanup stays local

**Status:** accepted · **Date:** 2026-11-19 · decided by the owner · amends [ADR-0016](0016-ingest-primitives-are-shared-orchestration-is-local.md) (the "Local, in the corpus" list: "how to turn bytes into text")

## Context

ADR-0016 kept "how to turn bytes into text (PDF cleanup, page furniture, HTML slicing)" local
to each corpus. That covered per-source cleanup well. It also, read literally, covered
reading a file format at all. Practice already differed: `corpus_toolkit.html_to_text` was a
shared extractor, and PDF text is `pdftotext -layout` everywhere through the toolkit's
snapshot and hash code.

Word files broke the literal reading. `OregonAI/states-government-it` wrote a pandoc-based
reader (`src/ingest_docx.py`, 184 lines at 63fe68a). `OregonAI/states-career-technical-education`
needed the same reader for state Perkins plans and copied it verbatim, so one file was kept in
two repos with a header telling readers to change it in the first one. Four known defects
were fixed in neither. corpus-toolkit#219 asked for one shared reader, and the owner approved
it.

## Decision

1. **A reader for a file format moves into the toolkit once a second corpus needs it.** It
   is a pure function from bytes to text, plus a structured form where consumers need one.
   `html_to_text` and `docx_to_text` (`docx_to_text()` and `docx_blocks()`, corpus-toolkit#219)
   are the two. The first corpus may keep its own reader until a second one needs it.
2. **What stays local is unchanged in every other respect.** Per-source cleanup (page
   furniture, a state's statute page layout, slicing a section out of a chapter page), which
   extractor a corpus calls for which source, what the body says, and identity checks stay
   in the corpus, as ADR-0016 says.
3. **A shared extractor's output is public surface.** A change to the text it produces for
   unchanged input re-derives documents in every corpus that adopted it. So that change
   lists the affected files in CHANGELOG.md and MIGRATION.md, as #219 did for its 8 of 52
   Word files.
4. **A shared extractor does not by itself enter the drift hash.** `content_hash` and the
   Fetcher keep hashing a Word file's raw bytes. Routing a format through an extractor in
   the hash would re-baseline every source of that format on the platform, and would make
   drift runs need the extractor's tools (pandoc). That would be its own decision.

## Consequences

- A corpus that adopts a shared reader deletes its copy and re-derives the documents the
  shared reader reads differently. states-government-it and states-career-technical-education
  do this after the release that ships #219.
- A new external tool requirement comes with the reader (pandoc ≥ 3.11 for Word). It is
  checked at call time with a clear error, and installed in the toolkit's own CI so the
  reader's tests run there.
- ADR-0016's list of shared primitives gains "format extractors once two corpora need them".
  Its boundary for everything else stands.
