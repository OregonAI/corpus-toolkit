"""changed_content_files() base-ref resolution.

The guardrail that makes an unresolvable --changed ref a hard error (rather than
silently validating zero files and exiting 0) shipped without tests of its own. These
cover both directions, because the two failure modes point opposite ways: swallowing a
missing ref turns CI green having checked nothing, while erroring on git's null SHA
fails CI on every newly created branch.
"""
import subprocess
from pathlib import Path

import pytest

from corpus_toolkit.repo import UnresolvableRef, changed_content_files

NULL_SHA = "0" * 40


class _Config:
    """Minimal stand-in for CorpusConfig — changed_content_files only needs .root and
    the content-root globs used to filter results."""

    def __init__(self, root: Path):
        self.root = root
        self.content_roots = [{"path": "docs", "doc_type": "doc"}]
        self.content_dirs = ["docs"]


def _git(root: Path, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path.parent, "init", "-q", "-b", "main", str(tmp_path))
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "first")
    (docs / "b.md").write_text("---\nid: b\n---\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "second")
    return tmp_path


def test_named_ref_that_git_cannot_resolve_is_a_hard_error(repo):
    """The original finding: a ref the caller named explicitly and git cannot resolve
    must never degrade to 'nothing changed'."""
    with pytest.raises(UnresolvableRef):
        changed_content_files(_Config(repo), "origin/nonexistent-branch")


def test_null_sha_is_treated_as_no_base_not_as_a_broken_ref(repo):
    """GitHub sends the all-zero SHA as github.event.before when a push creates a ref.
    Both shipped workflows pass that straight to --changed, so raising here would fail
    CI on the first push to every new branch."""
    files = changed_content_files(_Config(repo), NULL_SHA)
    assert isinstance(files, list)          # resolved, did not raise


def test_abbreviated_null_sha_also_treated_as_no_base(repo):
    """Some tooling abbreviates the sentinel; it means the same thing."""
    assert isinstance(changed_content_files(_Config(repo), "0000000"), list)


def test_resolvable_ref_still_reports_its_changes(repo):
    """The guardrail must not have broken the normal path."""
    files = changed_content_files(_Config(repo), "HEAD~1")
    assert any(Path(f).name == "b.md" for f in files), files
