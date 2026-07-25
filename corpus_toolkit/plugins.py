"""Dynamic loading of corpus-supplied extension points (see
docs/reference-architecture.md and MIGRATION.md). A corpus registers a hook by
naming a dotted module/attribute path in `_meta/corpus.yml`; the toolkit never
imports corpus-specific code directly. The corpus repo's own root must be on
`sys.path` (true when its CLI/CI runs from the repo root, since Python puts
the current directory's ancestors on the path via the working directory)."""
from __future__ import annotations

import importlib
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
    registration) and return it."""
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return importlib.import_module(dotted_path)


def snapshot_slice_fn(config):
    """(doc_id, snapshot_id, raw_text) -> str. Default: identity (whole text).
    Corpora with shared-snapshot splitting (e.g. one source file backing many
    documents) register `plugins.snapshot_slice_module: "mypackage.slicing:slice"`
    in corpus.yml."""
    if not config.snapshot_slice_module:
        return lambda doc_id, snapshot_id, raw_text: raw_text
    return load_attr(config.snapshot_slice_module, config.root)
