"""corpus-toolkit#212: `compare` runs one query per jurisdiction and groups the answers.

The point is the failure a merged ranking has: a jurisdiction with more text outranks one
with less, and the caller cannot tell "no hits" from "outranked". Each group is its own
search, so neither can happen, and a jurisdiction with nothing is an explicit empty group.
"""
import re
from pathlib import Path

from tests.test_backends import DOC, corpus, fw   # noqa: F401  (corpus is a fixture)
from tests.test_overview_jurisdictions import _commit

from corpus_toolkit.config import load as load_config


def _add(root: Path, n: int, jurisdiction: str, body: str, tags=("ai",)) -> str:
    doc_id = f"wa-{n}"
    text = DOC.format(id=doc_id, title=f"Policy {n}", doc_type="statute",
                      citation=f"WA {n}", sha=f"{n:x}".rjust(64, "c"), tag=tags[0],
                      glance="Glance.", body=body)
    text = text.replace("status: current\n", f"status: current\njurisdiction: {jurisdiction}\n", 1)
    (root / "statutes" / f"{doc_id}.md").write_text(text)
    return doc_id


def _two_states(root: Path) -> None:
    """us-wa: five documents that say 'permit' over and over. us-or: one that says it once."""
    p = root / "statutes" / "ors-1.010.md"
    p.write_text(re.sub(r"^jurisdiction:.*\n", "", p.read_text(), flags=re.M)
                 .replace("status: current\n", "status: current\njurisdiction: us-or\n", 1))
    q = root / "statutes" / "ors-2.020.md"
    q.write_text(q.read_text().replace("status: current\n",
                                       "status: current\njurisdiction: us-wa\n", 1))
    for i in range(3, 8):
        _add(root, i, "us-wa", "permit permit permit permit permit. " * 8)
    _commit(root)


def _group(out, jur):
    return next(g for g in out["groups"] if g["jurisdiction"] == jur)


def test_a_small_jurisdiction_is_not_crowded_out_by_a_large_one(corpus):
    _two_states(corpus)
    f = fw(corpus)
    merged = f.search_corpus("permit", limit=3, mode="keyword")
    assert {h["id"] for h in merged}.isdisjoint({"ors-1.010"}), "fixture must crowd us-or out"
    out = f.compare("permit", ["us-or", "us-wa"], limit_per_jurisdiction=3, mode="keyword")
    assert [g["jurisdiction"] for g in out["groups"]] == ["us-or", "us-wa"]   # request order
    assert [h["id"] for h in _group(out, "us-or")["hits"]] == ["ors-1.010"]
    assert len(_group(out, "us-wa")["hits"]) == 3


def test_a_jurisdiction_with_no_match_is_an_explicit_empty_group(corpus):
    _two_states(corpus)
    out = fw(corpus).compare("permit", ["us-wa", "us-co"], mode="keyword")
    g = _group(out, "us-co")
    assert g["hits"] == [] and g["returned"] == 0 and g["has_hits"] is False
    assert g["in_corpus"] is False          # no document carries it: not "outranked"
    assert _group(out, "us-wa")["has_hits"] is True and _group(out, "us-wa")["in_corpus"] is True


def test_present_jurisdiction_with_no_match_says_so_differently(corpus):
    _two_states(corpus)
    out = fw(corpus).compare("zzzunmatchable", ["us-or"], mode="keyword")
    g = _group(out, "us-or")
    assert g["hits"] == [] and g["in_corpus"] is True       # held, but nothing matched


def test_no_hits_record_never_appears_as_a_hit(corpus):
    _two_states(corpus)
    out = fw(corpus).compare("zzzunmatchable", ["us-or"], mode="keyword")
    assert not any(h.get("no_hits") for h in _group(out, "us-or")["hits"])


def test_filters_apply_inside_every_group(corpus):
    _two_states(corpus)
    out = fw(corpus).compare("permit", ["us-or", "us-wa"], tags=["ai"], mode="keyword")
    assert _group(out, "us-or")["hits"] == []               # ors-1.010 is not tagged ai
    assert all(h["id"].startswith("wa-") for h in _group(out, "us-wa")["hits"])
    out = fw(corpus).compare("permit", ["us-wa"], doc_type="regulation", mode="keyword")
    assert _group(out, "us-wa")["hits"] == []
    assert out["filters"] == {"doc_type": "regulation"}


def test_envelope_and_echo(corpus):
    _two_states(corpus)
    out = fw(corpus).compare("permit", ["us-or", "us-or", " us-wa "], mode="keyword")
    assert [g["jurisdiction"] for g in out["groups"]] == ["us-or", "us-wa"]   # deduped, stripped
    assert out["query"] == "permit"
    for key in ("corpus", "archetype", "authoritative_source", "disclaimer", "contract_version"):
        assert key in out


def test_refusals_carry_error_and_no_groups(corpus):
    f = fw(corpus)
    for bad in ([], [""], None):
        out = f.compare("permit", bad)
        assert "error" in out and "groups" not in out


def test_backend_that_cannot_filter_is_not_asked(corpus):
    class _Legacy:
        name = "legacy"

        def __init__(self, config):
            self.config = config

        def search(self, query, *, doc_type=None, issuing_body=None, limit=10, mode="hybrid"):
            raise AssertionError("must not be called")

    f = fw(corpus)
    f.backend = _Legacy(f.config)
    out = f.compare("x", ["us-wa"])
    assert "error" in out and "groups" not in out and "cannot filter" in out["error"]


def test_the_mcp_tool_is_registered_and_round_trips(corpus):
    import pytest
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod
    _two_states(corpus)
    mcp = server_mod.build_server(load_config(str(corpus / "_meta" / "corpus.yml")))
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "compare")
    assert tool.parameters.get("required") == ["query", "jurisdictions"]
    out = tool.fn("permit", ["us-or", "us-wa"], mode="keyword")
    assert [g["jurisdiction"] for g in out["groups"]] == ["us-or", "us-wa"]


def test_compare_is_a_reserved_tool_name():
    from corpus_toolkit.mcp.server import RESERVED_TOOL_NAMES
    assert "compare" in RESERVED_TOOL_NAMES
