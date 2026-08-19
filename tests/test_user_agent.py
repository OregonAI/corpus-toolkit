"""The sibling-index fetcher must identify itself honestly (corpus-toolkit#82).

`remote.USER_AGENT` was the hand-written literal `corpus-toolkit/1.1`, frozen since v1.1 and
wrong for twenty-four releases. It is the only thing a remote server learns about us on a
sibling-index fetch, and this platform asks publishers to make deliberate decisions about its
agent — `corpus-detect-changes --check-robots` exists for exactly that. Telling them a version
that has not existed since v1.1 undercuts the whole posture.

Note what is NOT tested here: `sources/changes.py`'s `corpus-toolkit-change-detector`. That
string is passed to `robots.allowed()` and `robots.ai_position()`, so it is the token matched
against robots.txt directives — load-bearing rather than cosmetic, and deliberately left alone.
"""
import pathlib
import re
from importlib.metadata import PackageNotFoundError, distribution, version

import pytest

from corpus_toolkit import remote

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def declared_version() -> str:
    """The version `pyproject.toml` states — an INDEPENDENT source of truth.

    Regex rather than tomllib because `requires-python` is >=3.10 and tomllib landed in
    3.11; the CI matrix runs both.
    """
    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.M)
    assert m, f"no version in {PYPROJECT}"
    return m.group(1)


def test_user_agent_names_the_version_this_source_declares():
    """The header must name the version of the code that is running.

    COMPARED AGAINST `pyproject.toml`, NOT against `importlib.metadata`. The first version of
    this test asserted `USER_AGENT.startswith(f"corpus-toolkit/{version('corpus-toolkit')}")`
    — the same call the implementation makes — so it was `version() == version()` and passed
    for any value that happened to be installed, including a wrong one. It did exactly that:
    it reported green on a checkout whose editable install still declared 1.14.0 while
    pyproject said 1.25.0. A check that cannot fail is worse than no check, and a tautology
    guarding a staleness bug is that failure in its purest form.

    A failure here means one of two things, and the message says both: the derivation broke,
    or the environment's install metadata is stale and needs `pip install -e .`. CI installs
    fresh on every run, so there it can only mean the former.
    """
    try:
        installed = version("corpus-toolkit")
    except PackageNotFoundError:
        pytest.skip("corpus-toolkit is not installed; nothing to compare metadata against")

    declared = declared_version()
    assert installed == declared, (
        f"install metadata says {installed}, pyproject.toml says {declared}. The user agent "
        f"is derived from the former, so it would report a version this source is not. "
        f"Refresh the install: pip install -e .")
    assert remote.USER_AGENT.startswith(f"corpus-toolkit/{declared} ")


def test_user_agent_keeps_its_shape():
    """product/version, a contact URL, and what the request is for — the three things an
    operator reading their access log needs to decide whether to allow us."""
    m = re.fullmatch(
        r"corpus-toolkit/(?P<v>\S+) \(\+https://github\.com/OregonAI/corpus-toolkit\) "
        r"sibling-index-fetch", remote.USER_AGENT)
    assert m, f"unexpected shape: {remote.USER_AGENT!r}"
    assert m.group("v") != "1.1", "the frozen literal is back"


def test_the_distribution_is_the_one_in_this_checkout():
    """Guards the comparison above from being satisfied by SOME OTHER install.

    `importlib.metadata` searches sys.path, so a stray site-packages copy can answer for a
    name this checkout also provides — which is how the stale 1.14.0 metadata was reached in
    the first place, from a `.dist-info` written when the editable install was made.
    """
    try:
        dist = distribution("corpus-toolkit")
    except PackageNotFoundError:
        pytest.skip("corpus-toolkit is not installed")
    assert dist.version == declared_version(), (
        f"the corpus-toolkit answering importlib.metadata is version {dist.version}, not the "
        f"{declared_version()} this tree declares")


def test_uninstalled_reports_unknown_rather_than_a_number(monkeypatch):
    """A source checkout has no package metadata. Say so.

    NOT a plausible-looking placeholder like `0.dev`: this platform's standing rule is that
    unknown is stated and never upgraded to a value (see the `status: ""` handling in
    remote.lookup, and corpus-toolkit#25). A version string nobody can act on is worse than
    an admission, because an operator reading the log cannot tell it from a real release.
    """
    def _raise(_name):
        raise PackageNotFoundError("corpus-toolkit")
    monkeypatch.setattr(remote, "version", _raise)

    assert remote._toolkit_version() == "unknown"


def test_the_version_lookup_never_raises(monkeypatch):
    """A fetcher that cannot build a header cannot fetch. The module's whole contract is that
    an unreachable sibling degrades resolution and never breaks the server; a header
    derivation that raises would break it at import time, before any of that applies."""
    def _boom(_name):
        raise RuntimeError("metadata backend exploded")
    monkeypatch.setattr(remote, "version", _boom)

    assert remote._toolkit_version() == "unknown"
