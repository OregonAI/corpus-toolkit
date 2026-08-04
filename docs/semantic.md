# Semantic search

Optional vector search over a prebuilt embeddings artifact, fused with the FTS5 keyword
index. Any corpus can enable it; none has to.

Undocumented until now — roughly 730 LOC across two releases (v1.17.0, v1.18.0) with no
entry in the README contents table, nothing in `docs/`, and no mention in
`reference-architecture.md` (corpus-toolkit#41).

## Enabling it

```yaml
# _meta/corpus.yml
plugins:
  semantic_search_module: "corpus_toolkit.semantic.search"
```

Seven corpora share this one module rather than seven copies drifting apart.
`plugins.load_module` looks for `<root>/corpus_toolkit/semantic/search.py` first and falls
back to `importlib.import_module`, so an installed-package path resolves with no per-repo
shim.

Install the extra, then build:

```bash
pip install "corpus-toolkit[semantic]"
corpus-build-semantic-index --config _meta/corpus.yml --backend sentence-transformers
corpus-build-semantic-index --config _meta/corpus.yml --check
```

## The artifact

Three files in `_meta/embeddings/`, written together or not at all:

| file | contents |
|---|---|
| `vectors.i8.npy` | int8 `[n_chunks, dim]`, each row L2-normalized × 127 |
| `chunks.jsonl` | one object per **row**, in row order: `{doc_id, heading, ordinal, preview}` |
| `meta.json` | `{backend, model, dim, granularity, n_chunks, fingerprint, …}` |

**Row → doc_id is positional**: row *i* is line *i* of `chunks.jsonl`. The only integrity
check is a length comparison at load, so a partially-written pair is not detectable.

**It is built, not committed.** Mount it at runtime. A corpus that commits it gains a large
binary that goes stale silently against its own content.

## Three sharp edges

### 1. Failure is silent by design

Every load error is swallowed and `available()` returns `False`, at which point
`search_corpus` serves keyword-only **with nothing in the response saying so**. A missing
mount looks exactly like working search with slightly worse results.

Corpora that enable this should assert `available()` in their healthcheck rather than
trusting that a green container means semantic search is running.

### 2. Pin `--backend` for production builds

`auto` reads the **build** host's GPU, not the serve host's. A rebuild on a GPU workstation
has already produced, once, an artifact the 2-core deploy host could not serve.

### 3. Section selection is per-corpus, and the default is wrong for some corpora

The original implementation took `## At a glance` plus `## Full text`, falling back to
`## Key provisions` — a correct description of one corpus and wrong for others.
**oregon-budget has no `## Full text` section anywhere**; its documents are generated
tables under `## Spending by band`, `## Largest vendors` and so on. Run unchanged there, it
would have embedded 1,761 glance paragraphs and silently indexed none of the actual
content.

So selection is config, with a whole-body fallback, and `--check` reports how much text
each document contributed — an empty corpus is visible rather than merely quiet.

```yaml
plugins:
  semantic_body_headings: ["At a glance", "Spending by band", "Largest vendors"]
```

## The plugin contract

Two functions, duck-typed by `corpus_toolkit.mcp.backends` at query time:

```python
def available() -> bool: ...
def rank(query: str, want: int) -> list[str]: ...   # -> doc_ids
```

What the backend does with them, which constrains what `rank` may do:

- it is called with a **pool** size, `max(limit * 4, 40)` — not the caller's limit;
- it must **not** apply `doc_type`/`issuing_body` filters, which the backend applies after;
- results are fused with BM25 by **reciprocal rank**, so only the ORDER matters — scores are
  never compared across the two retrievers, because BM25 and cosine are not on one scale.

## Relationship to `### ` subsections and chunk paging

Distinct mechanisms that are easy to conflate. Chunks here are an **embedding** unit,
recomputed deterministically by the same chunker the index was built with (no offsets are
stored, so nothing can drift). `### ` subsections are an **addressing** unit in the document
text, served by `get_document(part=…)` since v1.21.0. A search hit may name a `chunk:N`
ordinal for retrieval; a human-meaningful heading comes from the subsection list.
