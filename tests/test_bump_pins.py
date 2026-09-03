"""Moving the SERVING pin, and leaving the CI track alone (corpus-toolkit#9, ADR-0014).

A corpus names the toolkit in two tracks. The CI track — `uses: …@v1` — floats on the major
tag the release gate advances after its canary; the serving track — the git URL in
requirements*.txt — is exact, so an image builds the same twice. bump_pins.py moves the
second and must never touch the first: a corpus that pins an exact tag in `uses:` has
HELD itself back on purpose, and a bumper that "helpfully" un-held it would silently undo
that decision on the next release.

The older hazard is still tested first: a regex loose enough to rewrite
`actions/checkout@v4` to `@v1.33.0` would be a far worse day than any drift.
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "bump_pins",
    pathlib.Path(__file__).resolve().parent.parent / ".github" / "scripts" / "bump_pins.py")
bump_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_pins)


WORKFLOW_ON_TRACK = """\
jobs:
  frontmatter:
    uses: OregonAI/corpus-toolkit/.github/workflows/validate-frontmatter.yml@v1
  generated:
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
"""

WORKFLOW_HELD = """\
jobs:
  frontmatter:
    uses: OregonAI/corpus-toolkit/.github/workflows/validate-frontmatter.yml@v1.31.1
    with:
      toolkit-ref: v1.31.1
  generated:
    steps:
      - uses: actions/checkout@v4
        with:
          repository: OregonAI/corpus-toolkit
          ref: v1.31.1
          path: .toolkit
"""

REQUIREMENTS = (
    "corpus-toolkit[mcp,semantic] @ git+https://github.com/OregonAI/corpus-toolkit@v1.31.1\n"
    "requests==2.32.3\n")


def test_third_party_action_pins_are_never_rewritten():
    """The expensive mistake. `@v4` and `@v5` belong to other owners on other cadences."""
    out, n = bump_pins.rewrite(WORKFLOW_ON_TRACK, "v1.33.0")
    assert out == WORKFLOW_ON_TRACK and n == 0


def test_a_held_corpus_stays_held():
    """Exact `uses:`/`toolkit-ref:`/checkout pins are the corpus's decision, not a pin to move."""
    out, n = bump_pins.rewrite(WORKFLOW_HELD, "v1.33.0")
    assert out == WORKFLOW_HELD and n == 0


def test_the_serving_pin_moves():
    out, n = bump_pins.rewrite(REQUIREMENTS, "v1.33.0")
    assert "corpus-toolkit@v1.33.0" in out
    assert "requests==2.32.3" in out
    assert n == 1


def test_the_dot_git_spelling_moves_too():
    out, n = bump_pins.rewrite(
        "corpus-toolkit @ git+https://github.com/OregonAI/corpus-toolkit.git@v1.30.0\n", "v1.33.0")
    assert "corpus-toolkit.git@v1.33.0" in out and n == 1


def test_rewrite_is_idempotent():
    once, _ = bump_pins.rewrite(REQUIREMENTS, "v1.33.0")
    twice, n = bump_pins.rewrite(once, "v1.33.0")
    assert twice == once and n == 0


def test_scan_reads_requirements_only(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(WORKFLOW_HELD)
    (tmp_path / "requirements.txt").write_text(REQUIREMENTS)
    (tmp_path / "requirements-build.txt").write_text(
        "corpus-toolkit[mcp] @ git+https://github.com/OregonAI/corpus-toolkit@v1.30.0\n")
    found = bump_pins.scan(tmp_path)
    assert {p.name for p in found} == {"requirements.txt", "requirements-build.txt"}, \
        "workflow files are the CI track and are not scanned"
    versions = {v for vs in found.values() for v in vs}
    assert versions == {"v1.31.1", "v1.30.0"}, "two requirements files disagreeing is the drift --check reports"


def test_scan_is_quiet_when_the_serving_pin_agrees(tmp_path):
    (tmp_path / "requirements.txt").write_text(REQUIREMENTS)
    found = bump_pins.scan(tmp_path)
    assert {v for vs in found.values() for v in vs} == {"v1.31.1"}
    assert len(found) == 1
