"""`repo_state()` shells out to git twice (`rev-parse HEAD`, `status --porcelain`) and
`ensure_index()` calls it on EVERY backend operation (corpus-toolkit#207). On a warm
server that is ~114 ms of fixed per-call overhead paid for information that changes only
on a commit or a working-tree edit, neither of which happens between most consecutive
calls.

This module proves two things about the fix: a short in-process memo means consecutive
calls within the window do not re-shell to git, and the memo still answers truthfully —
a commit, or a plain working-tree edit, invalidates it once the window has passed.
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


def test_a_new_commit_invalidates_the_memo_once_the_window_passes(git_repo):
    first = repo.repo_state(git_repo, ttl_seconds=0.05)
    time.sleep(0.06)

    (git_repo / "a.txt").write_text("two\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "second"], cwd=git_repo, check=True)

    second = repo.repo_state(git_repo, ttl_seconds=0.05)
    assert second != first, "a new commit must still invalidate the cached state"


def test_a_working_tree_edit_invalidates_the_memo_once_the_window_passes(git_repo):
    first = repo.repo_state(git_repo, ttl_seconds=0.05)
    time.sleep(0.06)

    (git_repo / "a.txt").write_text("edited, uncommitted\n")

    second = repo.repo_state(git_repo, ttl_seconds=0.05)
    assert second != first, (
        "an uncommitted working-tree edit must still invalidate the cached state")


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
