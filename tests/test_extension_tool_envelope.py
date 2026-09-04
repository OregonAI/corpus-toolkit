"""An extension tool can satisfy response convention 1, and the toolkit gives it the means.

corpus-toolkit#96. Convention 1 requires `corpus`, `archetype` and `authoritative_source` on
every object-shaped response and exempts exactly one tool — `search_corpus`, on the grounds
that it is list-shaped. Extension tools registered through `plugins.tools_module` return
objects and were therefore in scope and simply non-conformant: every one on the platform is
annotated bare `-> dict`, which makes the SDK emit no output schema and no structured content
at all. A client reading structured content gets nothing from a corpus's own tools while
getting a parsed object from every built-in.

The two facts are one problem. The toolkit offered extension tools no supported way to
satisfy the convention: `CorpusFramework` assembles the envelope in a single PRIVATE method,
and while the registration hook hands the framework to the corpus, reaching into `_envelope`
is not an interface anyone should be asked to depend on.

WHY NOT `-> dict[str, Any]`, which would also produce a schema and a structured half: it
declares no fields, so convention 1 stays unsatisfiable by field-level validation — the
defect corpus-toolkit#15 closed for the built-ins. Taking that route instead would require
amending convention 1 with an explicit extension-tool exemption, argued the way
`search_corpus`'s is. That is a maintainer decision and it was made the other way.

THE MEASURED FACT THIS FILE EXISTS TO PIN: annotating a live extension tool
`-> ResponseEnvelope` WITHOUT adding the envelope to its payload is not a degraded response,
it is a hard ToolError — the three fields are required with no defaults. So the annotation
and the payload change together or not at all, and a fixture that carries the envelope while
claiming to mirror tools that do not would turn the suite green over exactly that break.
"""
import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="needs the mcp extra: pip install -e '.[mcp,test]'")

sys.path.insert(0, str(Path(__file__).parent))
from test_tools_module import _corpus, _isolate_imports          # noqa: E402,F401

from corpus_toolkit.mcp import sdk                              # noqa: E402
from corpus_toolkit.mcp.framework import CorpusFramework         # noqa: E402
from corpus_toolkit.mcp.server import build_server               # noqa: E402


def _tool(mcp, name):
    return sdk.tools_by_name(mcp)[name]


def _structured(mcp, name, arguments=None):
    """The structured half a client receives, on either SDK major.

    Through `sdk.structured_result` — the PUBLIC seam the rest of the suite uses —
    rather than `mcp.call_tool` directly: 1.x returns `(blocks, structured)` and 2.x a
    `CallToolResult`, and hand-rolling that difference in a test is how a suite ends up
    green on one leg of the matrix and red on the other, which is exactly what the first
    draft of this file did.
    """
    payload = asyncio.new_event_loop().run_until_complete(
        sdk.call_tool(mcp, name, arguments or {}))
    return sdk.structured_result(_tool(mcp, name), payload)


# ---------- the accessor ----------

def test_the_public_envelope_matches_the_one_the_builtins_use(tmp_path):
    """One assembly point, reachable by name.

    A corpus reaching `framework._envelope()` would be depending on a private method; a
    corpus hand-rolling the three keys is the divergence that method was created to stop,
    now spread across repo boundaries where it is harder to see. So the accessor is public
    AND is asserted to return exactly what the built-in path returns — a second
    implementation that happened to agree today would drift tomorrow."""
    fw = CorpusFramework(_corpus(tmp_path))

    assert fw.response_envelope() == fw._envelope()
    assert set(fw.response_envelope()) == {"corpus", "archetype", "authoritative_source"}


def test_the_public_envelope_is_a_copy_even_if_the_assembly_point_starts_caching(tmp_path):
    """Written to fail if `_envelope` is ever memoized.

    Asserting only that two calls disagree after mutation CANNOT FAIL today: `_envelope`
    builds a fresh dict literal every call, so the defensive copy protects nothing yet and
    the guard passed with the copy removed. In a suite whose own history is "a sweep that
    cannot fail is worse than no sweep", that is not a guard. This pins the property that
    actually matters — the caller never receives the framework's own object — against the
    change that would make it matter."""
    fw = CorpusFramework(_corpus(tmp_path))
    cached = fw._envelope()
    fw._envelope = lambda: cached          # simulate a memoized assembly point

    handed = fw.response_envelope()
    handed["corpus"] = "something-else"

    assert fw.response_envelope()["corpus"] == "t"
    assert cached["corpus"] == "t"


# ---------- what the wire actually carries ----------

CONFORMING = '''
    from corpus_toolkit.mcp.responses import ResponseEnvelope

    def register(mcp, framework):
        @mcp.tool()
        def list_datasets() -> ResponseEnvelope:
            """An extension tool that satisfies convention 1."""
            return framework.with_envelope({"datasets": [{"key": "budget", "rows": 3}]})
'''

NON_CONFORMING = '''
    from corpus_toolkit.mcp.responses import ResponseEnvelope

    def register(mcp, framework):
        @mcp.tool()
        def list_datasets() -> ResponseEnvelope:
            """Annotated but NOT carrying the envelope — the half-migration."""
            return {"datasets": [{"key": "budget", "rows": 3}]}
'''

BARE = '''
    def register(mcp, framework):
        @mcp.tool()
        def list_datasets() -> dict:
            """The surface every live extension tool ships today."""
            return {"datasets": [{"key": "budget", "rows": 3}]}
'''


def test_a_bare_dict_extension_tool_declares_nothing(tmp_path):
    """The baseline this issue reports, and the control for the assertions below. A bare
    `dict` annotation makes the SDK emit no output schema at all, and with no schema there
    is no structured content — the answer travels only as a JSON text block."""
    mcp = build_server(_corpus(tmp_path, archetype="hybrid", tools_py=BARE,
                               module="bare_tools"))

    assert _tool(mcp, "list_datasets").output_schema is None


def test_a_conforming_extension_tool_declares_the_three_fields(tmp_path):
    """The payoff: a corpus's own tool answers in the same wire format as the built-ins."""
    mcp = build_server(_corpus(tmp_path, archetype="hybrid", tools_py=CONFORMING,
                               module="conforming_tools"))

    schema = _tool(mcp, "list_datasets").output_schema

    assert schema is not None
    assert set(schema.get("properties", {})) >= {
        "corpus", "archetype", "authoritative_source"}
    assert set(schema.get("required", [])) >= {
        "corpus", "archetype", "authoritative_source"}


def test_a_conforming_extension_tool_serializes_a_structured_half(tmp_path):
    """A declared schema is only half the point; the client must actually receive the
    object."""
    mcp = build_server(_corpus(tmp_path, archetype="hybrid", tools_py=CONFORMING,
                               module="conforming_call"))

    structured = _structured(mcp, "list_datasets")

    assert structured is not None, "no structured content — the whole point of the schema"
    assert structured["corpus"] == "t"
    assert structured["datasets"] == [{"key": "budget", "rows": 3}]


def test_annotating_without_the_envelope_is_a_hard_error_not_a_degraded_response(tmp_path):
    """THE MEASURED FACT, pinned. The three fields are required with no defaults, so a
    half-migration — annotation changed, payload untouched — does not answer weakly. It
    stops answering.

    This is why the fixture in tests/test_result_marshalling.py had to be made faithful: it
    carried the envelope while claiming to mirror tools that do not, so a sweep that
    annotated the live corpora would have gone green here and broken oregon-legislature and
    oregon-budget on deploy."""
    mcp = build_server(_corpus(tmp_path, archetype="hybrid", tools_py=NON_CONFORMING,
                               module="non_conforming"))

    with pytest.raises(Exception) as e:
        _structured(mcp, "list_datasets")

    message = str(e.value)
    assert "corpus" in message and "archetype" in message
    assert "authoritative_source" in message


# ---------- precedence: the envelope wins, and the corpus does not have to remember ----------

DISPLACING = '''
    from corpus_toolkit.mcp.responses import ResponseEnvelope

    def register(mcp, framework):
        @mcp.tool()
        def join_lookup(document_id: str = "") -> ResponseEnvelope:
            """A join to a SIBLING corpus. `corpus` is the natural key for that, and
            oregon-budget really does ship a join_lookup."""
            return framework.with_envelope({
                "corpus": "oregon-legislature",
                "rows": [{"measure": "HB 2049"}]})
'''


def test_a_corpus_payload_cannot_displace_the_envelope(tmp_path):
    """The rule the toolkit enforces on ITSELF, extended to the tools corpora write.

    corpus-toolkit#102/#104 established that a mapping the framework does not control never
    displaces the envelope, and every built-in puts the assembled front LAST for exactly
    that reason. The first draft of this feature blessed the opposite for extension tools —
    `{**framework.response_envelope(), **payload}` — which reinstated the same class across
    a repo boundary, where it is harder to see.

    Measured: a `join_lookup` carrying `corpus: "oregon-legislature"` served
    `corpus='oregon-legislature'` for a corpus whose id is `t`. That is #38 reopened from
    the extension side, and a list-valued `corpus` stops the tool answering at all.

    `with_envelope` merges the envelope OVER the payload, so the precedence is not each
    corpus's to remember. A rule every corpus author must remember is a rule that will be
    forgotten."""
    mcp = build_server(_corpus(tmp_path, archetype="hybrid", tools_py=DISPLACING,
                               module="displacing"))

    structured = _structured(mcp, "join_lookup", {"document_id": "x"})

    assert structured["corpus"] == "t"                    # config's value, not the payload's
    assert structured["rows"] == [{"measure": "HB 2049"}]  # ...and the payload survives


def test_with_envelope_carries_the_same_values_as_the_builtin_path(tmp_path):
    """One assembly point, still."""
    fw = CorpusFramework(_corpus(tmp_path))

    merged = fw.with_envelope({"datasets": []})

    assert {k: merged[k] for k in fw._envelope()} == fw._envelope()
    assert merged["datasets"] == []
