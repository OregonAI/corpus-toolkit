# Provenance Frontmatter Schema — v1

The generic, corpus-agnostic metadata standard. It is the oregon-policy-repo
schema with three additions (`schema_version`, `corpus`, `jurisdiction`) and
requiredness rules expressed per archetype. Machine form:
`schemas/document.frontmatter.v1.schema.json`.

## Fields

| Field | Req | Applies | Notes |
|---|---|---|---|
| `schema_version` | ✅ | all | literal `1` |
| `corpus` | ✅ | all | corpus id = repo name |
| `jurisdiction` | ✅ | all | e.g. `oregon`, `oregon/marion-county`, `oregon/salem` |
| `id` | ✅ | all | stable slug = filename |
| `title` | ✅ | all | |
| `doc_type` | ✅ | all | shared enum (statute, rule, executive_order, policy, procedure, standard, manual, schedule, ordinance, entity_doc, dataset_doc, external_reference, audit_report, federal_instrument, performance_report), extensible per corpus since v1.19.0 via corpus.yml `schema.doc_types: [{name, verbatim}]` — this claim predated the mechanism (corpus-toolkit#40) |
| `citation` | ✅ | all | human citation string |
| `authority_level` | ✅ | doc/hybrid docs | free string; no toolkit code ranks or validates it (an earlier claim of a ranked, extensible enum described a design that was never built) |
| `issuing_body` | ✅ | all | |
| `legal_authority` | – | doc/hybrid docs | upstream citations |
| `source_url` | ✅ | all | pinned URL or API base for entity docs |
| `source_format` | ✅ | all | pdf, html, xml, json, odata, soda |
| `retrieved` | ✅ | doc/hybrid docs | ISO date fetched |
| `source_sha256` | ✅ | doc/hybrid docs | hash of OUR RENDERING of the source (the committed extraction, via hash_snapshot) — never the upstream bytes. It proves the committed text has not drifted since ingest; it CANNOT prove extraction captured everything (that is corpus-verify-extraction's job) |
| `effective_date`, `last_reviewed`, `source_version` | – | doc/hybrid docs | transcribed exactly as source prints them |
| `status` | ✅ | all | current, superseded, repealed, proposed, draft, suspended — `suspended` is in force until recently, out of force now, expected to return: a temporary, usually dated loss of force, as distinct from `repealed`'s permanent one (corpus-toolkit#159) |
| `content_mode` | ✅ | doc/hybrid docs | `verbatim` for jurisdiction-authored; `summary` only for third-party/external_reference |
| `content_exception` / `migration_pending` | – | doc/hybrid docs | escape hatches, CI warns |
| `conversion_notes` | – | doc/hybrid docs | what was stripped/normalized |
| `last_verified`, `verified_by` | ✅ | all | human verification record; for entity docs = schema checked against live API |
| `maintainer` | ✅ | all | CODEOWNERS handle |
| `relationships` | – | all | implements, implemented_by, references_external, related, supersedes; values may be local ids or `corpus:id` remote refs |
| `tags` | – | all | |

## Archetype notes

- **API corpora** use `doc_type: entity_doc` (one file per entity/endpoint)
  and `dataset_doc` (one per dataset). `retrieved`/`source_sha256` are
  replaced by `last_verified` against the live schema; the CI schema-drift
  job updates a `live_schema_hash` field.
- **Hybrid** join files use `doc_type: dataset_doc` plus a `joins:` list of
  `{document_id, dataset, key}` entries.

  **Referential integrity is split, and only half of it is the toolkit's.**
  This page previously said "CI checks referential integrity" full stop,
  which was not true of any half: the field was shape-validated only and
  nothing read it, so a corpus could ship joins pointing at documents that
  do not exist with every gate green (corpus-toolkit#3). What holds now:

  | part | checked by | on failure |
  |---|---|---|
  | entry shape `{document_id, dataset, key}` | the frontmatter JSON schema | error |
  | `document_id` resolves to a document in this corpus | `corpus-validate-frontmatter` | error |
  | `{dataset, key}` selects at least one row | **the corpus, not the toolkit** | — |

  The last row cannot move into the toolkit. Only the corpus knows what one
  of its dataset keys means, so a corpus shipping `joins:` owes itself a
  `--check` of its own (`oregon-budget`'s is `src/build_joins.py --check`)
  wired into the `generated` CI job. Without it a join whose key matches
  zero rows is silent — and "no relationship recorded" reads exactly like
  "no relationship exists".

## Versioning policy

Additive changes (new optional fields) stay v1. Renames, removals, or
requiredness changes bump to v2 with a migration note in this doc and a
toolkit major-version bump. Validators accept exactly the versions they
ship with.
