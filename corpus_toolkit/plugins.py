"""Dynamic loading of corpus-supplied extension points (see
docs/reference-architecture.md and MIGRATION.md). A corpus registers a hook by
naming a dotted module/attribute path in `_meta/corpus.yml`; the toolkit never
imports corpus-specific code directly. The corpus repo's own root must be on
`sys.path` (true when its CLI/CI runs from the repo root, since Python puts
the current directory's ancestors on the path via the working directory)."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


def load_attr(dotted_path: str, root: Path):
    """Import 'module.sub:attr' or 'module.sub.attr' relative to a corpus repo
    rooted at `root`, returning the resolved attribute (function, etc.)."""
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    module_name, _, attr = dotted_path.partition(":")
    if not attr:
        module_name, _, attr = dotted_path.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def load_module(dotted_path: str, root: Path):
    """Import a module purely for its side effects (e.g. citation-scheme
    registration) and return it.

    Loaded under a name NAMESPACED BY CORPUS ROOT, not by its bare dotted path.
    Every corpus in this platform follows the same `src.citations` convention, so a
    plain importlib.import_module put the first one into sys.modules and handed that
    same module back to every later corpus — whose own schemes then never registered,
    silently, while resolve_citation reported the FIRST corpus's schemes as the ones it
    had attempted. Path-based loading with a unique name removes the collision."""
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    target = root
    for part in dotted_path.split("."):
        target = target / part
    file_path = None
    if (target.with_suffix(".py")).is_file():
        file_path = target.with_suffix(".py")
    elif (target / "__init__.py").is_file():
        file_path = target / "__init__.py"
    if file_path is None:
        # Not a file under this root (an installed package, say) — no collision risk
        # from the corpus convention, so the ordinary import is correct.
        return importlib.import_module(dotted_path)

    alias = f"_corpus_{abs(hash(root_str)) & 0xffffffff:08x}_{dotted_path.replace('.', '_')}"
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(alias, None)
        raise
    return module


def snapshot_slice_fn(config):
    """(doc_id, snapshot_id, raw_text) -> str. Default: identity (whole text).
    Corpora with shared-snapshot splitting (e.g. one source file backing many
    documents) register `plugins.snapshot_slice_module: "mypackage.slicing:slice"`
    in corpus.yml."""
    if not config.snapshot_slice_module:
        return lambda doc_id, snapshot_id, raw_text: raw_text
    return load_attr(config.snapshot_slice_module, config.root)
