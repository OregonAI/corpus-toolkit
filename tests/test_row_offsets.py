"""The named column offsets must actually name the columns the SELECTs return.

`KW_*` was introduced because positional reads "had to be found and re-counted by hand
every time the SELECT changed" — and then two of the three readers kept counting by hand:
`search()`'s semantic-only branch read `mr[0], mr[1], mr[3]…` off `_doc_meta_row`, whose
projection is column-for-column identical to the one named beside it, and `get()` read
`r[0]…r[11]` off `_doc_row` across sixty lines.

Naming the offsets only helps if the names stay true, and nothing checked that. These tests
execute the real queries against a real corpus and assert each constant lands on the column
it is named for, so reordering a SELECT without updating the constants fails here rather
than surfacing as a document served with its citation in the title field.

Also pins `extract_section` as a plain function: it was a FileBackend method that
`CorpusFramework._extract_section` called as `FileBackend._extract_section(self, ...)`,
handing an unrelated class's instance in as `self`.
"""
import inspect
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp import backends as B
from corpus_toolkit.mcp.backends import FileBackend
from corpus_toolkit.mcp.framework import CorpusFramework

DOC = """---
schema_version: 1
id: ors-1.010
title: "Definitions for water rights"
doc_type: statute
citation: "ORS 1.010"
authority_level: statute
issuing_body: "Water Resources Department"
source_url: "https://example.invalid/ors-1.010"
source_format: html
retrieved: "2026-07-26"
source_sha256: "aaaa"
status: current
content_mode: verbatim
effective_date: "2024-01-01"
last_verified: "2026-07-26"
verified_by: "@test"
tags: ["ors"]
---

## At a glance

Defines terms.

## Full text

A person may not appropriate water without a permit.
"""


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "statutes").mkdir()
    (tmp_path / "_meta").mkdir()
    (tmp_path / "statutes" / "ors-1.010.md").write_text(DOC)
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""
        corpus:
          id: test-corpus
          name: Test Corpus
          jurisdiction: oregon
          archetype: document
        content_roots:
          - path: statutes
            doc_type: statute
        graph_path: _meta/graph.json
    """).strip() + "\n")
    (tmp_path / "_meta" / "graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def backend(corpus: Path) -> FileBackend:
    return FileBackend(load_config(str(corpus / "_meta" / "corpus.yml")))


# ------------------------------------------------------------------ KW_* / _doc_meta_row

def test_kw_offsets_name_the_doc_meta_row_columns(corpus):
    """The projection `search()`'s semantic-only branch reads."""
    fb = backend(corpus)
    r = fb._doc_meta_row(fb.ensure_index(), "ors-1.010")

    assert r[B.KW_ID] == "ors-1.010"
    assert r[B.KW_TITLE] == "Definitions for water rights"
    assert r[B.KW_CITATION] == "ORS 1.010"
    assert r[B.KW_DOCTYPE] == "statute"
    assert r[B.KW_ISSUER] == "Water Resources Department"
    assert r[B.KW_PATH] == "statutes/ors-1.010.md"


def test_kw_offsets_name_the_fts_row_columns(corpus):
    """The same six columns, plus bm25, out of the keyword query — the pair being
    identical is exactly why one of them got read positionally."""
    fb = backend(corpus)
    rows, order = fb._fts_rows(fb.ensure_index(), "permit", None, None, 10)
    r = rows[order[0]]

    assert r[B.KW_ID] == "ors-1.010"
    assert r[B.KW_TITLE] == "Definitions for water rights"
    assert r[B.KW_CITATION] == "ORS 1.010"
    assert r[B.KW_DOCTYPE] == "statute"
    assert r[B.KW_ISSUER] == "Water Resources Department"
    assert r[B.KW_PATH] == "statutes/ors-1.010.md"
    assert isinstance(r[B.KW_BM25], float)


# ---------------------------------------------------------------------- DOC_* / _doc_row

def test_doc_offsets_name_the_doc_row_columns(corpus):
    """A wider, DIFFERENTLY ORDERED projection than KW_* — note DOC_PATH is 1 where
    KW_TITLE is 1. Reading one tuple with the other's constants is silent and wrong,
    which is the failure this pins."""
    r = backend(corpus)._doc_row("ors-1.010")

    assert r[B.DOC_ID] == "ors-1.010"
    assert r[B.DOC_PATH] == "statutes/ors-1.010.md"
    assert r[B.DOC_DOCTYPE] == "statute"
    assert r[B.DOC_CITATION] == "ORS 1.010"
    assert r[B.DOC_TITLE] == "Definitions for water rights"
    assert r[B.DOC_STATUS] == "current"
    assert r[B.DOC_SOURCE_URL] == "https://example.invalid/ors-1.010"
    assert r[B.DOC_RETRIEVED] == "2026-07-26"
    assert r[B.DOC_EFFECTIVE] == "2024-01-01"
    assert r[B.DOC_CONTENT_MODE] == "verbatim"
    assert r[B.DOC_EXCEPTION] == ""
    assert isinstance(r[B.DOC_SIZE], int) and r[B.DOC_SIZE] > 0


def test_get_maps_every_named_column_onto_the_right_field(corpus):
    """The end-to-end consequence: a reordered SELECT would put the citation in `title`
    and nothing else in the suite would notice."""
    d = backend(corpus).get("ors-1.010")

    assert d["id"] == "ors-1.010"
    assert d["title"] == "Definitions for water rights"
    assert d["citation"] == "ORS 1.010"
    assert d["doc_type"] == "statute"
    assert d["status"] == "current"
    assert d["source_url"] == "https://example.invalid/ors-1.010"
    assert d["authoritative_source"] == d["source_url"]
    assert d["effective_date"] == "2024-01-01"
    assert d["content_mode"] == "verbatim"
    assert d["path"] == "statutes/ors-1.010.md"


# ------------------------------------------------------------------------ extract_section

def test_extract_section_is_a_plain_function():
    """No `self`. It was a FileBackend method, and CorpusFramework called it as
    `FileBackend._extract_section(self, body, heading)` — an unbound method of an
    unrelated class handed a CorpusFramework as `self`. That holds only while the body
    ignores `self`, and nothing would have flagged the line that stopped ignoring it."""
    assert list(inspect.signature(B.extract_section).parameters) == ["body", "heading"]
    assert B.FileBackend._extract_section is B.extract_section


def test_the_framework_helper_agrees_with_the_module_function(corpus):
    body = "## At a glance\n\nDefines terms.\n\n## Full text\n\nBody.\n"
    fw = CorpusFramework(load_config(str(corpus / "_meta" / "corpus.yml")))

    assert fw._extract_section(body, "At a glance") == "Defines terms."
    assert fw._extract_section(body, "At a glance") == B.extract_section(body, "At a glance")
    assert fw._extract_section(body, "Nope") is None
