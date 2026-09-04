"""Compatibility alias. The client seam lives at `corpus_toolkit.mcp.sdk` (ADR 0006); this
name is kept so existing imports keep working. Import from `corpus_toolkit.mcp.sdk`.

The alias is the module object itself, not a re-export list: `_sdk` and `sdk` are one
module, so a name added to `sdk` is visible through `_sdk` without editing this file, and
a test that monkeypatches one patches the other.
"""
import sys as _sys

from corpus_toolkit.mcp import sdk as _public

_sys.modules[__name__] = _public
