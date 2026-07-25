# AGENTS.md — corpus-toolkit

This repo is the shared platform for the {{ORG}} civic corpus system. It
contains tooling and specs only — never civic content.

## Rules
- Read `docs/reference-architecture.md` before changing anything structural.
- Schema or MCP-contract changes require updating the matching doc in the
  same PR; breaking changes bump the major version.
- Reusable workflows must stay corpus-agnostic: all corpus specifics come
  from the calling repo's `_meta/corpus.yml` and manifest.
- Conventional commits. All changes via PR.
- Never weaken a guardrail (validator, diff check, review gate) to make a
  corpus ingest easier; fix the corpus instead.
