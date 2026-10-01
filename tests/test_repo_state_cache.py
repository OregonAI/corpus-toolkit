"""`repo_state()` shells out to git twice (`rev-parse HEAD`, `status --porcelain`) and
`ensure_index()` calls it on EVERY backend operation (corpus-toolkit#207). On a warm
server that is ~114 ms of fixed per-call overhead paid for information that changes only
on a commit or a working-tree edit, neither of which happens between most consecutive
calls.

This module proves three things about the fix: a short in-process memo means consecutive
calls within the window do not re-shell to git, a call made WITHIN the window after a
change still serves the stale memoized value (proving the memo is actually live, not a
no-op on this code path), and the first call after the window passes observes the change —
whether by waiting it out or by passing `ttl_seconds=0` to bypass a warm cache outright.
"""
import subprocess
import time
from pathlib import Path

import pytest

from corpus_toolkit import repo


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def test_repeated_calls_within_the_window_do_not_reshell_to_git(git_repo, monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(args, **kw):
        calls.append(args)
        return real_run(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)

    first = repo.repo_state(git_repo)
    for _ in range(9):
        assert repo.repo_state(git_repo) == first

    # One `rev-parse` + one `status --porcelain` for the whole burst, not per call.
    assert len(calls) == 2, (
        f"repo_state() shelled out to git {len(calls)} times across 10 calls with "
        f"nothing changing between them; the memo should have served 8 of them")


def test_a_new_commit_is_served_stale_inside_the_window_then_seen_after_it_passes(
        git_repo):
    # Warm the memo BEFORE the change, so the window below is proving something: that
    # a call landing inside it still returns the value computed before the commit,
    # not just that repo_state() eventually reflects git (which would hold even on
    # origin/main, where there is no memo at all).
    first = repo.repo_state(git_repo, ttl_seconds=0.2)

    (git_repo / "a.txt").write_text("two\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "second"], cwd=git_repo, check=True)

    still_stale = repo.repo_state(git_repo, ttl_seconds=0.2)
    assert still_stale == first, (
        "a call inside the TTL window must still serve the memoized value, proving "
        "the memo is actually live on this call rather than a no-op")

    time.sleep(0.25)
    after_window = repo.repo_state(git_repo, ttl_seconds=0.2)
    assert after_window != first, "a new commit must be observed once the window passes"


def test_a_working_tree_edit_is_served_stale_inside_the_window_then_seen_after_it_passes(
        git_repo):
    first = repo.repo_state(git_repo, ttl_seconds=0.2)

    (git_repo / "a.txt").write_text("edited, uncommitted\n")

    still_stale = repo.repo_state(git_repo, ttl_seconds=0.2)
    assert still_stale == first, (
        "a call inside the TTL window must still serve the memoized value, proving "
        "the memo is actually live on this call rather than a no-op")

    time.sleep(0.25)
    after_window = repo.repo_state(git_repo, ttl_seconds=0.2)
    assert after_window != first, (
        "an uncommitted working-tree edit must be observed once the window passes")


def test_ttl_zero_bypasses_a_warm_cache_and_sees_the_change_immediately(git_repo):
    first = repo.repo_state(git_repo, ttl_seconds=5.0)   # warm a long-lived memo

    (git_repo / "a.txt").write_text("edited, uncommitted\n")

    # Still within the 5s window, but ttl_seconds=0 must bypass the warm cache rather
    # than serve the value memoized a moment ago.
    live = repo.repo_state(git_repo, ttl_seconds=0)
    assert live != first, (
        "ttl_seconds=0 must force a live recompute and see the edit immediately, "
        "even though a warm, unexpired memo exists for this root")


def test_env_var_overrides_the_default_ttl(git_repo, monkeypatch):
    """CORPUS_TOOLKIT_REPO_STATE_TTL_SECONDS lets an operator tune the default window
    per deployment without a code change (corpus-toolkit#207 review). It is read fresh
    on every call with ttl_seconds=None, not frozen at import time."""
    monkeypatch.setenv(repo.REPO_STATE_TTL_ENV_VAR, "0.2")

    first = repo.repo_state(git_repo)   # ttl_seconds=None -> reads the env var
    (git_repo / "a.txt").write_text("edited, uncommitted\n")

    still_stale = repo.repo_state(git_repo)
    assert still_stale == first, "the env-configured window must still memoize"

    time.sleep(0.25)
    after_window = repo.repo_state(git_repo)
    assert after_window != first, "the edit must be observed once the env window passes"


def test_env_var_unset_falls_back_to_the_default_constant(git_repo, monkeypatch):
    monkeypatch.delenv(repo.REPO_STATE_TTL_ENV_VAR, raising=False)
    assert repo._default_ttl_seconds() == repo.DEFAULT_REPO_STATE_TTL_SECONDS


def test_env_var_unparseable_or_negative_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(repo.REPO_STATE_TTL_ENV_VAR, "not-a-number")
    assert repo._default_ttl_seconds() == repo.DEFAULT_REPO_STATE_TTL_SECONDS

    monkeypatch.setenv(repo.REPO_STATE_TTL_ENV_VAR, "-1")
    assert repo._default_ttl_seconds() == repo.DEFAULT_REPO_STATE_TTL_SECONDS


def test_different_roots_do_not_share_a_cache_slot(tmp_path, monkeypatch):
    def make_repo(name):
        root = tmp_path / name
        root.mkdir()
        (root / "f.txt").write_text(name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "c"], cwd=root, check=True)
        return root

    one = make_repo("one")
    two = make_repo("two")

    assert repo.repo_state(one) != repo.repo_state(two)
