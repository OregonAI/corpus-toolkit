"""Response convention 1 must be visible to schema-driven validation (corpus-toolkit#15).

`corpus`, `archetype` and `authoritative_source` were already present in every
object-shaped response — in the JSON TEXT. Every tool was annotated `-> dict`, and a bare
`dict` is unconstrained, so the SDK emitted no `output_schema` at all and nothing could
check the convention without parsing prose.

The risk in fixing it is over-constraining: a declared schema that rejected a tool's own
payload would break every corpus. These tests pin BOTH halves — the fields are declared,
and arbitrary extras still pass.
"""
import asyncio

import pytest

from corpus_toolkit.mcp.server import ObjectResponse

CONVENTION_1 = ("corpus", "archetype", "authoritative_source")

# A minimal real corpus on disk. build_server needs one; nothing here depends on its
# content beyond a single valid document.
CORPUS_YML = """\
schema_version: 1
corpus:
  id: "t"
  name: "T"
  jurisdiction: "oregon"
  archetype: "document"
  authoritative_source: "https://example.invalid/official"
  schema_version: 1
  contract_version: 1
content_roots:
  - path: "docs"
    doc_type: doc
disclaimer_marker: "NON-AUTHORITATIVE"
"""

DOC = """\
---
schema_version: 1
corpus: t
id: alpha
title: Alpha
doc_type: doc
---

NON-AUTHORITATIVE test document.
"""


@pytest.fixture
def corpus_server(tmp_path):
    from corpus_toolkit import config as config_mod
    from corpus_toolkit.mcp.server import build_server

    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(CORPUS_YML)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "alpha.md").write_text(DOC)
    return build_server(config_mod.load(str(tmp_path / "_meta" / "corpus.yml")))


# Object-shaped tools. search_corpus is deliberately absent: it returns a list, and
# convention 1 is about object-shaped responses (corpus-toolkit#10).
OBJECT_TOOLS = ("get_document", "resolve_citation", "graph_neighbors", "corpus_overview",
                "authority_chain", "issuing_body_profile")


def _tools(built_server):
    mgr = getattr(built_server, "_tool_manager", None) or built_server
    return {t.name: t for t in mgr.list_tools()}


def test_object_tools_declare_the_convention_fields(corpus_server):
    tools = _tools(corpus_server)
    for name in OBJECT_TOOLS:
        if name not in tools:
            continue  # archetype-gated (authority_chain, issuing_body_profile)
        schema = getattr(tools[name], "output_schema", None)
        assert schema, f"{name} declares no output_schema — a bare `dict` emits none"
        props = schema.get("properties", {})
        for field in CONVENTION_1:
            assert field in props, f"{name}'s output_schema omits {field}"


def test_extras_are_not_forbidden(corpus_server):
    """The over-constraining guard. `additionalProperties: false` here would reject every
    tool's own payload — each response carries the three fields PLUS its real content."""
    tools = _tools(corpus_server)
    for name in OBJECT_TOOLS:
        if name not in tools:
            continue
        schema = getattr(tools[name], "output_schema", None) or {}
        assert schema.get("additionalProperties", None) is not False, (
            f"{name} forbids extra properties; its own payload would not validate")


def test_a_real_call_still_returns_its_full_payload(corpus_server):
    # THROUGH `_sdk.call_tool`, not the tool manager directly. `ToolManager.call_tool`
    # takes an optional `context` on mcp 1.x and a REQUIRED one on 2.x — an earlier
    # version of this test called the manager itself, passed on 1.x, and raised
    # TypeError on 2.x. Spanning that is exactly what the seam exists for; a test that
    # bypasses it re-earns the break it was written to prevent.
    from corpus_toolkit.mcp import _sdk

    result = asyncio.new_event_loop().run_until_complete(
        _sdk.call_tool(corpus_server, "corpus_overview", {}))
    d = result if isinstance(result, dict) else result[0]
    for field in CONVENTION_1:
        assert field in d, f"corpus_overview response lost {field}"
    assert len(d) > len(CONVENTION_1), (
        "response carries only the declared fields — the schema constrained the payload")


def test_object_response_is_total_false():
    """An error response may carry none of the three and must still validate."""
    assert getattr(ObjectResponse, "__total__", True) is False
