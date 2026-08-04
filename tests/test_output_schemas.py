"""The output-schema declaration on object-shaped tools must not own the payload.

v1.24.0 declared a TypedDict return on the six object tools to make response convention 1
visible to schema-driven validation (corpus-toolkit#15) and took all four live corpora
down with it (corpus-toolkit#61). Two distinct failures, one cause — a TypedDict return
makes the SDK build a pydantic model and push every response through it:

  1. `authoritative_source: str` rejects None, which is the DOCUMENTED value for a corpus
     declaring no source. `total=False` makes a key optional, not nullable. corpus_overview,
     resolve_citation and unknown-id get_document became hard ValidationErrors everywhere.
  2. Undeclared keys are dropped on the way out. get_document returned its envelope and no
     document body — deleted at serialization, silently, the call still reporting success.

THE TESTS THAT SHIPPED WITH IT PASSED, and that is the part worth not repeating. They
asserted on the emitted schema's `additionalProperties` (absent, so extras validate) and
called tools through `_sdk.call_tool`, which passes `convert_result=False` — so the one
step that discards the payload was the one step never exercised. A schema assertion cannot
see this. Every test here round-trips a real payload through `convert_result`.
"""
import asyncio

import pytest

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

# The same corpus declaring NO authoritative_source. Supported and documented: config.py
# types it `str | None`, and convention 1 says such a corpus gets `authoritative_source:
# null` plus a config_warning. This is the fixture the TypedDict could not serve.
CORPUS_YML_NO_SOURCE = CORPUS_YML.replace(
    '  authoritative_source: "https://example.invalid/official"\n', "")

DOC = """\
---
schema_version: 1
corpus: t
id: alpha
title: Alpha
doc_type: doc
---

NON-AUTHORITATIVE test document.

The body text that get_document must return, and that v1.24.0 dropped.
"""

BODY_MARKER = "The body text that get_document must return"


def _build(tmp_path, corpus_yml):
    from corpus_toolkit import config as config_mod
    from corpus_toolkit.mcp.server import build_server

    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(corpus_yml)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "alpha.md").write_text(DOC)
    return build_server(config_mod.load(str(tmp_path / "_meta" / "corpus.yml")))


@pytest.fixture
def corpus_server(tmp_path):
    return _build(tmp_path, CORPUS_YML)


@pytest.fixture
def sourceless_server(tmp_path):
    return _build(tmp_path, CORPUS_YML_NO_SOURCE)


# Object-shaped tools. search_corpus is deliberately absent: it returns a list, and
# convention 1 is about object-shaped responses (corpus-toolkit#10).
OBJECT_TOOLS = ("get_document", "resolve_citation", "graph_neighbors", "corpus_overview",
                "authority_chain", "issuing_body_profile")


def _tools(built_server):
    mgr = getattr(built_server, "_tool_manager", None) or built_server
    return {t.name: t for t in mgr.list_tools()}


def _serialize(tool, payload):
    """Push a payload through the tool's OWN declared output schema, the way the SDK does
    when answering a client — the step `_sdk.call_tool(..., convert_result=False)` skips,
    and skipping it is why the regression shipped green.

    Through `_sdk.structured_result` rather than the SDK directly, because the result of
    that conversion is one of the things the 1.x/2.x break moved: 1.x returns a tuple, 2.x
    a `CallToolResult`. An earlier version of this file unpacked the tuple inline, passed
    on 1.x, and asserted against the wrapper OBJECT on 2.x — where `field in out` is a
    membership test on a pydantic model and quietly answers False. Version-dependent
    shapes belong behind the seam; that is what it is for.
    """
    from corpus_toolkit.mcp import _sdk

    return _sdk.structured_result(tool, payload)


def _call(server, name, arguments):
    from corpus_toolkit.mcp import _sdk

    result = asyncio.new_event_loop().run_until_complete(
        _sdk.call_tool(server, name, arguments))
    return result if isinstance(result, dict) else result[0]


def test_declared_schema_does_not_forbid_extras(corpus_server):
    """The schema half of the guard. Necessary, and — as v1.24.0 proved — nowhere near
    sufficient on its own; the tests below cover what this one cannot see."""
    tools = _tools(corpus_server)
    for name in OBJECT_TOOLS:
        if name not in tools:
            continue  # archetype-gated (authority_chain, issuing_body_profile)
        schema = getattr(tools[name], "output_schema", None) or {}
        assert schema.get("additionalProperties", None) is not False, (
            f"{name} forbids extra properties; its own payload would not validate")


def test_serialization_keeps_the_document_body(corpus_server):
    """Bug 2. The response reaching a client must carry the document, not just the
    envelope. Asserted on a REAL get_document result, converted the way a client gets it."""
    tools = _tools(corpus_server)
    payload = _call(corpus_server, "get_document", {"doc_id": "alpha"})
    assert BODY_MARKER in str(payload), (
        "the tool itself lost the body — this is a framework bug, not a schema one")

    out = _serialize(tools["get_document"], payload)
    assert BODY_MARKER in str(out), (
        "the declared output schema DROPPED the document body on the way out: the tool "
        "returned it and the client would not see it. A schema may describe the response; "
        "it must never be what serializes it")
    assert set(payload) <= set(out), (
        f"serialization dropped keys: {sorted(set(payload) - set(out))}")


def test_a_null_authoritative_source_survives(sourceless_server):
    """Bug 1. A corpus declaring no source emits `authoritative_source: null` by design.
    Every object tool must serialize that, not reject it."""
    tools = _tools(sourceless_server)
    for name in OBJECT_TOOLS:
        if name not in tools:
            continue
        payload = {"corpus": "t", "archetype": "document", "authoritative_source": None,
                   "detail": "tool-specific payload"}
        out = _serialize(tools[name], payload)  # must not raise
        assert "authoritative_source" in out and out["authoritative_source"] is None, (
            f"{name} did not round-trip a null authoritative_source; convention 1 "
            f"documents null as the value for a corpus that declares no source")


def test_corpus_overview_serializes_end_to_end(sourceless_server):
    """The exact call that was erroring on all four corpora, all the way through."""
    tools = _tools(sourceless_server)
    payload = _call(sourceless_server, "corpus_overview", {})
    out = _serialize(tools["corpus_overview"], payload)

    for field in CONVENTION_1:
        assert field in out, f"corpus_overview response lost {field}"
    assert out["authoritative_source"] is None
    assert "config_warning" in out, (
        "a corpus with no declared source must say so; the warning is convention 1's "
        "own remedy and it is a tool-specific key, so it is exactly what a payload-owning "
        "schema would silently drop")
    assert len(out) > len(CONVENTION_1), (
        "response carries only the declared fields — the schema constrained the payload")
