"""The two-track pin shape, held in the workflow files themselves (ADR-0014).

These parse the YAML the corpora and the release process actually run. They exist because
the shape has drifted silently before: `toolkit-ref` was required and every corpus copied
the `uses:` tag into it; the release gate ran on every `v*` tag, which would have included
the floating `v1` the gate itself moves — and deleted it.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
REUSABLE = ["validate-frontmatter", "verify-provenance", "check-links", "detect-upstream-changes"]


def _load(name):
    d = yaml.safe_load((WF / f"{name}.yml").read_text())
    # PyYAML reads the bare key `on:` as boolean True.
    d["on"] = d.pop(True) if True in d else d.get("on")
    return d


def _steps(d):
    for job in d["jobs"].values():
        for step in job.get("steps", []):
            yield step


def test_toolkit_ref_is_optional_in_every_reusable_workflow():
    for name in REUSABLE:
        inp = _load(name)["on"]["workflow_call"]["inputs"]["toolkit-ref"]
        assert inp.get("required") is False, f"{name}: toolkit-ref must not be required"
        assert inp.get("default", "") == "", f"{name}: the default is the workflow's own commit, not a tag"


def test_the_toolkit_checkout_falls_back_to_the_workflows_own_commit():
    for name in REUSABLE:
        checkouts = [s for s in _steps(_load(name))
                     if str(s.get("uses", "")).startswith("actions/checkout")
                     and (s.get("with") or {}).get("repository") == "OregonAI/corpus-toolkit"]
        assert len(checkouts) == 1, f"{name}: expected exactly one toolkit checkout"
        ref = checkouts[0]["with"]["ref"]
        assert "github.job_workflow_sha" in ref and "inputs.toolkit-ref" in ref, \
            f"{name}: ref must be `inputs.toolkit-ref || github.job_workflow_sha`, got {ref!r}"


def _github_glob_to_regex(pattern: str) -> re.Pattern:
    """The subset of GitHub's filter-pattern syntax these workflows use."""
    out = ""
    for ch in pattern:
        out += {"*": ".*", "?": ".", ".": r"\."}.get(ch, ch)
    return re.compile("^" + out + "$")


def test_release_gate_ignores_the_floating_major_tag():
    tags = _load("release-gate")["on"]["push"]["tags"]
    regexes = [_github_glob_to_regex(t) for t in tags]
    assert any(r.match("v1.33.0") for r in regexes), "a release tag must trigger the gate"
    assert not any(r.match("v1") for r in regexes), \
        "the floating `v1` the gate moves must not re-trigger it (version-matches-tag would delete it)"
    assert not any(r.match("v2") for r in regexes)


def test_release_gate_has_the_canary_and_advances_the_major_tag_only_after_it():
    jobs = _load("release-gate")["jobs"]
    assert "plan" in jobs and "canary" in jobs and "advance-major-tag" in jobs
    assert "fromJSON(needs.plan.outputs.canary)" in str(jobs["canary"]["strategy"]["matrix"]["repo"])
    assert set(jobs["advance-major-tag"]["needs"]) >= {"corpus-end-to-end", "version-matches-tag", "canary"}
    assert "needs.canary.result == 'success'" in jobs["advance-major-tag"]["if"]
    assert "canary" in jobs["unpublish-a-failed-tag"]["needs"], "a red canary unpublishes the tag"
    unpub_if = jobs["unpublish-a-failed-tag"]["if"]
    assert "cancelled" in unpub_if and "failure" in unpub_if, \
        "a canary leg killed by its own timeout is `cancelled`, not `failure`, and must still unpublish"
    assert jobs["canary"]["timeout-minutes"] >= 60, \
        "ERF's full provenance alone is 27-29 min (measured 2026-09-01..03); 30 cancelled the leg"
    run = jobs["advance-major-tag"]["steps"][-1]["run"]
    assert "refs/tags/$MAJOR" in run and "--force" in run


def test_held_corpora_are_reported_not_blocking():
    canary = _load("release-gate")["jobs"]["canary"]
    gates = [s for s in canary["steps"] if str(s.get("run", "")).startswith("corpus-")]
    assert len(gates) == 3, "frontmatter, provenance, relationships — the three the reusable workflows run"
    for s in gates:
        assert "steps.track.outputs.state != 'floating'" in str(s.get("continue-on-error")), s["name"]


def test_propagate_pin_runs_after_the_gate_not_on_the_tag():
    d = _load("propagate-pin")
    assert "push" not in d["on"], "a tag push must not fan out before the gate has passed"
    assert d["on"]["workflow_run"]["workflows"] == ["release-gate"]
    plan_if = d["jobs"]["plan"]["if"]
    assert "vars.PROPAGATE_PINS == 'true'" in plan_if
    assert "workflow_run.conclusion == 'success'" in plan_if
    assert "fromJSON(needs.plan.outputs.pins)" in str(d["jobs"]["propagate"]["strategy"]["matrix"]["repo"])


def test_propagate_pin_requests_auto_merge_and_refuses_over_a_stale_pr():
    steps = list(_steps(_load("propagate-pin")))
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "gh pr merge --auto" in runs
    assert "toolkit-pin-" in runs and "still has a bump PR open from an earlier release" in runs
