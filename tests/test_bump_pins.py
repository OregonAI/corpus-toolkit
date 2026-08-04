"""Moving toolkit pins, and detecting when they disagree (corpus-toolkit#9).

Every toolkit tag obliges a manual edit in every corpus, and each corpus pins in at least
two places per workflow call — `uses:` selects the workflow FILE, `toolkit-ref:` selects
the CODE it installs. They are separate knobs on purpose and drift silently.

Measured 2026-08-04 across 10 repos: 126 pin sites, six toolkit versions in use, and
oregon-legislature running `verify-provenance` at v1.21.0 while `validate-frontmatter` and
`check-links` in the SAME ci.yml ran at v1.19.0. Nothing failed; nothing could.

The hazard in automating this is a regex loose enough to rewrite `actions/checkout@v4` to
`@v1.23.0`, which would be a far worse day than the drift. That case is tested first.
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "bump_pins",
    pathlib.Path(__file__).resolve().parent.parent / ".github" / "scripts" / "bump_pins.py")
bump_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_pins)


WORKFLOW = """\
jobs:
  frontmatter:
    uses: OregonAI/corpus-toolkit/.github/workflows/validate-frontmatter.yml@v1.19.0
    with:
      toolkit-ref: v1.19.0
  generated:
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          repository: OregonAI/corpus-toolkit
          ref: v1.19.0
          path: .toolkit
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
"""

REQUIREMENTS = (
    "corpus-toolkit[mcp,semantic] @ git+https://github.com/OregonAI/corpus-toolkit@v1.19.0\n"
    "requests==2.32.3\n")


def test_third_party_action_pins_are_never_rewritten():
    """The expensive mistake. `@v4` and `@v5` belong to other owners on other cadences."""
    out, n = bump_pins.rewrite(WORKFLOW, "v1.23.0")
    assert "actions/checkout@v4" in out
    assert "actions/setup-python@v5" in out
    assert "@v1.23.0" not in out.split("actions/checkout@")[1][:8]


def test_all_three_toolkit_pin_shapes_move():
    out, n = bump_pins.rewrite(WORKFLOW, "v1.23.0")
    assert "validate-frontmatter.yml@v1.23.0" in out
    assert "toolkit-ref: v1.23.0" in out
    assert "ref: v1.23.0" in out, "the toolkit checkout's own ref is the third knob"
    assert n == 3
    assert "v1.19.0" not in out


def test_a_bare_ref_not_under_a_toolkit_checkout_is_left_alone():
    """`ref:` alone says nothing — actions/checkout uses it for the corpus's own code."""
    text = ("      - uses: actions/checkout@v4\n"
            "        with:\n"
            "          repository: OregonAI/oregon-budget\n"
            "          ref: v1.19.0\n")
    out, n = bump_pins.rewrite(text, "v1.23.0")
    assert n == 0 and "ref: v1.19.0" in out


def test_requirements_git_url_moves():
    out, n = bump_pins.rewrite(REQUIREMENTS, "v1.23.0")
    assert "corpus-toolkit@v1.23.0" in out
    assert "requests==2.32.3" in out
    assert n == 1


def test_rewrite_is_idempotent():
    once, _ = bump_pins.rewrite(WORKFLOW, "v1.23.0")
    twice, n = bump_pins.rewrite(once, "v1.23.0")
    assert twice == once and n == 0


def test_check_detects_two_versions_in_one_repo(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(WORKFLOW.replace(
        "validate-frontmatter.yml@v1.19.0", "validate-frontmatter.yml@v1.21.0"))
    found = bump_pins.scan(tmp_path)
    versions = {v for vs in found.values() for v in vs}
    assert versions == {"v1.19.0", "v1.21.0"}, "the drift the org actually had"


def test_check_is_quiet_when_every_pin_agrees(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(WORKFLOW)
    (tmp_path / "requirements.txt").write_text(REQUIREMENTS)
    found = bump_pins.scan(tmp_path)
    assert {v for vs in found.values() for v in vs} == {"v1.19.0"}
    assert len(found) == 2, "workflows and requirements.txt are both scanned"
