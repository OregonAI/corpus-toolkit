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
