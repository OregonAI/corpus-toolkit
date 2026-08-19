"""The release gate runs corpus-template's build-time commands (corpus-toolkit#100).

`release-gate.yml` checks the template out and runs `contract_smoke.py` against it — a
Python-level check that never executed the template's Dockerfile. That `RUN` is the one
artifact in the org describing how a corpus actually STARTS, and nothing ran it.

corpus-toolkit#75 deleted `CorpusFramework.ensure_index` after finding no caller in this
repo. The template's Dockerfile calls it. The gate went green, v1.25.0 and v1.26.0 shipped,
and every corpus image build failed until v1.26.1 — while a reconcile loop retried the
failing build every ten minutes and helped fill the deploy host's disk.

EXTRACT, DO NOT COPY. A hardcoded duplicate of that command in CI drifts from the Dockerfile
and then asserts nothing — the same species of bug this closes.

NARROW BEATS GENERAL, AND SILENCE IS THE ENEMY. This is not a Dockerfile parser. It
recognises the handful of step shapes the template actually uses and REFUSES anything else,
because a parser that silently matches nothing is exactly the green-that-asserts-nothing
being fixed here. A previous attempt at this issue ran two hours and produced nothing;
the recorded direction was to keep it narrow and make it report what it skipped.
"""
import importlib.util
import pathlib
import sys

import pytest

_GATE = pathlib.Path(__file__).resolve().parent.parent / ".github" / "scripts" / "contract_smoke.py"


def _gate_module():
    """Load the gate script by path — it lives in .github/scripts and is not importable."""
    spec = importlib.util.spec_from_file_location("contract_smoke", _GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["contract_smoke"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _gate_module()


# The template's real Dockerfile shape, verbatim in structure: an apt-get chain with a
# cleanup, a pip install, and the three toolkit-facing steps chained with `&&`.
TEMPLATE_DOCKERFILE = """\
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && \\
    rm -rf /var/lib/apt/lists/*
WORKDIR /repo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python3 -c "\\
from corpus_toolkit import config as config_mod; \\
from corpus_toolkit.mcp.framework import CorpusFramework; \\
CorpusFramework(config_mod.load('_meta/corpus.yml')).ensure_index()" \\
 && python3 -c "import corpus_toolkit.mcp.server" \\
 && corpus-mcp-serve --help >/dev/null
EXPOSE 8000
CMD ["corpus-mcp-serve"]
"""


def test_the_real_template_dockerfile_is_the_fixture(tmp_path):
    """BASELINE. If the template's Dockerfile stops looking like this fixture, every
    assertion below is testing a shape that no longer exists — which is the failure this
    whole issue is about, one level up."""
    # Resolved from the env var the gate job can set, then the two places a checkout
    # actually lands. The first version looked only in $HOME, which never exists on a
    # GitHub runner — so the alarm meant to fire when the template stops calling
    # `ensure_index` was DEAD in CI and green on the author's machine, which is how it got
    # written that way. The authoritative drift check is the gate step itself; this is the
    # fast one, and a fast check that cannot run is not a check.
    import os
    candidates = [os.environ.get("CORPUS_TEMPLATE"),
                  pathlib.Path(__file__).resolve().parent.parent.parent / "corpus-template",
                  pathlib.Path(__file__).resolve().parent.parent / "template",
                  pathlib.Path.home() / "corpus-template"]
    real = next((pathlib.Path(c) / "Dockerfile" for c in candidates
                 if c and (pathlib.Path(c) / "Dockerfile").is_file()), None)
    if real is None:
        pytest.skip("no corpus-template checkout found (set CORPUS_TEMPLATE to point at one)")

    run, skipped = gate.dockerfile_build_commands(real)

    assert any("ensure_index()" in c for c in run), (
        "the template no longer calls ensure_index at build time — if that is deliberate, "
        "this test and corpus-toolkit#100's premise both need revisiting")
    assert any("corpus_toolkit.mcp.server" in c for c in run)
    assert any(c.startswith("corpus-mcp-serve") for c in run)


def test_the_toolkit_facing_steps_are_extracted(tmp_path):
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE)

    run, _skipped = gate.dockerfile_build_commands(d)

    assert len(run) == 3
    assert "ensure_index()" in run[0]
    assert run[1] == 'python3 -c "import corpus_toolkit.mcp.server"'
    assert run[2] == "corpus-mcp-serve --help >/dev/null"


def test_container_only_steps_are_skipped_and_reported(tmp_path):
    """Skipping silently is how a gate ends up asserting less than it appears to. The
    caller prints these, so an operator can see what the gate chose not to run."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE)

    _run, skipped = gate.dockerfile_build_commands(d)

    assert any("apt-get update" in s for s in skipped)
    assert any("rm -rf /var/lib/apt/lists" in s for s in skipped)
    assert any("pip install" in s for s in skipped)


def test_an_unrecognised_step_fails_loudly(tmp_path):
    """THE POINT. A step this does not understand must stop the gate, not be dropped.

    If the template later chains something new into that RUN — a corpus-side build script,
    a new console script — the gate must say it does not know what to do rather than
    quietly skipping it and reporting success, which is precisely the class of green this
    issue exists to remove."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE.replace(
        ' && corpus-mcp-serve --help >/dev/null',
        ' && ./scripts/build_graph.py --check'))

    with pytest.raises(Exception) as e:
        gate.dockerfile_build_commands(d)

    assert "build_graph" in str(e.value)


def test_a_dockerfile_with_no_toolkit_steps_fails_rather_than_passing_empty(tmp_path):
    """An extractor that finds nothing and reports success is the exact defect being fixed:
    the gate would go green having run no build command at all."""
    d = tmp_path / "Dockerfile"
    d.write_text("FROM python:3.12-slim\nWORKDIR /repo\nCOPY . .\nCMD [\"corpus-mcp-serve\"]\n")

    with pytest.raises(Exception) as e:
        gate.dockerfile_build_commands(d)

    assert "no toolkit" in str(e.value).lower() or "found no" in str(e.value).lower()


def test_line_continuations_join_the_way_a_shell_joins_them(tmp_path):
    """A MISPARSE IS THE TRAP THIS ISSUE WARNED ABOUT, and the first version hit it.

    A shell removes `\\<newline>` entirely — it does not insert a space and does not eat the
    whitespace around it. Replacing the continuation with a space instead put a leading
    space inside the `python3 -c` payload:

        python3 -c " from corpus_toolkit import ..."
                    ^ IndentationError

    The command then failed for a reason that had nothing to do with the toolkit, which
    would have read as "this release breaks the template" on every single run — a gate
    that cries wolf is worth as little as one that stays silent."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE)

    run, _ = gate.dockerfile_build_commands(d)

    assert run[0].startswith('python3 -c "from corpus_toolkit'), run[0]
    assert '-c " ' not in run[0], "leading whitespace inside -c is an IndentationError"


def test_the_extracted_commands_actually_execute(tmp_path):
    """The end of the chain: extraction is only useful if what comes out RUNS.

    Asserted on the two steps that need no corpus on disk, so this stays a fast unit test;
    the `ensure_index()` step is covered end-to-end by the gate itself."""
    import subprocess
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE)

    run, _ = gate.dockerfile_build_commands(d)
    for step in [c for c in run if "ensure_index" not in c]:
        proc = subprocess.run(step, shell=True, capture_output=True, text=True)
        assert proc.returncode == 0, f"{step!r} -> {proc.returncode}: {proc.stderr[:200]}"


# ---------- hardening found in review: silence is the enemy, three more ways in ----------

def test_a_step_that_swallows_its_own_failure_is_refused(tmp_path):
    """Recognition was PREFIX-only, so anything appended after a recognised prefix ran
    unexamined — including a tail that makes the step always succeed.

    Not hypothetical: `oregon-counties/Dockerfile` already ships
    `python3 -c "…" || echo "WARNING…"`, deliberately non-fatal there, and corpus
    Dockerfiles derive from this template. A gate that runs that shape prints
    `OK: 1 build command(s)` having asserted precisely nothing — the exact green
    corpus-toolkit#100 exists to remove, rebuilt inside its own fix."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE.replace(
        ' && corpus-mcp-serve --help >/dev/null',
        ' || echo "WARNING: not warmed"'))

    with pytest.raises(Exception) as e:
        gate.dockerfile_build_commands(d)

    assert "||" in str(e.value) or "swallow" in str(e.value).lower()


@pytest.mark.parametrize("tail", ['; true', '& sleep 1', '# why: platform-deploy#2'])
def test_other_shell_tails_are_refused_too(tmp_path, tail):
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE.replace(
        ' && corpus-mcp-serve --help >/dev/null', f' && corpus-mcp-serve --help {tail}'))

    with pytest.raises(Exception):
        gate.dockerfile_build_commands(d)


def test_a_comment_line_ending_in_a_backslash_does_not_eat_the_next_instruction(tmp_path):
    """Docker strips comments BEFORE processing continuations; joining first does not.

    These Dockerfiles carry long comment blocks directly above the toolkit `RUN` — including
    ones with embedded shell examples. A comment line ending in `\\` glued the `RUN` onto the
    comment and the whole instruction vanished, leaving `to_run` non-empty from the OTHER
    RUN so the emptiness guard never fired."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE.replace(
        'RUN python3 -c "\\',
        '# see platform-deploy#2 — an image built green and crash-looped \\\nRUN python3 -c "\\'))

    run, _ = gate.dockerfile_build_commands(d)

    assert any("ensure_index()" in c for c in run), (
        "the toolkit RUN was swallowed by the comment above it")


def test_a_comment_inside_a_continued_run_does_not_truncate_the_chain(tmp_path):
    """Legal Docker, and it silently dropped every step after the comment."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_DOCKERFILE.replace(
        ' && python3 -c "import corpus_toolkit.mcp.server" \\',
        '# why: the CMD runs `server`, so the build must import it \\\n'
        ' && python3 -c "import corpus_toolkit.mcp.server" \\'))

    run, _ = gate.dockerfile_build_commands(d)

    assert len(run) == 3, f"chain truncated at a comment: {run}"


def test_run_followed_by_a_tab_is_not_skipped_in_silence(tmp_path):
    """Docker accepts any whitespace after the instruction. Requiring a literal space made
    the gate blind to the step rather than refusing it — and a step this cannot SEE is worse
    than one it refuses, because nothing says so."""
    d = tmp_path / "Dockerfile"
    d.write_text('FROM python:3.12-slim\nRUN\tpython3 -c "import corpus_toolkit.mcp.server"\n')

    run, _ = gate.dockerfile_build_commands(d)

    assert run == ['python3 -c "import corpus_toolkit.mcp.server"']
