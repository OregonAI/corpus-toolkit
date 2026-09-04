"""`plugins.tools_module` lets a corpus register its own MCP tools.

The seven built-in tools were a closed set, which forced corpora needing anything else to
smuggle it through `get_document` enrichment. That works for attaching data to a document
and is useless for a tool keyed on something that is not a document id — a dataset key, a
join — which is exactly what `docs/mcp-interface-contract.md` already promises for hybrid
corpora (`list_datasets`, `query_dataset`, `join_lookup`).

The behaviour worth pinning down is the failure mode, not the happy path. A server that
catches a bad `tools_module` and starts anyway looks completely healthy: every built-in
answers correctly, and the caller cannot distinguish "this corpus has no join_lookup" from
"join_lookup failed to load". Refusing to start is the only signal that reaches anyone.
"""
import textwrap

import pytest

pytest.importorskip("mcp", reason="needs the mcp extra: pip install -e '.[mcp,test]'")

from corpus_toolkit import config as config_mod              # noqa: E402
from corpus_toolkit.mcp.server import build_server           # noqa: E402

CORPUS_YML = """\
corpus:
  id: "t"
  name: "T"
  jurisdiction: "oregon"
  archetype: "{archetype}"
  schema_version: 1
  contract_version: 1
content_roots:
  - path: "docs"
    doc_type: doc
{plugins}
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

BUILTINS = {"search_corpus", "get_document", "resolve_citation", "graph_neighbors",
            "corpus_overview"}


@pytest.fixture(autouse=True)
def _isolate_imports():
    """Undo `load_attr`'s sys.path/sys.modules side effects between tests.

    `load_attr` inserts the corpus root on sys.path and imports through the normal
    machinery, so `src` is cached as a package with `__path__` frozen to whichever
    tmp_path imported it first. Every later test then resolves `src.<anything>` against
    that first directory and fails — or worse, silently loads the wrong module and passes.

    This does NOT paper over a production bug: `corpus-mcp-serve` loads one corpus per
    process. It exists so the suite tests the seam rather than Python's import cache.
    """
    import sys
    before_path = list(sys.path)
    before_mods = set(sys.modules)
    yield
    sys.path[:] = before_path
    for name in set(sys.modules) - before_mods:
        if name == "src" or name.startswith("src."):
            del sys.modules[name]


def _corpus(tmp_path, archetype="document", tools_py=None, module=None, attr="register"):
    """Build a throwaway corpus, optionally with a tools module.

    `module` must be UNIQUE PER TEST. `load_attr` goes through
    `importlib.import_module`, which caches by module name, so two tests both using
    `src.corpus_tools` would silently share the first one's module and the second test
    would assert against code it never loaded. This does not arise in production —
    `corpus-mcp-serve` serves one corpus per process — but it makes a test suite that
    appears to exercise the seam actually exercise Python's import cache.
    """
    (tmp_path / "_meta").mkdir()
    plugins = f'plugins:\n  tools_module: "src.{module}:{attr}"\n' if module else ""
    (tmp_path / "_meta/corpus.yml").write_text(
        CORPUS_YML.format(archetype=archetype, plugins=plugins))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/alpha.md").write_text(DOC)
    if tools_py is not None:
        pkg = tmp_path / "src"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / f"{module}.py").write_text(textwrap.dedent(tools_py))
    return config_mod.load(str(tmp_path / "_meta/corpus.yml"))


def _names(mcp):
    return {t.name for t in mcp._tool_manager.list_tools()}


def test_absent_key_changes_nothing(tmp_path):
    """Every corpus that does not set the key must behave exactly as before."""
    config = _corpus(tmp_path)
    assert config.tools_module is None
    assert BUILTINS <= _names(build_server(config))


def test_corpus_tool_is_registered(tmp_path):
    config = _corpus(
        tmp_path,
        module="tools_registered",
        tools_py='''
        def register(mcp, framework):
            @mcp.tool()
            def query_dataset(key: str) -> dict:
                """Corpus-specific tool."""
                return {"key": key}
        ''')
    names = _names(build_server(config))
    assert "query_dataset" in names
    # Registering last must ADD to the built-ins, never replace them.
    assert BUILTINS <= names


def test_framework_is_passed_not_just_the_server(tmp_path):
    """register(mcp, framework) — a corpus tool that could not reach the framework would
    have to re-implement retrieval to answer anything."""
    config = _corpus(
        tmp_path,
        module="tools_framework",
        tools_py='''
        def register(mcp, framework):
            assert hasattr(framework, "search_corpus"), framework
            assert hasattr(framework, "backend"), framework
            @mcp.tool()
            def uses_framework() -> int:
                """Reaches the framework."""
                return len(framework.corpus_overview()["documents_by_type"])
        ''')
    assert "uses_framework" in _names(build_server(config))


def test_import_failure_is_fatal(tmp_path):
    """THE REGRESSION THIS FILE EXISTS FOR. Starting anyway would yield a server that
    passes every built-in call while silently missing the corpus's own tools."""
    config = _corpus(tmp_path, module="no_such_module")
    with pytest.raises(ModuleNotFoundError):
        build_server(config)


def test_raising_registrar_is_fatal(tmp_path):
    config = _corpus(
        tmp_path,
        module="tools_raises",
        tools_py='''
        def register(mcp, framework):
            raise ValueError("boom")
        ''')
    with pytest.raises(ValueError, match="boom"):
        build_server(config)


def test_registering_nothing_is_an_error(tmp_path):
    """Declaring the hook and adding no tools is a mistake that would otherwise be
    indistinguishable from a working corpus with no extra tools."""
    config = _corpus(
        tmp_path,
        module="tools_empty",
        tools_py='''
        def register(mcp, framework):
            pass
        ''')
    with pytest.raises(RuntimeError, match="registered no tools"):
        build_server(config)


def test_works_for_a_hybrid_corpus(tmp_path):
    """The archetype this seam was added for. `authority_chain` is document/hybrid-only,
    so this also pins that corpus tools coexist with archetype-gated built-ins."""
    config = _corpus(
        tmp_path, archetype="hybrid", module="tools_hybrid",
        tools_py='''
        def register(mcp, framework):
            @mcp.tool()
            def list_datasets() -> list:
                """Datasets this corpus proxies."""
                return []
        ''')
    names = _names(build_server(config))
    assert "list_datasets" in names
    assert "authority_chain" in names


# ---------- name collisions with a built-in (corpus-toolkit#111) ----------

SHADOWS_A_BUILTIN = '''
    def register(mcp, framework):
        @mcp.tool()
        def corpus_overview() -> dict:
            """A corpus tool whose name collides with a built-in."""
            return {"mine": True}

        @mcp.tool()
        def join_lookup(document_id: str = "") -> dict:
            return {"rows": []}
'''

ALL_COLLIDE = '''
    def register(mcp, framework):
        @mcp.tool()
        def corpus_overview() -> dict:
            return {}

        @mcp.tool()
        def get_document(doc_id: str = "") -> dict:
            return {}
'''

REGISTERS_NOTHING = '''
    def register(mcp, framework):
        pass
'''


def test_a_tool_colliding_with_a_builtin_refuses_to_start(tmp_path):
    """corpus-toolkit#111. The SDK keeps the EXISTING tool on a duplicate name, so the
    built-in wins and the corpus's version never exists. Nothing said so: the summary
    infers what was added by set difference, and a shadowed name was already present
    before the hook ran, so it can never appear in the difference.

    The corpus author ships a tool, it never runs, and the output reports success — which
    is indistinguishable from the tool working, because a built-in of the same name
    answers. This is the outcome the surrounding block already refuses to allow for a
    module that fails to LOAD; reaching it by a different route makes it no better."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=SHADOWS_A_BUILTIN,
                     module="shadow_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    assert "corpus_overview" in str(e.value)


def test_the_collision_error_is_not_the_registered_nothing_error(tmp_path):
    """Two different mistakes must not produce one message.

    When EVERY registration collides, the set difference is empty and the pre-existing
    guard fired "registered no tools" — blaming the corpus for the opposite mistake. It
    registered two; both were rejected."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=ALL_COLLIDE,
                     module="all_collide_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    message = str(e.value)
    assert "registered no tools" not in message
    assert "corpus_overview" in message and "get_document" in message


def test_a_module_that_genuinely_registers_nothing_still_says_so(tmp_path):
    """The pre-existing guard, unchanged. Declaring the hook and adding nothing is its own
    mistake and keeps its own message."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=REGISTERS_NOTHING,
                     module="empty_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    assert "registered no tools" in str(e.value)


NO_COLLISION = '''
    def register(mcp, framework):
        @mcp.tool()
        def join_lookup(document_id: str = "") -> dict:
            return {"rows": []}
'''


def test_a_module_with_no_collisions_starts_exactly_as_before(tmp_path):
    """The control. A corpus registering only its own names is untouched by this."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=NO_COLLISION,
                     module="clean_tools")

    names = _names(build_server(config))

    assert BUILTINS <= names
    assert "join_lookup" in names


# ---------- ways the first version of the #111 guard could be walked past ----------

POSITIONAL_NAME = '''
    def register(mcp, framework):
        @mcp.tool("corpus_overview")
        def my_overview() -> dict:
            """`name` is the FIRST POSITIONAL parameter of tool()."""
            return {"mine": True}
'''

DUPLICATE_WITHIN_MODULE = '''
    def register(mcp, framework):
        @mcp.tool()
        def join_lookup(document_id: str = "") -> dict:
            return {"first": True}

        @mcp.tool(name="join_lookup")
        def join_lookup_v2(document_id: str = "") -> dict:
            return {"second": True}
'''

VIA_ADD_TOOL = '''
    def register(mcp, framework):
        def corpus_overview() -> dict:
            return {"mine": True}
        mcp.add_tool(corpus_overview)

        @mcp.tool()
        def join_lookup(document_id: str = "") -> dict:
            return {"rows": []}
'''

RESERVED_BUT_UNREGISTERED = '''
    def register(mcp, framework):
        @mcp.tool()
        def issuing_body_profile(slug: str = "") -> dict:
            """A CONTRACT name this corpus does not happen to register."""
            return {"mine": True}
'''


def test_a_positionally_named_collision_is_caught(tmp_path):
    """`@mcp.tool("corpus_overview")` — the first version read only `kwargs["name"]`, so it
    recorded the FUNCTION's name, the intersection was empty, and corpus-toolkit#111
    survived its own fix. Worse, when that was the module's only tool the fallback fired
    "registered no tools" — the exact mis-blame the fix claims to have separated."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=POSITIONAL_NAME,
                     module="positional_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    assert "corpus_overview" in str(e.value)
    assert "registered no tools" not in str(e.value)


def test_a_duplicate_within_the_module_is_caught(tmp_path):
    """Sibling instance: the guard compared against names that existed BEFORE the hook, so
    two registrations of one name inside the module never met each other. The SDK keeps the
    first there too, so the second tool is discarded and the summary prints success."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=DUPLICATE_WITHIN_MODULE,
                     module="dup_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    assert "join_lookup" in str(e.value)


def test_registering_through_add_tool_is_caught_too(tmp_path):
    """`add_tool` is public on both majors and is what `tool()` calls internally. Wrapping
    only the decorator left the choke point itself unguarded."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=VIA_ADD_TOOL,
                     module="addtool_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    assert "corpus_overview" in str(e.value)


def test_a_reserved_contract_name_is_refused_even_when_not_registered(tmp_path):
    """`issuing_body_profile` and `authority_chain` are CONDITIONALLY registered. Keying the
    guard on what happens to be present let a corpus without a registry claim the name and
    start clean — serving corpus semantics under a core contract name, and flipping to
    fatal later when a registry is added, with no change to the tools module."""
    config = _corpus(tmp_path, archetype="hybrid", tools_py=RESERVED_BUT_UNREGISTERED,
                     module="reserved_tools")

    with pytest.raises(RuntimeError) as e:
        build_server(config)

    assert "issuing_body_profile" in str(e.value)


def test_the_sdk_really_keeps_the_first_registration(tmp_path):
    """THE PREMISE THE WHOLE GUARD RESTS ON, asserted rather than read off the source.

    Every other test here passes whether the SDK keeps the first registration or replaces
    with the last — they only assert that `build_server` raises. If a future major flips to
    last-wins, the guard still fires while its message ("these were DISCARDED and the
    built-ins answer in their place") becomes false, and the real hazard inverts to a corpus
    tool OVERWRITING a core tool. This is the test that would notice."""
    from corpus_toolkit.mcp import sdk

    srv = sdk.Server("premise-check")

    @srv.tool()
    def duplicated() -> dict:
        return {"which": "first"}

    @srv.tool(name="duplicated")
    def duplicated_again() -> dict:
        return {"which": "second"}

    import asyncio
    answer = asyncio.new_event_loop().run_until_complete(
        sdk.call_tool(srv, "duplicated", {}))

    assert answer == {"which": "first"}, (
        "this SDK replaces on duplicate registration rather than keeping the first — "
        "corpus-toolkit#111's guard message and its hazard both need rewriting")


def test_the_reserved_list_covers_every_tool_the_server_registers(tmp_path):
    """The reserved list and the server must not drift apart.

    A hardcoded list is only correct while it matches what `build_server` actually
    registers. If a new built-in arrives and this set does not, a corpus can claim its name
    — which is the hole `authority_chain` and `issuing_body_profile` were already in,
    because they are conditional and the first guard keyed on what happened to be present."""
    from corpus_toolkit.mcp.server import RESERVED_TOOL_NAMES

    registered = _names(build_server(_corpus(tmp_path)))

    assert registered <= RESERVED_TOOL_NAMES, (
        f"built-in(s) missing from RESERVED_TOOL_NAMES: "
        f"{sorted(registered - RESERVED_TOOL_NAMES)}")
