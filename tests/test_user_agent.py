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
import json
import pathlib
import re
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest

from corpus_toolkit import remote

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def declared_version() -> str:
    """The version `pyproject.toml` states — an INDEPENDENT source of truth.

    Regex rather than tomllib because `requires-python` is >=3.10 and tomllib landed in
    3.11; the CI matrix runs both.
    """
    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.M)
    assert m, f"no version in {PYPROJECT}"
    return m.group(1)


def distribution_metadata_path(dist) -> pathlib.Path:
    """Where the metadata that answered lives, resolved, as precisely as it can be had.

    Named in the skip reason, because the cause of corpus-toolkit#146 is invisible without
    it: two agents read these failures as a property of the BRANCH and wrote "pre-existing
    on `origin/main`" into PR #144. They are a property of the DIRECTORY.

    `Distribution._path` — the `.dist-info` or `.egg-info` directory itself — is not public,
    so a distribution served by some other finder may not have one. `locate_file("")` is the
    coarser public answer: the directory that distribution resolves its files against, which
    for a `.dist-info` is its parent rather than the directory itself. Coarser still tells
    one checkout from another, which is the whole job here.
    """
    path = getattr(dist, "_path", None)
    if path is None:
        path = dist.locate_file("")
    return pathlib.Path(path).resolve()


def distribution_source_tree(dist):
    """The checkout this distribution was built from, resolved — or `None` if it cannot be
    placed at all.

    Two install mechanisms leave two different traces, and both must be read:

    * **PEP 610 `direct_url.json`**, written by `pip install -e .`. It names the source
      directory outright, wherever the `.dist-info` itself ended up — which for an editable
      install is site-packages, nowhere near the tree it serves.
    * **The package files the distribution owns**, for everything else: a wheel unpacked
      into site-packages, or the `*.egg-info` setuptools leaves beside `pyproject.toml`.
      Owning them is checked, not assumed — `locate_file` joins a path without caring
      whether anything is there, so an unchecked join would place every distribution
      somewhere and this function would never be able to say it does not know.

    A NON-editable `direct_url.json` is deliberately not read as the source tree. That
    install is a copy taken at some past moment; the code running here is this tree's, so
    the copy's version describes something else.
    """
    raw = _direct_url(dist)
    if raw is not None:
        try:
            info = json.loads(raw)
            editable = bool(info["dir_info"]["editable"])
            url = info["url"]
        except (ValueError, TypeError, KeyError):
            editable, url = False, None
        if editable and isinstance(url, str) and url.startswith("file:"):
            return pathlib.Path(url2pathname(urlsplit(url).path)).resolve()

    owned = pathlib.Path(dist.locate_file("corpus_toolkit/__init__.py"))
    return owned.resolve().parent.parent if owned.is_file() else None


def _direct_url(dist):
    """`direct_url.json`'s text, or `None` where there is none or it cannot be read.

    `PathDistribution.read_text` already returns `None` for a file that is absent or
    unreadable; the guard is for any other `Distribution` implementation, because a guard
    that decides whether the suite runs must not be the thing that breaks it.
    """
    try:
        return dist.read_text("direct_url.json")
    except OSError:
        return None


def in_tree_distribution(lookup=distribution):
    """The installed distribution belonging to THIS checkout, or `pytest.skip` saying why.

    "Is corpus-toolkit installed?" is the wrong question, and asking it is
    corpus-toolkit#146. `importlib.metadata` resolves against `sys.path`, not against this
    checkout, so in a `git worktree` something always answers — a stale user-site install
    made from a different directory. The old guard saw an answer, ran the comparison, and
    compared THIS tree's code against SOMEONE ELSE'S metadata. That comparison cannot hold
    and says nothing about the code under test: it is the "nothing to compare metadata
    against" the skip already described, reached by a route the condition did not cover.

    The question is therefore whether the distribution that answered is this tree's. Where
    it is, nothing is skipped — a stale install of this checkout must still FAIL, which is
    the whole reason these tests exist, and CI installs editable from the checkout root so
    a broken derivation is still caught there.
    """
    try:
        dist = lookup("corpus-toolkit")
    except PackageNotFoundError:
        pytest.skip("corpus-toolkit is not installed; nothing to compare metadata against")

    source = distribution_source_tree(dist)
    if source != REPO_ROOT:
        built = f"built from {source}" if source else "and cannot be placed in any checkout"
        pytest.skip(
            f"corpus-toolkit {dist.version} answered importlib.metadata from "
            f"{distribution_metadata_path(dist)}, {built}, not from this checkout at "
            f"{REPO_ROOT} — so there is nothing here to compare metadata against. This is a "
            f"property of the DIRECTORY, not of the branch: `*.egg-info/` is gitignored, so "
            f"a fresh `git worktree` has none and resolution falls through to whatever else "
            f"is on sys.path. Do not conclude 'pre-existing on main'; do not run "
            f"`pip install -e .` to silence it, which repoints the shared install for every "
            f"other worktree (corpus-toolkit#146).")
    return dist


def assert_metadata_matches_this_checkout(dist) -> None:
    """This checkout's own install metadata must state the version this source declares.

    Factored out of the two tests below so the STALE case can be exercised against a
    distribution built for the purpose. A staleness bug is by definition absent from the
    directory the suite happens to run in, so it can only be reached by construction.
    """
    declared = declared_version()
    assert dist.version == declared, (
        f"this checkout's own install metadata says {dist.version}, pyproject.toml says "
        f"{declared}. The user agent is derived from the former, so it would report a "
        f"version this source is not. The install is stale and needs rebuilding — but note "
        f"that a bare `pip install -e .` repoints a SHARED user-site install, so do it "
        f"deliberately, not reflexively.")


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
    or THIS checkout's own install metadata is stale. CI installs fresh on every run, so
    there it can only mean the former. It no longer means a third thing — that some
    unrelated install answered — because `in_tree_distribution` excludes that case before
    the comparison rather than letting it arrive here as a version disagreement
    (corpus-toolkit#146).
    """
    assert_metadata_matches_this_checkout(in_tree_distribution())
    assert remote.USER_AGENT.startswith(f"corpus-toolkit/{declared_version()} ")


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

    That identity question is now decided before the comparison, by `in_tree_distribution`,
    rather than after it by a version that happened to disagree (corpus-toolkit#146).
    """
    assert_metadata_matches_this_checkout(in_tree_distribution())


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


# --- The guard that decides whether there is anything to compare against (corpus-toolkit#146) ---
#
# `importlib.metadata` resolves against sys.path, not against this checkout, so SOMETHING
# usually answers for the name even where nothing in this tree was ever installed. These
# exercise the guard directly, with real `Distribution` objects built on disk, because the
# behaviour that matters is a three-way split — pass, skip, fail — that a single run in a
# single directory can only ever show one third of.


def _distribution_at(tmp_path, version, editable_source=None, package_dir=False):
    """A real on-disk `.dist-info` a `Distribution` can be read from.

    `editable_source` writes the PEP 610 `direct_url.json` an editable install leaves
    behind; `package_dir` writes the package files a wheel install would own instead.
    """
    info = tmp_path / f"corpus_toolkit-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: corpus-toolkit\nVersion: {version}\n")
    if editable_source is not None:
        (info / "direct_url.json").write_text(
            json.dumps({"dir_info": {"editable": True},
                        "url": pathlib.Path(editable_source).as_uri()}))
    if package_dir:
        pkg = tmp_path / "corpus_toolkit"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("")
    return Distribution.at(info)


def _foreign_distribution(tmp_path):
    """A stale editable install made from a DIFFERENT checkout — the worktree case, exactly
    as it stands on this machine: user-site metadata pointing at the main clone."""
    other = tmp_path / "some-other-checkout"
    other.mkdir()
    return _distribution_at(tmp_path, "1.26.1", editable_source=other), other


def test_a_distribution_from_another_tree_is_not_this_checkouts(tmp_path):
    """The worktree case, and the whole of corpus-toolkit#146.

    A stale user-site editable install answers for the name, but it was made from some
    OTHER directory, so its version says nothing about the code this run imported. That is
    the "nothing to compare metadata against" the skip already describes.
    """
    dist, other = _foreign_distribution(tmp_path)

    with pytest.raises(pytest.skip.Exception) as caught:
        in_tree_distribution(lambda _name: dist)

    assert "1.26.1" in caught.value.msg
    assert str(other) in caught.value.msg


def test_the_skip_reason_names_the_resolved_path_of_what_answered(tmp_path):
    """So nobody has to infer the cause — and nobody concludes "pre-existing on main".

    Two agents reported these failures as a property of the BRANCH; they are a property of
    the DIRECTORY. Naming the metadata that answered and the checkout it is not from is the
    entire diagnosis, and it is the one thing the old message could not give.
    """
    dist, _other = _foreign_distribution(tmp_path)

    with pytest.raises(pytest.skip.Exception) as caught:
        in_tree_distribution(lambda _name: dist)

    assert str((tmp_path / "corpus_toolkit-1.26.1.dist-info").resolve()) in caught.value.msg
    assert str(REPO_ROOT) in caught.value.msg


def test_an_editable_install_of_this_checkout_is_this_ones(tmp_path):
    """The case a blanket skip would destroy, half one: it must NOT skip.

    An editable install made from this very tree is the one whose version genuinely
    describes the code that ran, so the comparison above is meaningful and must happen.
    """
    dist = _distribution_at(tmp_path, declared_version(), editable_source=REPO_ROOT)

    # Caught rather than allowed to propagate: an escaping `Skipped` would report this test
    # as skipped, and a skipped test is not a failing one. The criterion would be unable to
    # fail at exactly the moment it is violated.
    try:
        resolved = in_tree_distribution(lambda _name: dist)
    except pytest.skip.Exception as skipped:
        pytest.fail(f"an editable install of THIS checkout was skipped: {skipped.msg}")

    assert resolved is dist


def test_a_stale_install_of_this_checkout_still_fails(tmp_path):
    """The case a blanket skip would destroy, half two — and the reason these tests exist.

    An editable install made from this tree, left behind at an older version, is a REAL
    staleness bug: the user agent would report 0.0.1 to every operator reading their access
    log. Widening the skip must not swallow it.
    """
    dist = _distribution_at(tmp_path, "0.0.1", editable_source=REPO_ROOT)

    try:
        resolved = in_tree_distribution(lambda _name: dist)
    except pytest.skip.Exception as skipped:
        pytest.fail(f"a STALE install of THIS checkout was skipped: {skipped.msg}")

    with pytest.raises(AssertionError) as caught:
        assert_metadata_matches_this_checkout(resolved)
    assert "0.0.1" in str(caught.value)
    assert declared_version() in str(caught.value)


def test_an_install_with_no_direct_url_is_placed_by_the_files_it_owns(tmp_path):
    """No `direct_url.json` at all — an unpacked wheel, and the `*.egg-info` shape too.

    The tree is then wherever the package files the distribution owns live, which is what
    makes the main checkout's in-tree `corpus_toolkit.egg-info` resolve to this tree while
    a site-packages copy does not.

    The version here MATCHES what pyproject declares, deliberately: the question the guard
    asks is one of IDENTITY, and a matching version from an unrelated install is exactly
    the coincidence that would let a wrong answer through.
    """
    dist = _distribution_at(tmp_path, declared_version(), package_dir=True)

    assert distribution_source_tree(dist) == tmp_path.resolve()
    with pytest.raises(pytest.skip.Exception):
        in_tree_distribution(lambda _name: dist)


def test_a_distribution_that_cannot_be_placed_says_so_rather_than_guessing(tmp_path):
    """No `direct_url.json`, and no package files where it would own them either.

    `locate_file` joins a path whether or not anything is there, so an unchecked join would
    place this distribution in `tmp_path` and the guard would compare against a tree that
    does not exist. "Could not check" is never reported as "is not there" (AGENTS.md), and
    the corollary here is that it is never reported as a location either.
    """
    dist = _distribution_at(tmp_path, declared_version())

    assert distribution_source_tree(dist) is None
    with pytest.raises(pytest.skip.Exception) as caught:
        in_tree_distribution(lambda _name: dist)
    assert "cannot be placed in any checkout" in caught.value.msg
