"""What a CLIENT receives, for EVERY registered tool — not just the object-shaped six.

corpus-toolkit#63. `_sdk.call_tool` passes `convert_result=False`, and that is deliberate:
the release gate asserts that an external graph neighbour comes back
`{citation, external: true}` and that a document body contains its text, and asserting
those through the SDK's marshalling would test the SDK instead of the toolkit. The
reasoning is sound and this file does not flip that flag. The gap it left is that nothing
asserted the marshalling EITHER, and v1.24.0 walked straight through the gap: a declared
output schema dropped every document body on the way out, the call still reported success,
and CI stayed green — including `corpus-end-to-end`, which builds a real corpus and calls
real tools, with `convert_result=False` (corpus-toolkit#61).

So the two legs are kept separate rather than merged. `call_tool` still answers "did the
toolkit compute the right thing"; this file answers "did the right thing survive the trip
to the client", by taking each tool's REAL answer from a real corpus on disk and pushing it
through the SAME conversion the SDK builds a response with. A failure in one says which of
the two broke, which a single merged assertion could not.

WHY A ROUND TRIP AND NOT A SCHEMA ASSERTION. The v1.24.0 change shipped WITH tests. They
measured `additionalProperties` on the emitted schema — absent, so extras validate — and
concluded extras were safe. Wrong layer: extras clear validation and are then DISCARDED by
the model that does the serializing. No amount of schema inspection can see that; only a
payload going in one end and being compared at the other can.

RELATION TO tests/test_output_schemas.py, which #61 produced and which round-trips too.
That file pins response convention 1 — the three envelope fields, and a NULL
`authoritative_source` — across the six object-shaped tools, mostly from synthetic
payloads, and deliberately excludes `search_corpus` because convention 1 is about
object-shaped responses. This file is the other axis and does not restate convention 1:
every REGISTERED tool, whatever its shape, asserted as WHOLE-PAYLOAD equality rather than
a field list. That covers three things nothing reached before —

  * `search_corpus`, whose list answer takes a different conversion path entirely: one
    content block PER HIT, plus a structured half wrapped in `{"result": [...]}`;
  * the content-block half of the response at all — the six-tool file reads only
    structured content, and a client renders both;
  * the `tools_module` extension tools a hybrid or api corpus registers, which are
    corpus-supplied and were covered by nothing.

— and it fails on a key dropped ANYWHERE, nested or per-hit, rather than only on the four
fields someone thought to list.
"""
import asyncio
import json
import textwrap

import pytest

pytest.importorskip("mcp", reason="needs the mcp extra: pip install -e '.[mcp,test]'")

from corpus_toolkit import config as config_mod              # noqa: E402
from corpus_toolkit.mcp import _sdk                          # noqa: E402
from corpus_toolkit.mcp.server import build_server           # noqa: E402

CORPUS_YML = """\
schema_version: 1
corpus:
  id: "t"
  name: "T"
  jurisdiction: "oregon"
  archetype: "{archetype}"
  authoritative_source: "https://example.invalid/official"
  schema_version: 1
  contract_version: 1
content_roots:
  - path: "docs"
    doc_type: doc
plugins:
  issuing_body_registry: _meta/registry.yml
{plugins}
disclaimer_marker: "NON-AUTHORITATIVE"
"""

# Declared by BOTH fixtures so `issuing_body_profile` is actually registered. It is one of
# the six tools v1.24.0 annotated, its registration is config-gated, and before this it was
# round-tripped nowhere on the platform: this file listed it in CALLS, test_output_schemas.py
# skips it when it is absent, and the release gate's corpus declares no registry either. A
# tool that appears in a coverage table and is served by no fixture is worse than an
# admitted gap, and #15 is going to annotate that tool.
REGISTRY = """\
entries:
  - slug: archives-division
    name: Archives Division
"""

DOC = """\
---
schema_version: 1
corpus: t
id: {doc_id}
title: {title}
doc_type: doc
citation: "Schedule {n}"
source_url: "https://example.invalid/{doc_id}"
relationships:
  references_external: ["OAR 166-300-0015"]
---

NON-AUTHORITATIVE test document.

{marker}
"""

# Two documents, both matching the search query, because one hit cannot tell "a block per
# hit" apart from "a block for the whole list" — and the per-hit path is precisely the one
# with no coverage.
DOCS = {
    "alpha": ("Alpha correspondence", "Body text of alpha, which get_document must return."),
    "beta": ("Beta correspondence", "Body text of beta, a second searchable document."),
}
QUERY = "correspondence"

# A corpus-supplied extension tool surface, in the three shapes a corpus actually writes.
# The payloads carry what a payload-owning schema eats: keys no shared convention knows
# about, a None, and nesting.
#
# `list_datasets` is annotated BARE `-> dict` on purpose — that is verbatim how
# oregon-legislature and oregon-budget write theirs, and a bare annotation makes the SDK
# emit no output schema and therefore no structured content on either major, so the whole
# answer travels as a content block. Covered here rather than tidied away, because the
# assertions have to hold for the surface the live corpora serve, not a nicer one. That
# these tools ship no structured half at all is its own finding — corpus-toolkit#96.
EXT_TOOLS = '''
from typing import Any


def register(mcp, framework):
    @mcp.tool()
    def list_datasets() -> dict:
        """Fixture: the extension surface as the live hybrid corpora annotate it."""
        return {"corpus": "t", "archetype": "hybrid",
                "authoritative_source": None,
                "datasets": [{"key": "budget", "rows": 3, "note": None}],
                "corpus_specific_key": {"nested": ["a", "b"]}}

    @mcp.tool()
    def query_dataset(key: str, limit: int = 10) -> list[dict]:
        """Fixture: a list-shaped extension tool."""
        return [{"key": key, "row": n, "value": None} for n in range(limit)]

    @mcp.tool()
    def join_lookup(document_id: str) -> dict[str, Any]:
        """Fixture: an extension tool annotated the way the built-ins are, so it DOES
        declare an output schema and does serialize a structured half."""
        return {"corpus": "t", "archetype": "hybrid", "authoritative_source": None,
                "document_id": document_id,
                "rows": [{"key": "budget", "amount": 1, "note": None}],
                "corpus_specific_key": {"nested": ["a", "b"]}}
'''

# name -> arguments. EVERY registered tool must appear here; `test_every_registered_tool_is
# _covered` fails when one does not, so a tool added later cannot quietly arrive with no
# marshalling coverage — which is how `search_corpus` and the extension tools got here.
CALLS = {
    "corpus_overview": {},
    "search_corpus": {"query": QUERY},
    "get_document": {"doc_id": "alpha"},
    "resolve_citation": {"citation": "Schedule 1"},
    "graph_neighbors": {"doc_id": "alpha"},
    "authority_chain": {"doc_id": "alpha"},
    "issuing_body_profile": {"slug_or_query": "archives-division"},
    "list_datasets": {},
    "query_dataset": {"key": "budget", "limit": 2},
    "join_lookup": {"document_id": "alpha"},
}

MANDATORY_CORE_TOOLS = {"corpus_overview", "search_corpus", "get_document",
                        "resolve_citation", "graph_neighbors"}

# THE ERROR BRANCHES, round-tripped as their own table (corpus-toolkit#15 review).
#
# CALLS above is happy-path only, and that was a real hole rather than a tidy one: response
# convention 1 applies to errors precisely BECAUSE "an error is the response an agent is most
# likely to misread" (docs/mcp-interface-contract.md), and an error shape is where the
# envelope and a handful of tool-specific keys are the WHOLE payload — so a declaration that
# ate extras would leave an error response that is nothing but three fields and still reads
# as a successful call. That is #61's shape with less to notice.
#
# `graph_neighbors('alpha')` in CALLS happens to hit `no_graph` already, because the fixture
# writes no `_meta/graph.json`. Incidental coverage is not coverage: nothing said so, and a
# fixture that later grew a graph would have removed it silently. Listed here deliberately.
#
# `get_document` on an unknown id is the branch corpus-toolkit#102 is about — the one place
# a backend record is merged OVER the envelope with no re-assertion — so it is the branch
# whose marshalling most wants pinning before that fix lands.
ERROR_CALLS = {
    "get_document": {"doc_id": "no-such-document"},
    "resolve_citation": {"citation": "not a citation in any registered scheme"},
    "graph_neighbors": {"doc_id": "no-such-document"},
    "authority_chain": {"doc_id": "no-such-document"},
    "issuing_body_profile": {"slug_or_query": "no-such-issuing-body"},
}


@pytest.fixture(autouse=True)
def _isolate_imports():
    """Undo `load_attr`'s sys.path / sys.modules side effects, exactly as
    test_tools_module.py does and for the same reason: `src` is cached as a package whose
    `__path__` freezes to whichever tmp_path imported it first, so a later test silently
    loads the wrong module. Production loads one corpus per process and never hits this."""
    import sys
    before_path = list(sys.path)
    before_mods = set(sys.modules)
    yield
    sys.path[:] = before_path
    for name in set(sys.modules) - before_mods:
        if name == "src" or name.startswith("src."):
            del sys.modules[name]


def _build(root, *, archetype="document", module=None):
    """A real corpus on disk, under its OWN directory — one test uses both fixtures, and a
    shared root would have them overwrite each other's corpus.yml."""
    plugins = f'  tools_module: "src.{module}:register"\n' if module else ""
    (root / "_meta").mkdir(parents=True)
    (root / "_meta" / "corpus.yml").write_text(
        CORPUS_YML.format(archetype=archetype, plugins=plugins))
    (root / "_meta" / "registry.yml").write_text(REGISTRY)
    (root / "docs").mkdir()
    for n, (doc_id, (title, marker)) in enumerate(DOCS.items(), 1):
        (root / "docs" / f"{doc_id}.md").write_text(
            DOC.format(doc_id=doc_id, title=title, n=n, marker=marker))
    if module:
        pkg = root / "src"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / f"{module}.py").write_text(textwrap.dedent(EXT_TOOLS))
    return build_server(config_mod.load(str(root / "_meta" / "corpus.yml")))


@pytest.fixture
def document_server(tmp_path):
    return _build(tmp_path / "document-corpus")


@pytest.fixture
def hybrid_server(tmp_path):
    """A hybrid corpus with its own tools. `module` is unique to this file because
    `load_attr` caches by module name and two corpora sharing one would silently share
    the first one's code — test_tools_module.py's own note."""
    return _build(tmp_path / "hybrid-corpus", archetype="hybrid",
                  module="marshalling_ext_tools")


def _tools(server):
    """Through the seam, not `server._tool_manager`: what that private attribute yields is
    one of the things the 1.x/2.x break moved, and keeping the reach in `_sdk` is the whole
    point of the file."""
    return _sdk.tools_by_name(server)


def _call(server, name, arguments):
    """The tool's raw answer — `convert_result=False`, the behaviour leg."""
    return asyncio.new_event_loop().run_until_complete(
        _sdk.call_tool(server, name, arguments))


def _jsonable(value):
    """Compare like a client sees it: the wire is JSON, so tuple-vs-list and other
    encoder-level equivalences are not differences. A DROPPED KEY still is."""
    return json.loads(json.dumps(value, default=str))


def _expected_structured(raw):
    """What `structured_content` must carry for a raw answer of this shape.

    An object-shaped tool's answer travels as itself; a list-shaped one is wrapped by the
    SDK under `result`, because JSON Schema's top level must be an object. Asserting the
    wrapper explicitly rather than reaching past it keeps this test honest about the shape
    a client actually parses."""
    return {"result": _jsonable(raw)} if isinstance(raw, list) else _jsonable(raw)


def _expected_blocks(raw):
    """What the content blocks must carry: ONE block per list item, one block for an
    object. Measured on both majors rather than assumed (mcp 1.28.1 and 2.0.0 agree)."""
    return _jsonable(raw) if isinstance(raw, list) else [_jsonable(raw)]


def _decoded_blocks(texts, name):
    decoded = []
    for text in texts:
        try:
            decoded.append(json.loads(text))
        except ValueError:
            raise AssertionError(
                f"{name}: a content block is not the JSON its payload was — a client "
                f"parsing the block gets nothing back: {text!r:.200}")
    return decoded


def test_every_registered_tool_is_covered(document_server, hybrid_server):
    """The coverage guard, and the reason #63 exists rather than #61 having been enough:
    the hole was never in the tools someone remembered to list."""
    registered = _sdk.tool_names(document_server) | _sdk.tool_names(hybrid_server)
    assert MANDATORY_CORE_TOOLS <= registered, (
        f"fixture corpus is not serving the mandatory core tools: "
        f"{sorted(MANDATORY_CORE_TOOLS - registered)} — the assertions below would pass "
        f"while checking almost nothing")
    uncovered = sorted(registered - set(CALLS))
    assert not uncovered, (
        f"tool(s) with no serialization coverage: {uncovered}. Add each to CALLS with "
        f"arguments that produce a real answer. A tool whose result is never round-tripped "
        f"is a tool that can start returning an empty envelope and still report success "
        f"(corpus-toolkit#61).")
    # AND THE OTHER DIRECTION, which is how `issuing_body_profile` sat in this table
    # covering nothing: neither fixture declared an `issuing_body_registry`, so the tool
    # never registered, the per-tool loop skipped it as archetype-gated, and the table read
    # complete. A one-directional guard cannot see its own blind spot.
    unregistered = sorted(set(CALLS) - registered)
    assert not unregistered, (
        f"CALLS names tool(s) no fixture registers: {unregistered}. They are round-tripped "
        f"nowhere while appearing to be covered — configure a fixture that serves them, or "
        f"remove the entry and say where they are covered instead.")


@pytest.mark.parametrize("fixture", ["document_server", "hybrid_server"])
def test_every_tool_answer_reaches_the_client_intact(fixture, request):
    """THE regression test for v1.24.0. Every registered tool's real answer, serialized
    the way a response is built, must come out equal to what went in — in BOTH halves of
    the response, blocks and structured content.

    Whole-payload equality rather than a field list, deliberately. v1.24.0's own tests
    checked the fields someone listed; what broke was everything they did not
    (`get_document` returned three envelope fields and no body). Equality has no such
    blind spot and needs no maintenance when a tool grows a key.

    A tool that DECLARES NO OUTPUT SCHEMA has no structured half at all — that is not a
    failure here, it is the shape a bare `-> dict` annotation produces on both majors
    (#96). Its answer must still arrive whole in the blocks. The exemption is keyed on the
    declaration and never on the conversion coming back empty: a tool that declares a
    schema and then serializes nothing is the #61 failure in its purest form, and keying on
    the result would file it under "expected"."""
    server = request.getfixturevalue(fixture)
    tools = _tools(server)
    problems = []
    for name, arguments in CALLS.items():
        if name not in tools:
            continue                       # archetype-gated or corpus-supplied
        raw = _call(server, name, arguments)
        try:
            texts, structured = _sdk.serialized_result(tools[name], raw)
        except Exception as e:             # noqa: BLE001
            # A ValidationError here is bug 1 of #61 verbatim: the declared shape refusing
            # a value the toolkit documents (`authoritative_source: null`). Collect rather
            # than raise, so one rejecting tool does not hide the others.
            problems.append(f"{name}{arguments} could not be serialized at all: "
                            f"{type(e).__name__}: {e}")
            continue

        blocks = _decoded_blocks(texts, name)
        if blocks != _expected_blocks(raw):
            problems.append(
                f"{name}{arguments}: the CONTENT BLOCKS a client renders are not the "
                f"answer the tool returned"
                f"\n      returned: {json.dumps(_expected_blocks(raw), sort_keys=True)[:400]}"
                f"\n      received: {json.dumps(blocks, sort_keys=True)[:400]}")

        declared = getattr(tools[name], "output_schema", None)
        if structured is None:
            if declared is None:
                continue                   # #96: no schema declared, blocks are the answer
            problems.append(
                f"{name}{arguments}: declares an output schema ({json.dumps(declared)[:120]}"
                f") and serialized NO structured content — the half a schema-driven client "
                f"parses is simply absent, with the call still reporting success")
            continue
        want = _expected_structured(raw)
        if structured != want:
            lost = sorted(set(want) - set(structured))
            problems.append(
                f"{name}{arguments}: the STRUCTURED content a client parses is not the "
                f"answer the tool returned"
                + (f"; keys dropped at serialization: {lost}" if lost else "")
                + f"\n      returned: {json.dumps(want, sort_keys=True)[:400]}"
                + f"\n      received: "
                + json.dumps(structured, sort_keys=True, default=str)[:400])
    assert not problems, "\n    " + "\n    ".join(problems)


def test_every_error_branch_reaches_the_client_intact(document_server):
    """The same whole-payload equality, on the shapes an agent is most likely to misread.

    Errors carry the envelope BY CONTRACT (response convention 1, "errors included"), and
    they are the responses with the least left over once the envelope is removed — so a
    declaration that dropped extras would turn `no_graph` into three fields that say
    nothing went wrong. Convention 5 is what is really at stake: `no_graph`, `not_in_graph`,
    `unresolved` and `sibling_unavailable` exist to keep "could not check" apart from "is
    not there", and every one of them travels in a key no shared schema declares.

    Asserted per branch rather than pooled, so a failure names the tool AND the arguments
    that produced it."""
    tools = _tools(document_server)
    problems = []
    for name, arguments in ERROR_CALLS.items():
        assert name in tools, f"fixture does not serve {name}; this table is not testing it"
        raw = _call(document_server, name, arguments)
        assert set(raw) - {"corpus", "archetype", "authoritative_source"}, (
            f"{name}{arguments} returned only the envelope — the fixture is not reaching "
            f"an error branch, so the assertions below prove nothing")
        try:
            texts, structured = _sdk.serialized_result(tools[name], raw)
        except Exception as e:                 # noqa: BLE001
            problems.append(f"{name}{arguments} could not be serialized at all: "
                            f"{type(e).__name__}: {e}")
            continue
        if _decoded_blocks(texts, name) != _expected_blocks(raw):
            problems.append(f"{name}{arguments}: the CONTENT BLOCKS a client renders are "
                            f"not the error the tool returned")
        want = _expected_structured(raw)
        if structured != want:
            lost = sorted(set(want) - set(structured))
            problems.append(
                f"{name}{arguments}: the STRUCTURED content a client parses is not the "
                f"error the tool returned"
                + (f"; keys dropped at serialization: {lost}" if lost else "")
                + f"\n      returned: {json.dumps(want, sort_keys=True)[:400]}"
                + f"\n      received: "
                + json.dumps(structured, sort_keys=True, default=str)[:400])
    assert not problems, "\n    " + "\n    ".join(problems)


def test_the_document_body_survives_in_both_halves(document_server):
    """A client renders content blocks and parses structured content. #61 emptied both and
    reported success; test_output_schemas.py reads only the structured half, so the blocks
    are asserted here."""
    tools = _tools(document_server)
    marker = DOCS["alpha"][1]
    raw = _call(document_server, "get_document", {"doc_id": "alpha"})
    assert marker in json.dumps(raw), (
        "the tool itself lost the body — a framework bug, not a marshalling one")

    texts, structured = _sdk.serialized_result(tools["get_document"], raw)
    assert marker in json.dumps(structured), (
        "structured content reached the client without the document body")
    assert any(marker in t for t in texts), (
        f"no content block carried the document body; a client rendering blocks would "
        f"show an envelope and nothing else. Blocks: {texts!r:.400}")


def test_search_hits_arrive_one_content_block_each(document_server):
    """The list path, which nothing exercised (#63, and #58 left it alone).

    `search_corpus` is not object-shaped, so it is out of scope for convention 1 and out of
    scope for test_output_schemas.py — and it converts through a different branch: one
    content block per hit, and a `{"result": [...]}` wrapper on the structured half. A
    return-annotation change here would drop or merge hits with nothing to notice."""
    tools = _tools(document_server)
    hits = _call(document_server, "search_corpus", {"query": QUERY})
    ids = sorted(h["id"] for h in hits)
    assert ids == sorted(DOCS), (
        f"fixture search returned {ids} — expected every document, so the per-hit "
        f"assertions below are testing more than one block")

    texts, structured = _sdk.serialized_result(tools["search_corpus"], hits)
    assert len(texts) == len(hits), (
        f"{len(hits)} hits converted to {len(texts)} content block(s); a client rendering "
        f"blocks would see the wrong number of results")
    for hit in hits:
        assert any(hit["id"] in t for t in texts), (
            f"hit {hit['id']!r} reached no content block")
    assert structured == {"result": _jsonable(hits)}, (
        "the structured half of a search response is not the hit list the tool returned")


def test_a_null_value_inside_a_corpus_supplied_payload_survives(hybrid_server):
    """Bug 1 of #61 on the surface nothing covered. Convention 1's `authoritative_source:
    null` is pinned for the built-ins by test_output_schemas.py; extension tools are
    written by a corpus, carry their own nulls and their own keys, and go through the same
    converter. `join_lookup` is the fixture's schema-declaring extension tool, so this
    asserts on the structured half a payload-owning schema would have eaten."""
    tools = _tools(hybrid_server)
    raw = _call(hybrid_server, "join_lookup", {"document_id": "alpha"})
    structured = _sdk.structured_result(tools["join_lookup"], raw)
    assert structured["authoritative_source"] is None
    assert structured["rows"][0]["note"] is None, (
        "a null nested inside a corpus-supplied payload did not survive serialization")
    assert structured["corpus_specific_key"] == {"nested": ["a", "b"]}, (
        "a corpus-specific key was dropped or flattened on the way to the client")
