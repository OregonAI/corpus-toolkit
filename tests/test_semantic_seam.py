"""The semantic seam is handed its CORPUS (corpus-toolkit#74).

Two things are pinned here.

FIRST, `make(config)`. The seam used to pass no corpus at all, so the shared module had
nothing to resolve paths against and nowhere to keep per-corpus state: `artifact_dir()`
read `Path.cwd()` and the loaded index lived in a module global, which every framework in
the process shared because `load_module` hands them one installed module object. The
builder never had that problem — `semantic/build.py` writes to `cfg.root/_meta/embeddings`
— so the two halves of one artifact disagreed about where it lives, and only the reader
could be wrong without saying so.

SECOND, `selftest()` actually runs. It was written to be checkable without the artifact,
which is gitignored and absent from CI ("this is checkable in CI, where the real index will
never exist"), and then nothing in .github/workflows/tests.yml ever called it. A guard
nobody runs is a guard that has already stopped working; you just do not know it yet.
"""
from pathlib import Path

import pytest

from corpus_toolkit import config as config_mod
from corpus_toolkit.mcp.framework import CorpusFramework
from corpus_toolkit.semantic import search as search_mod

CORPUS_YML = """\
corpus:
  id: {id}
  name: {id}
  jurisdiction: test
  archetype: document
content_roots:
  - path: docs
    doc_type: statute
{plugins}"""

# A corpus-supplied semantic module of each generation: one exposing the `make(config)`
# factory, one that predates it and is duck-typed as the module itself.
WITH_MAKE = """\
class Index:
    def __init__(self, config):
        self.root = str(config.root)

    def available(self):
        return True

    def rank(self, query, want):
        return [f"{self.root}:{query}"]


def make(config):
    return Index(config)
"""

WITHOUT_MAKE = """\
def available():
    return True


def rank(query, want):
    return ["module-level"]
"""


def make_corpus(root: Path, corpus_id: str, module_src: str | None = None) -> Path:
    (root / "_meta").mkdir(parents=True)
    (root / "docs").mkdir()
    plugins = ""
    if module_src is not None:
        (root / "src").mkdir()
        (root / "src" / "__init__.py").write_text("")
        (root / "src" / "sem.py").write_text(module_src)
        plugins = 'plugins:\n  semantic_search_module: "src.sem"\n'
    (root / "_meta" / "corpus.yml").write_text(
        CORPUS_YML.format(id=corpus_id, plugins=plugins))
    return root / "_meta" / "corpus.yml"


def framework(cfg_path: Path) -> CorpusFramework:
    return CorpusFramework(config_mod.load(cfg_path))


# --------------------------------------------------------------- artifact_dir precedence

def test_artifact_dir_resolves_against_the_corpus_root(monkeypatch):
    """The path the BUILDER already writes to. The reader used to guess."""
    monkeypatch.delenv("CORPUS_SEMANTIC_DIR", raising=False)

    class Cfg:
        root = Path("/srv/oregon-audits")

    assert search_mod.artifact_dir(Cfg()) == Path("/srv/oregon-audits/_meta/embeddings")


def test_artifact_dir_without_a_config_keeps_the_cwd_default(monkeypatch):
    """The module-level shims have no config to ask, and every existing deployment runs
    with the repo root as its cwd. Changing that default would be the behaviour change
    this fix exists to avoid."""
    monkeypatch.delenv("CORPUS_SEMANTIC_DIR", raising=False)

    assert search_mod.artifact_dir() == Path.cwd() / "_meta" / "embeddings"


def test_env_override_wins_over_the_corpus_root(monkeypatch):
    """An operator's explicit mount is the most specific answer available, so it must
    still beat the config — otherwise this fix silently relocates a mounted volume."""
    monkeypatch.setenv("CORPUS_SEMANTIC_DIR", "/mnt/vectors")

    class Cfg:
        root = Path("/srv/oregon-audits")

    assert search_mod.artifact_dir(Cfg()) == Path("/mnt/vectors")


# ------------------------------------------------------------------- the framework seam

def test_framework_prefers_the_make_factory(tmp_path):
    cfg = make_corpus(tmp_path / "a", "corpus-a", WITH_MAKE)

    sem = framework(cfg)._semantic

    assert sem.rank("q", 1) == [f"{tmp_path / 'a'}:q"], \
        "the framework did not hand the corpus to make(config)"


def test_two_corpora_get_their_own_index(tmp_path):
    """The failure this closes: one installed module object, one loaded index, shared by
    every framework in the process — so corpus B ranked against corpus A's vectors."""
    a = framework(make_corpus(tmp_path / "a", "corpus-a", WITH_MAKE))._semantic
    b = framework(make_corpus(tmp_path / "b", "corpus-b", WITH_MAKE))._semantic

    assert a is not b
    assert a.rank("q", 1) != b.rank("q", 1)


def test_a_module_without_make_is_still_duck_typed(tmp_path):
    """A corpus-supplied semantic module written before the factory existed keeps
    working, unchanged, with no edit to its corpus.yml."""
    cfg = make_corpus(tmp_path / "old", "corpus-old", WITHOUT_MAKE)

    sem = framework(cfg)._semantic

    assert sem.available() is True
    assert sem.rank("q", 1) == ["module-level"]


def test_no_semantic_module_is_none(tmp_path):
    assert framework(make_corpus(tmp_path / "bare", "corpus-bare"))._semantic is None


# ------------------------------------------------------------------------ the selftest

def test_selftest_passes():
    """Runs the module's own guard, which CI never called.

    It asserts the properties this file cannot reach cheaply: that converting the int8
    artifact to float32 once at load cannot reorder a ranking, that rank() works on both
    resident dtypes, that an unavailable index returns [] rather than raising, and that
    prepare_vectors() still converts.
    """
    pytest.importorskip("numpy", reason="the semantic extra is not installed")

    assert search_mod.selftest() == 0
