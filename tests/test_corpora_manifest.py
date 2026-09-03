"""The corpora manifest (ADR-0008) and the two matrices generated from it (ADR-0014).

Nine files across four repos used to name which corpora exist, and at least one was wrong
in both directions (corpus-toolkit#83). The manifest is now the one tooling list. These
tests hold it to the shape the release gate and propagate-pin rely on; propagate-pin's own
preflight holds it to the ORG, which a unit test cannot see.
"""
import json
import pathlib

import pytest

from corpus_toolkit import corpora


def test_the_shipped_manifest_validates():
    repos = corpora.load_manifest()
    assert len(repos) >= 10


def test_eight_live_corpora_as_context_md_says():
    """CONTEXT.md: 'Eight live'. corpus-gateway's registry holds eight. This is the count
    that has been written wrong more than once (a memory note records 'nine' three times),
    so the manifest states it and this test pins it."""
    assert len(corpora.live_corpora(corpora.load_manifest())) == 8


def test_canary_targets_are_live_public_corpora_on_the_ci_track():
    repos = corpora.load_manifest()
    by = {r["name"]: r for r in repos}
    targets = corpora.canary_targets(repos)
    assert set(targets) == set(corpora.live_corpora(repos)), \
        "every live corpus is public and on the CI track today, so the canary covers all eight"
    for name in targets:
        r = by[name]
        assert r["visibility"] == "public" and r["status"] == "live" and r["ci_track"]
    assert "corpus-template" not in targets, "the template is proven by instantiation, not by the canary"
    assert "oregon-records-retention" not in targets


def test_pin_targets_include_the_consumer_tier_and_the_template():
    targets = set(corpora.pin_targets(corpora.load_manifest()))
    assert {"corpus-gateway", "corpus-chat", "oregon-stories", "corpus-template"} <= targets
    assert "corpus-toolkit" not in targets and "oregon-records-retention" not in targets


def test_no_private_repo_is_a_canary_target():
    repos = corpora.load_manifest()
    private = {r["name"] for r in repos if r["visibility"] == "private"}
    assert not private & set(corpora.canary_targets(repos))


def _manifest(*entries):
    base = dict(tier="corpus", visibility="public", status="live", ci_track=True, toolkit_pin=True)
    return {"repos": [{**base, **e} for e in entries]}


def test_validate_rejects_a_private_ci_track_repo():
    with pytest.raises(corpora.ManifestError, match="private repo cannot be on the CI track"):
        corpora.load_manifest(json.dumps(_manifest({"name": "x", "visibility": "private"})))


def test_validate_rejects_duplicates_unknown_tiers_and_missing_fields_together():
    bad = _manifest({"name": "a"}, {"name": "a", "tier": "sideways"})
    del bad["repos"][1]["toolkit_pin"]
    with pytest.raises(corpora.ManifestError) as e:
        corpora.load_manifest(json.dumps(bad))
    msg = str(e.value)
    assert "duplicate name" in msg and "tier must be one of" in msg and "missing toolkit_pin" in msg


def test_validate_rejects_a_retired_repo_that_is_still_bumped_or_canaried():
    with pytest.raises(corpora.ManifestError, match="retired repo is neither"):
        corpora.load_manifest(json.dumps(_manifest({"name": "x", "status": "retired"})))


def _workflows(tmp_path, **files):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in files.items():
        (wf / name).write_text(text)
    return tmp_path


USES = "    uses: OregonAI/corpus-toolkit/.github/workflows/{}.yml@{}\n"


def test_ci_track_state_floating(tmp_path):
    repo = _workflows(tmp_path, **{"ci.yml": USES.format("validate-frontmatter", "v1") + USES.format("check-links", "v1")})
    assert corpora.ci_track_state(repo)["state"] == "floating"


def test_ci_track_state_held(tmp_path):
    repo = _workflows(tmp_path, **{"ci.yml": USES.format("validate-frontmatter", "v1.31.1")})
    s = corpora.ci_track_state(repo)
    assert s["state"] == "held" and s["exact"] == ["v1.31.1"]


def test_ci_track_state_mixed_is_its_own_answer(tmp_path):
    """The partial bump: some calls floated, some not. Not collapsed into either."""
    repo = _workflows(tmp_path, **{"ci.yml": USES.format("validate-frontmatter", "v1"),
                                   "scheduled.yml": USES.format("detect-upstream-changes", "v1.30.0")})
    assert corpora.ci_track_state(repo)["state"] == "mixed"


def test_ci_track_state_none_when_no_reusable_workflow_is_called(tmp_path):
    repo = _workflows(tmp_path, **{"tests.yml": "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n"})
    assert corpora.ci_track_state(repo)["state"] == "none"


def test_cli_prints_json_matrices(capsys):
    assert corpora.main(["--canary-matrix"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list) and "executive-regulatory-frameworks" in out
    assert corpora.main(["--pin-matrix"]) == 0
    assert "corpus-chat" in json.loads(capsys.readouterr().out)
    assert corpora.main(["--names", "--status", "retired"]) == 0
    assert json.loads(capsys.readouterr().out) == ["oregon-records-retention"]
