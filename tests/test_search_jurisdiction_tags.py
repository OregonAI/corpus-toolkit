"""corpus-toolkit#211: `search_corpus` filters on `jurisdiction` and `tags`.

Both are optional. A filter has to hold on the keyword side AND the semantic side, and
must still hold when the semantic artifact is absent (hybrid degrades to keyword). An
unfiltered search is unchanged.
"""
import re
from pathlib import Path

from tests.test_backends import corpus, fw   # noqa: F401  (corpus is a fixture)
from tests.test_overview_jurisdictions import _commit

from corpus_toolkit.config import load as load_config
from corpus_toolkit.mcp.backends import FileBackend


def _edit(root: Path, doc_id: str, jurisdiction: str | None, tags: list[str]) -> None:
    p = root / "statutes" / f"{doc_id}.md"
    text = re.sub(r"^(jurisdiction|tags):.*\n", "", p.read_text(), flags=re.M)
    extra = f"tags: [{', '.join(repr(t) for t in tags)}]\n"
    if jurisdiction:
        extra += f"jurisdiction: {jurisdiction}\n"
    p.write_text(text.replace("status: current\n", "status: current\n" + extra, 1))


def _three_docs(root: Path) -> None:
    """ors-1.010 us-or [ai, privacy]; ors-2.020 us-wa [ai]; a third with no own jurisdiction."""
    _edit(root, "ors-1.010", "us-or", ["ai", "privacy"])
    _edit(root, "ors-2.020", "us-wa", ["ai"])
    _commit(root)


class _RanksEverything:
    def available(self):
        return True

    def rank(self, query, want):
        return ["ors-2.020", "ors-1.010"]


class _Absent(_RanksEverything):
    def available(self):
        return False


def _ids(hits):
    return sorted(h["id"] for h in hits)


def test_jurisdiction_filter_on_the_keyword_side(corpus):
    _three_docs(corpus)
    hits = fw(corpus).search_corpus("permit", jurisdiction="us-wa", mode="keyword")
    assert _ids(hits) == ["ors-2.020"]


def test_tag_filter_on_the_keyword_side(corpus):
    _three_docs(corpus)
    assert _ids(fw(corpus).search_corpus("permit", tags=["privacy"],
                                         mode="keyword")) == ["ors-1.010"]


def test_several_tags_must_all_be_carried(corpus):
    _three_docs(corpus)
    assert _ids(fw(corpus).search_corpus("permit", tags=["ai"],
                                         mode="keyword")) == ["ors-1.010", "ors-2.020"]
    assert _ids(fw(corpus).search_corpus("permit", tags=["ai", "privacy"],
                                         mode="keyword")) == ["ors-1.010"]


def test_a_tag_is_matched_whole_not_as_a_word_in_the_search_text(corpus):
    """`tags` are also FTS text; a filter on `ai` must not be satisfied by `air`."""
    _edit(corpus, "ors-1.010", "us-or", ["air-quality"])
    _commit(corpus)
    assert fw(corpus).search_corpus("permit", tags=["air"], mode="keyword")[0].get("no_hits")


def test_jurisdiction_filter_holds_on_the_semantic_side(corpus):
    _three_docs(corpus)
    cfg = load_config(str(corpus / "_meta" / "corpus.yml"))
    be = FileBackend(cfg, _RanksEverything())
    # The semantic module ranks us-wa first; the filter must drop the other document and,
    # in semantic mode, nothing else can supply a result.
    assert _ids(be.search("zzzz", jurisdiction="us-or", mode="semantic")) == ["ors-1.010"]
    assert _ids(be.search("zzzz", tags=["privacy"], mode="semantic")) == ["ors-1.010"]
    assert _ids(be.search("permit", jurisdiction="us-wa", mode="hybrid")) == ["ors-2.020"]
    assert _ids(be.search("permit", tags=["privacy"], mode="hybrid")) == ["ors-1.010"]


def test_filter_holds_when_the_semantic_artifact_is_absent(corpus):
    _three_docs(corpus)
    cfg = load_config(str(corpus / "_meta" / "corpus.yml"))
    be = FileBackend(cfg, _Absent())
    assert _ids(be.search("permit", jurisdiction="us-wa", mode="hybrid")) == ["ors-2.020"]
    assert _ids(be.search("permit", tags=["privacy"], mode="semantic")) == ["ors-1.010"]
    be = FileBackend(cfg, None)
    assert _ids(be.search("permit", jurisdiction="us-or")) == ["ors-1.010"]


def test_a_document_without_its_own_jurisdiction_matches_the_declared_one(corpus):
    assert _ids(fw(corpus).search_corpus("permit", jurisdiction="oregon",
                                         mode="keyword")) == ["ors-1.010", "ors-2.020"]


def test_filters_combine_with_the_existing_ones(corpus):
    _three_docs(corpus)
    out = fw(corpus).search_corpus("permit", doc_type="statute", jurisdiction="us-wa",
                                   tags=["ai"], mode="keyword")
    assert _ids(out) == ["ors-2.020"]


def test_an_empty_filtered_answer_says_what_was_searched_for(corpus):
    _three_docs(corpus)
    out = fw(corpus).search_corpus("permit", jurisdiction="us-ca", tags=["ai"])
    assert len(out) == 1 and out[0]["no_hits"] is True
    assert "id" not in out[0]
    assert out[0]["filters"] == {"jurisdiction": "us-ca", "tags": ["ai"]}
    assert "us-ca" in out[0]["note"]


def test_unfiltered_search_is_unchanged(corpus):
    _three_docs(corpus)
    hits = fw(corpus).search_corpus("permit")
    assert _ids(hits) == ["ors-1.010", "ors-2.020"]
    assert set(hits[0]) == {"id", "title", "citation", "doc_type", "issuing_body",
                            "path", "snippet"}
    # Blank values mean "no filter", as the MCP tool's empty-string defaults do.
    assert fw(corpus).search_corpus("permit", jurisdiction="", tags=[]) == hits


class _LegacyBackend:
    name = "legacy"

    def __init__(self, config, semantic=None):
        self.config = config

    def search(self, query, *, doc_type=None, issuing_body=None, limit=10, mode="hybrid"):
        return [{"id": "legacy:1", "title": "L", "citation": "L 1", "doc_type": "policy",
                 "issuing_body": "", "path": "l.md", "snippet": "..."}]


def test_a_backend_that_cannot_filter_does_not_answer_unfiltered(corpus):
    """An adapter whose search() names neither keyword would return everything, and the
    caller would read that as the filtered answer. Say it could not filter instead."""
    from corpus_toolkit.mcp.framework import CorpusFramework
    f = fw(corpus)
    f.backend = _LegacyBackend(f.config)
    assert f.search_corpus("x")[0]["id"] == "legacy:1"          # unfiltered still works
    out = f.search_corpus("x", jurisdiction="us-wa")
    assert len(out) == 1 and out[0]["no_hits"] is True
    assert out[0]["filters_unsupported"] == ["jurisdiction"]
    assert "NOT applied" in out[0]["note"]


def test_the_mcp_tool_exposes_both_filters(corpus):
    """Through the registered tool, with the empty-string/None defaults a client sends."""
    import pytest
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod
    _three_docs(corpus)
    mcp = server_mod.build_server(load_config(str(corpus / "_meta" / "corpus.yml")))
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "search_corpus")
    props = tool.parameters["properties"]
    assert "jurisdiction" in props and "tags" in props
    assert tool.parameters.get("required") == ["query"]      # both are optional
    fn = tool.fn
    assert _ids(fn("permit", jurisdiction="us-wa", tags=["ai"], mode="keyword")) == ["ors-2.020"]
    assert _ids(fn("permit", mode="keyword")) == ["ors-1.010", "ors-2.020"]


class _CrowdedRanker:
    """Ranks every large-jurisdiction document above the one small-jurisdiction document,
    and honours `want` the way the real semantic module does: it returns at most that many."""

    def __init__(self, order):
        self.order = order

    def available(self):
        return True

    def rank(self, query, want):
        return self.order[:want]


def test_a_small_jurisdiction_is_not_crowded_out_of_the_semantic_pool(corpus):
    """257 us-wa documents rank above the single us-or one. The filter runs after the
    ranker's cut, so a fixed pool of 200 returned "no hits" for us-or; the pool has to
    cover every candidate when a filter is set."""
    from tests.test_compare import _add
    _edit(corpus, "ors-1.010", "us-or", ["ai"])
    _edit(corpus, "ors-2.020", "us-wa", ["ai"])
    wa = [_add(corpus, i, "us-wa", "Something else entirely.") for i in range(3, 3 + 255)]
    _commit(corpus)
    cfg = load_config(str(corpus / "_meta" / "corpus.yml"))
    be = FileBackend(cfg, _CrowdedRanker(wa + ["ors-2.020", "ors-1.010"]))
    assert _ids(be.search("zzzz", jurisdiction="us-or", mode="semantic")) == ["ors-1.010"]
    assert _ids(be.search("zzzz", tags=["ai"], jurisdiction="us-or",
                          mode="hybrid")) == ["ors-1.010"]


def test_compare_does_not_read_a_crowded_out_jurisdiction_as_no_hits(corpus):
    """The #212 failure through the tool: in_corpus is true, so an empty us-or group would
    claim "documents carry this jurisdiction and none matched" while one does."""
    from tests.test_compare import _add
    _edit(corpus, "ors-1.010", "us-or", ["ai"])
    _edit(corpus, "ors-2.020", "us-wa", ["ai"])
    wa = [_add(corpus, i, "us-wa", "Something else entirely.") for i in range(3, 3 + 255)]
    _commit(corpus)
    f = fw(corpus)
    f.backend = FileBackend(f.config, _CrowdedRanker(wa + ["ors-2.020", "ors-1.010"]))
    out = f.compare("zzzz", ["us-or", "us-wa"], mode="semantic")
    g = next(g for g in out["groups"] if g["jurisdiction"] == "us-or")
    assert g["in_corpus"] is True and g["has_hits"] is True
    assert [h["id"] for h in g["hits"]] == ["ors-1.010"]
