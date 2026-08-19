"""The fork-pool and the staleness gate, each written once (corpus-toolkit#76).

WHAT WAS DUPLICATED. `validate/frontmatter.py` and `validate/provenance.py` each carried
their own module-global worker state, their own `_init_worker`, and their own
`if jobs == 1 or len(paths) < 50: ... else: mp.get_context("fork").Pool(...)` with the same
`chunksize=64`. `index.py` and `status.py` each carried their own regenerate-and-compare
gate with its own notion of "stale".

The threshold and the chunk size were duplicated CONSTANTS, and the global-plus-initializer
handoff is the part of these tools hardest to get right and easiest to copy wrong.

A correction to the issue: it claimed the staleness gate was written three times, counting
`site.py`. It is written twice. `site.py` deliberately has no gate — it builds the index at
deploy time and never commits one, precisely so there is nothing to fall stale.
"""
import json
import pathlib

import pytest

from corpus_toolkit import repo

# Module scope so the fork pool can pickle them.
_SEEN: list = []


def _setup(tag):
    _SEEN.append(tag)


def _stem(path):
    return pathlib.Path(path).stem


def _double(path):
    return int(pathlib.Path(path).stem) * 2


# ------------------------------------------------------------------ map_documents

def test_setup_runs_before_the_work_sequentially():
    _SEEN.clear()
    out = repo.map_documents(["a.md", "b.md"], _stem, jobs=1, setup=_setup, setup_args=("t",))

    assert _SEEN == ["t"], "setup must run exactly once in the sequential path"
    assert out == ["a", "b"]


def test_sequential_below_the_threshold_even_with_many_jobs():
    """Forking costs more than it saves on a small corpus, which is why the threshold
    exists. Asserting the RESULTS rather than the mechanism, but the ordering is the tell:
    the pool path is imap_unordered."""
    paths = [f"{i}.md" for i in range(repo.PARALLEL_THRESHOLD - 1)]

    assert repo.map_documents(paths, _double, jobs=8) == [i * 2 for i in range(len(paths))]


@pytest.mark.filterwarnings(
    # Python 3.12 warns when fork() runs in a MULTI-THREADED parent. pytest is one; the
    # `corpus-*` CLIs that actually call this are not, so the condition being warned about
    # does not arise in production. Filtered here rather than repo-wide so a fork warning
    # from anywhere else still surfaces.
    #
    # Worth knowing: this warning only appears now because this is the first test the
    # toolkit has ever had for the parallel path at all — both validators forked untested.
    "ignore:This process .* is multi-threaded, use of fork:DeprecationWarning")
def test_every_path_is_processed_in_the_parallel_path():
    """Above the threshold with jobs>1 this forks. Order is NOT asserted — the pool uses
    imap_unordered, so results arrive as workers finish, and both callers aggregate rather
    than index. Changing that to ordered would be a behaviour change, not a refactor."""
    n = repo.PARALLEL_THRESHOLD + 10
    paths = [f"{i}.md" for i in range(n)]

    out = repo.map_documents(paths, _double, jobs=4)

    assert sorted(out) == sorted(i * 2 for i in range(n))


def test_no_paths_still_runs_setup():
    """Named for what actually happens, not what sounded tidy.

    The first version of this test was called `test_no_paths_is_no_work`, cleared `_SEEN`
    and then asserted nothing about it — so it neither pinned nor noticed that setup runs
    anyway. It does, and it did before this extraction too, so skipping it would be a
    behaviour change rather than the pure refactor this is meant to be.
    """
    _SEEN.clear()

    assert repo.map_documents([], _stem, jobs=4, setup=_setup, setup_args=("t",)) == []
    assert _SEEN == ["t"], "setup still runs; preserved from the pre-extraction code"


def test_jobs_zero_means_do_not_fork():
    """`-j 0` was `max(1, args.jobs)` in both validators — sequential.

    A falsy check (`jobs if jobs else cpu_count()`) reads 0 as "unset" and forks across
    every CPU instead: the opposite of what someone passing 0 asked for, and silently so in
    exactly the constrained environments where they would have.
    """
    n = repo.PARALLEL_THRESHOLD + 10
    paths = [f"{i}.md" for i in range(n)]

    # Ordered output is the tell: the parallel path is imap_unordered.
    assert repo.map_documents(paths, _double, jobs=0) == [i * 2 for i in range(n)]


# ----------------------------------------------------------------- check_generated

def test_missing_file_is_not_current(tmp_path):
    current, msg = repo.check_generated(tmp_path / "STATUS.md", "anything")

    assert current is False
    assert "missing" in msg


def test_identical_content_is_current(tmp_path):
    p = tmp_path / "STATUS.md"
    p.write_text("same")

    current, msg = repo.check_generated(p, "same")

    assert current is True
    assert "current" in msg


def test_different_content_is_stale(tmp_path):
    p = tmp_path / "STATUS.md"
    p.write_text("old")

    current, msg = repo.check_generated(p, "new")

    assert current is False
    assert "STALE" in msg


def test_normalize_decides_what_counts_as_a_difference(tmp_path):
    """The whole reason this is a parameter: index.py drops a `generated` key, status.py
    strips date-shaped lines. Both mean "ignore this when deciding staleness"."""
    p = tmp_path / "STATUS.md"
    p.write_text("Generated 1999-01-01\nbody")

    current, _ = repo.check_generated(p, "Generated 2026-08-19\nbody",
                                      normalize=lambda s: s.split("\n", 1)[1])

    assert current is True


def test_a_normalizer_that_cannot_parse_reports_that_not_staleness(tmp_path):
    """`index.py` compares parsed JSON. A committed file that is not JSON at all is a
    different answer from one that is JSON and out of date, and the message must say so —
    telling someone to regenerate a corrupt file is right, telling them it is merely stale
    is not."""
    p = tmp_path / "corpus-index.json"
    p.write_text("{not json")

    current, msg = repo.check_generated(p, "{}", normalize=json.loads)

    assert current is False
    assert "not in the expected format" in msg
    assert "STALE" not in msg


def test_undecodable_bytes_are_reported_not_raised(tmp_path):
    """`read_text` raises UnicodeDecodeError, which is a ValueError and NOT an OSError.

    An `except OSError` alone lets one stray latin-1 byte in a committed artifact escape as
    a traceback — and `index.py --check` used to catch exactly this case and print an
    actionable line, so letting it through would have been a regression introduced by a
    refactor.
    """
    p = tmp_path / "corpus-index.json"
    p.write_bytes(b'{"a": "\xe9"}')

    current, msg = repo.check_generated(p, "{}", normalize=json.loads)

    assert current is False
    assert "could not be read" in msg


def test_the_gate_can_actually_fail(tmp_path):
    """The check this whole issue is downstream of. status.py's predecessor asked "does the
    file contain '## Documents by type'?" and could not fail: a file listing 0 documents and
    dated 1970 passed. Pin that the current one distinguishes all four states."""
    p = tmp_path / "f"
    states = []
    p.write_text("a")
    states.append(repo.check_generated(p, "a")[0])
    states.append(repo.check_generated(p, "b")[0])
    p.unlink()
    states.append(repo.check_generated(p, "a")[0])

    assert states == [True, False, False]
