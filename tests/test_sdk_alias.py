"""`corpus_toolkit.mcp.sdk` is the public client seam (ADR 0006); `_sdk` is the same module
under its historical name. Same object, not a copy: a name added to `sdk` is visible through
`_sdk` without editing the alias, and patching one patches the other."""
import importlib


def test_the_alias_is_the_same_module_object():
    from corpus_toolkit.mcp import _sdk, sdk

    assert _sdk is sdk
    assert importlib.import_module("corpus_toolkit.mcp._sdk") is sdk
    assert sdk.__name__ == "corpus_toolkit.mcp.sdk"


def test_every_public_name_reaches_through_the_alias():
    from corpus_toolkit.mcp import _sdk, sdk

    public = [n for n in dir(sdk) if not n.startswith("_")]
    assert {"Server", "call_tool", "tool_names", "sdk_version", "SDK_MAJOR"} <= set(public)
    for name in public:
        assert getattr(_sdk, name) is getattr(sdk, name), name


def test_from_import_of_a_name_through_the_old_path_still_works():
    from corpus_toolkit.mcp._sdk import Server as OldServer
    from corpus_toolkit.mcp.sdk import Server

    assert OldServer is Server
