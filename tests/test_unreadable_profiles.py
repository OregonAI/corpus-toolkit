"""A declared curated-profiles file this corpus CANNOT READ is one condition, however it
fails, and it is reported rather than raised (corpus-toolkit#143).

THE SECOND FILE, WHICH DID NOT GET corpus-toolkit#136's TREATMENT. `issuing_body_profile`
reads two files back to back. The issuing-body registry goes through
`read_issuing_body_registry`, so gone / unopenable / unparseable / wrongly-shaped all come
back as one reported condition. The curated overlay two lines later parsed inline:

    curated = (yaml.safe_load(self.config.issuing_body_profiles.read_text()) or {}).get(
        "profiles", {})

`read_text()` raises `PermissionError` or `UnicodeDecodeError`, `safe_load` raises
`ParserError`, and `.get` raises `AttributeError` for a file that parses to a list or a
string. The `is_file()` in front of it guards only ABSENCE — the one failure mode that was
never the problem.

TWO FINDINGS THAT MUST NOT COLLAPSE INTO EACH OTHER (CONTEXT.md: "could not check" is
never reported as "is not there"):

  * this body HAS NO CURATED NOTES  -> the overlay was read, and it says nothing about it
  * the overlay COULD NOT BE READ   -> nobody knows, and the reason names file and key

AND THE PROFILES FILE IS NOT THE REGISTRY. Different file, different config key, different
fix. The registry may be perfectly readable while the overlay is not, so an unreadable
overlay must not degrade the answer into "that body is not registered" — the registry
identity, holdings and attribution are served, and the response states the limit.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit.config import load as load_config              # noqa: E402
from corpus_toolkit.mcp.framework import CorpusFramework           # noqa: E402

DAS = "department-of-administrative-services"

CONFIG = """
    schema_version: 1
    corpus:
      id: test-unreadable-profiles
      name: Test Unreadable Profiles
      jurisdiction: oregon
      archetype: document
      authoritative_source: "https://sos.oregon.gov/archives/"
    content_roots:
      - path: "policies"
        doc_type: "policy"
    plugins:
      issuing_body_registry: "_meta/registry.yml"
      issuing_body_registry_key: "entries"
      issuing_body_profiles: "_meta/profiles.yml"
"""

DOC = """---
schema_version: 1
id: pol-1
title: "Policy 1"
doc_type: policy
citation: "POL 1"
authority_level: agency_policy
issuing_body: "Department of Administrative Services"
source_url: "https://sos.oregon.gov/archives/pol-1"
source_format: html
retrieved: "2026-08-20"
source_sha256: "{sha}"
status: current
content_mode: verbatim
last_verified: "2026-08-20"
verified_by: "@test"
tags: ["t"]
---

## At a glance

A policy about water.

## Full text

The department shall issue a water permit on application.
""".format(sha="1" * 64)

# A registry that reads PERFECTLY. Every test here is about the other file.
REGISTRY = json.dumps({"entries": [
    {"slug": DAS, "name": "Administrative Services, Department of"}]})

# Unclosed flow mapping — the exact shape #143 was reproduced with.
MALFORMED = "profiles: { das: {note: a\n"

GOOD_PROFILES = json.dumps({"profiles": {DAS: {"note": "The state's central service agency."}}})


def _corpus(tmp_path: Path, profiles: str | None, *, declare: bool = True,
            with_documents: bool = True) -> Path:
    """A corpus with a readable registry and whatever `profiles` says about the overlay.

    `with_documents=False` LEAVES THE DOCUMENTS OUT, for the validator guards below. The seam
    they test is the corpus-level config check, which runs whether or not the corpus holds
    documents, and this fixture's one policy does not satisfy the bundled frontmatter
    schema — so a run over it exits non-zero on `main` already, and a guard asserting
    "the validator fails" would pass without the config check existing at all. Keeping the
    content out is what makes the exit code mean this finding.
    """
    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    if with_documents:
        (tmp_path / "policies" / "p-1.md").write_text(DOC)
    (tmp_path / "_meta").mkdir(exist_ok=True)
    config = textwrap.dedent(CONFIG).strip() + "\n"
    if not declare:
        config = config.replace('  issuing_body_profiles: "_meta/profiles.yml"\n', "")
    (tmp_path / "_meta" / "corpus.yml").write_text(config)
    (tmp_path / "_meta" / "registry.yml").write_text(REGISTRY)
    if profiles is not None:
        (tmp_path / "_meta" / "profiles.yml").write_text(profiles)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def _config(root: Path):
    return load_config(str(root / "_meta" / "corpus.yml"))


def _fw(root: Path) -> CorpusFramework:
    return CorpusFramework(_config(root))


def test_a_malformed_profiles_file_is_answered_not_raised(tmp_path):
    """THE BUG. A malformed line in the OPTIONAL half of this tool's answer took the whole
    call down — registry identity, holdings and attribution lost to a file whose ABSENCE
    would have cost nothing."""
    out = _fw(_corpus(tmp_path, MALFORMED)).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert out["registry"]["name"] == "Administrative Services, Department of"
    assert "in_repo" in out and "attribution" in out, out
    assert out["corpus"], "every return path carries the envelope (corpus-toolkit#38)"


def test_the_response_says_the_overlay_could_not_be_read_and_why(tmp_path):
    """SERVING `curated: {}` SILENTLY IS THE OTHER HALF OF THE DEFECT. "This body has no
    curated notes" and "this corpus's curated notes could not be read" are two findings
    with two different fixes, and only the second one is true here."""
    out = _fw(_corpus(tmp_path, MALFORMED)).issuing_body_profile(DAS)

    assert out["curated"] == {}
    warning = out.get("curated_warning")
    assert warning, f"an unreadable overlay served curated: {{}} in silence: {out}"
    assert "could not be read" in warning, warning
    assert "ParserError" in warning, warning


def test_the_warning_names_the_profiles_file_and_the_key_that_declared_it(tmp_path):
    """"WHICH FILE" IS HALF AN ANSWER — an operator needs the config key to change. And it
    is NOT the registry's key: reporting a broken overlay as a broken registry sends the
    fix to the wrong file."""
    out = _fw(_corpus(tmp_path, MALFORMED)).issuing_body_profile(DAS)
    warning = out.get("curated_warning") or ""

    assert "plugins.issuing_body_profiles" in warning, warning
    assert "_meta/profiles.yml" in warning, warning
    assert "plugins.issuing_body_registry" not in warning, warning
    assert "_meta/registry.yml" not in warning, warning


def test_an_unreadable_overlay_is_not_reported_as_this_body_being_unregistered(tmp_path):
    """THE ANSWER MUST NOT DEGRADE INTO A CLAIM ABOUT THE BODY. The registry here reads
    perfectly; only the optional overlay is missing. `issuing_body_profile`'s no-match and
    broken-registry wordings are what this must never become."""
    out = _fw(_corpus(tmp_path, MALFORMED)).issuing_body_profile(DAS)

    assert "error" not in out, out
    assert "candidates" not in out, out
    assert out["slug"] == DAS


def test_a_readable_overlay_is_served_and_carries_no_warning(tmp_path):
    """THE GUARD MUST NOT FIRE ON A WORKING CORPUS. A response that always reported a fault
    would satisfy every paragraph above and check nothing — and a curated note on every
    response is a note every agent learns to ignore."""
    out = _fw(_corpus(tmp_path, GOOD_PROFILES)).issuing_body_profile(DAS)

    assert out["curated"] == {"note": "The state's central service agency."}
    assert "curated_warning" not in out, out
    assert _config(_corpus(tmp_path, GOOD_PROFILES)).issuing_body_profiles_fault is None


def test_a_corpus_declaring_no_overlay_says_nothing_about_one(tmp_path):
    """DECLARING NONE IS A CHOICE, NOT A FAULT. A corpus with no `issuing_body_profiles`
    key served `curated: {}` quite happily before this change and must keep doing so,
    without acquiring a warning about a file it never claimed to have."""
    out = _fw(_corpus(tmp_path, None, declare=False)).issuing_body_profile(DAS)

    assert out["curated"] == {}
    assert "curated_warning" not in out, out


def test_an_overlay_that_parses_to_a_list_does_not_raise_attributeerror(tmp_path):
    """`.get("profiles", {})` ASSUMES A MAPPING. A file that parses to a list raises
    `AttributeError` on the same line — a traceback naming neither the file nor the key,
    out of a live tool."""
    out = _fw(_corpus(tmp_path, "- one\n- two\n")).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert out["curated"] == {}
    assert "list" in (out.get("curated_warning") or ""), out


def test_an_overlay_that_parses_to_a_string_does_not_raise_attributeerror(tmp_path):
    """The same defect wearing its other coat: a scalar document is a `str`, which has no
    `.get` either. Both are "shaped like something other than a profiles file"."""
    out = _fw(_corpus(tmp_path, "just some prose\n")).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert out["curated"] == {}
    assert "str" in (out.get("curated_warning") or ""), out


def test_a_profiles_key_that_is_not_a_mapping_is_reported_not_served(tmp_path):
    """`profiles:` HOLDS SLUG -> NOTES. A list under it makes `curated.get(slug, {})` raise
    `AttributeError` too, one level down, and serving it as an empty overlay would report a
    misshapen file as a corpus with nothing curated."""
    out = _fw(_corpus(tmp_path, json.dumps({"profiles": ["das"]}))).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert out["curated"] == {}
    assert "list" in (out.get("curated_warning") or ""), out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file anyway")
def test_an_overlay_that_cannot_be_opened_is_reported_too(tmp_path):
    """PARSE FAILURE IS NOT THE ONLY WAY TO FAIL A READ. Permission is the one that happens
    in a container, and it lands in the same place — the condition is "this corpus declares
    an overlay it cannot read", not "the YAML is bad"."""
    root = _corpus(tmp_path, GOOD_PROFILES)
    (root / "_meta" / "profiles.yml").chmod(0o000)

    out = _fw(root).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert "PermissionError" in (out.get("curated_warning") or ""), out


def test_an_overlay_that_is_not_text_is_reported_too(tmp_path):
    """AN OVERLAY IS READ AS TEXT, AND NOT EVERY FILE IS. A latin-1 or binary file raises
    `UnicodeDecodeError` out of `read_text()` — a `ValueError`, so an `OSError`/`YAMLError`
    catch misses it and the raise escapes into whatever asked."""
    root = _corpus(tmp_path, GOOD_PROFILES)
    (root / "_meta" / "profiles.yml").write_bytes(b"profiles:\n  caf\xe9: {}\n")

    out = _fw(root).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert "UnicodeDecodeError" in (out.get("curated_warning") or ""), out


def test_a_declared_overlay_that_is_not_there_is_a_fault_not_a_silence(tmp_path):
    """A DECLARED FILE THAT IS GONE IS THE FOURTH WAY TO FAIL A READ, and the registry
    reader has always treated it as one. `is_file()` used to swallow it, so a corpus
    pointing at a path nobody created reported "this body has no curated notes" — a
    configuration error rendered as a fact about the body."""
    out = _fw(_corpus(tmp_path, None)).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert out["curated"] == {}
    assert "_meta/profiles.yml" in (out.get("curated_warning") or ""), out


# ---- the config seam: one read, one wording ------------------------------------------

def test_the_overlay_is_read_through_the_config_like_the_registry_is(tmp_path):
    """ONE READER, ON THE CONFIG, CACHED — the shape corpus-toolkit#136 landed for the
    registry. A second reader written inline at a call site is how the two files came to
    disagree about what "unreadable" means in the first place."""
    config = _config(_corpus(tmp_path, MALFORMED))

    read = config.issuing_body_profiles_read

    assert read.readable is False
    assert read.profiles is None, "could not read is never the same answer as an empty overlay"
    assert "ParserError" in (read.problem or "")


def test_an_empty_overlay_is_read_and_is_not_unknown(tmp_path):
    """AN OVERLAY THAT HOLDS NOTHING IS NOT AN OVERLAY NOBODY COULD OPEN. It was read; the
    answer to "does this corpus curate notes for this body" is a genuine no."""
    config = _config(_corpus(tmp_path, "profiles: {}\n"))

    assert config.issuing_body_profiles_read.readable is True
    assert config.issuing_body_profiles_read.profiles == {}
    assert config.issuing_body_profiles_fault is None


def test_the_fault_sentence_is_declared_once_and_is_not_the_registrys(tmp_path):
    """TWO FILES, TWO KEYS, TWO SENTENCES. Borrowing the registry's wording for the overlay
    is the "one fact declared twice with nothing gating agreement" shape this project has
    hit six times; the fix for a broken overlay is a different file from the fix for a
    broken registry, and the sentence has to say which."""
    config = _config(_corpus(tmp_path, MALFORMED))

    fault = config.issuing_body_profiles_fault

    assert fault, "a declared overlay that cannot be read must carry its reason"
    assert fault.startswith("plugins.issuing_body_profiles "), fault
    assert "_meta/profiles.yml" in fault, fault
    assert config.issuing_body_registry_fault is None, (
        "the registry here reads perfectly; a broken overlay must not be reported as one")


# ---- the operator's two surfaces: CI, and the server's startup line -------------------
#
# THE FAULT REACHED ONE READER, AND IT WAS THE WRONG ONE (corpus-toolkit#150).
# `issuing_body_registry_fault` is read by the validator (a CI error), by `build_server`
# (one stderr line to the operator) and by the per-call notes. After corpus-toolkit#143
# `issuing_body_profiles_fault` was read by the per-call note ALONE — so a malformed
# overlay merged through a green CI, deployed, and served every body with silently empty
# curated data until somebody read one tool response closely.
#
# The decision is full parity with the registry: declaring the key is optional, but a
# DECLARED file that cannot be read is a config defect. Not fatal at load — the loader is
# deliberately tolerant so a pin mismatch degrades rather than dies.


def _validate(root: Path) -> subprocess.CompletedProcess:
    """Run the validator the way a corpus's CI does, from the corpus root.

    THIS interpreter and THIS checkout, pinned: `python3` plus a bare cwd would run
    whichever `corpus_toolkit` happened to be importable there, so the guard could pass
    against an install that does not contain the code under test.
    """
    return subprocess.run(
        [sys.executable, "-m", "corpus_toolkit.validate.frontmatter",
         "--config", "_meta/corpus.yml"],
        cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})


def test_an_unreadable_profiles_file_fails_the_validator(tmp_path):
    """THE BUG, AT THE GATE. A corpus could commit a malformed overlay and merge it on
    green: the validator never asked the config whether the file it declared could be
    read, so the one check that runs on every PR passed without checking anything."""
    out = _validate(_corpus(tmp_path, MALFORMED, with_documents=False))

    assert out.returncode != 0, out.stdout + out.stderr
    assert "FAILED" in out.stdout, out.stdout


def test_the_validator_finding_names_the_profiles_file_and_its_key(tmp_path):
    """"SOMETHING IS WRONG" IS NOT A FINDING. The operator has to know which file to open
    and which key declared it — and it is NOT the registry's key or the registry's file.
    The registry here reads perfectly; naming it would send the fix to the wrong file."""
    out = _validate(_corpus(tmp_path, MALFORMED, with_documents=False))

    assert "plugins.issuing_body_profiles" in out.stdout, out.stdout
    assert "_meta/profiles.yml" in out.stdout, out.stdout
    assert "could not be read" in out.stdout, out.stdout
    assert "plugins.issuing_body_registry" not in out.stdout, out.stdout
    assert "_meta/registry.yml" not in out.stdout, out.stdout


def test_a_corpus_whose_overlay_reads_fine_gets_no_finding(tmp_path):
    """THE GUARD MUST NOT FIRE ON A WORKING CORPUS. A finding printed for every corpus that
    declares an overlay is a finding every maintainer learns to scroll past, and a gate
    that refuses a correct config is worse than no gate — a blanket "report the overlay"
    would satisfy both guards above and check nothing."""
    out = _validate(_corpus(tmp_path, GOOD_PROFILES, with_documents=False))

    assert out.returncode == 0, out.stdout + out.stderr
    assert "issuing_body_profiles" not in out.stdout, out.stdout


def test_a_corpus_declaring_no_overlay_is_not_told_about_one(tmp_path):
    """DECLARING NONE IS A CHOICE, NOT A FAULT — the rule the registry follows, and the
    reason this can be an error at all. A corpus with no `plugins.issuing_body_profiles`
    key must validate exactly as it did before, with nothing said about a file it never
    claimed to have."""
    out = _validate(_corpus(tmp_path, None, declare=False, with_documents=False))

    assert out.returncode == 0, out.stdout + out.stderr
    assert "issuing_body_profiles" not in out.stdout, out.stdout


def test_the_operator_is_told_at_startup_which_overlay_is_broken(tmp_path):
    """SAID ONCE, TO THE OPERATOR, because the per-call answer only reaches the agent —
    the registry's startup line, for the file beside it. A corpus deploying a malformed
    overlay serves every body with an empty curated block, and nothing on the way in said
    so: the `curated_warning` reaches whoever calls `issuing_body_profile`, never the
    person who can edit the file."""
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        server_mod.build_server(_config(_corpus(tmp_path, MALFORMED)))

    said = captured.getvalue()
    assert "plugins.issuing_body_profiles" in said, said
    assert "_meta/profiles.yml" in said, said
    assert "could not be read" in said, said


def test_the_server_still_starts_with_an_overlay_it_cannot_read(tmp_path):
    """A WARNING, NOT A REFUSAL. The overlay is the optional half of ONE tool's answer, so
    a corpus that cannot read it loses its curated notes and keeps everything else —
    refusing to start would cost it every other question as well. The config loader is
    tolerant for the same reason: a pin bump must degrade a corpus, not take it down."""
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod

    with contextlib.redirect_stderr(io.StringIO()):
        mcp = server_mod.build_server(_config(_corpus(tmp_path, MALFORMED)))

    assert "issuing_body_profile" in {t.name for t in mcp._tool_manager.list_tools()}


def test_a_corpus_whose_overlay_reads_gets_no_startup_warning(tmp_path):
    """THE GUARD MUST NOT FIRE ON A WORKING CORPUS. A startup line printed for every corpus
    is a line every operator learns to ignore, which is the same as printing nothing."""
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        server_mod.build_server(_config(_corpus(tmp_path, GOOD_PROFILES)))

    assert "issuing_body_profiles" not in captured.getvalue(), captured.getvalue()


def test_one_sentence_reaches_all_three_surfaces_verbatim(tmp_path):
    """DECLARED ONCE, READ THREE TIMES — the shape the registry has had since
    corpus-toolkit#136, and the reason this issue is two call sites and not two messages.
    "One fact declared twice with nothing gating agreement" is a pattern this project has
    hit six times; a re-wording at either new call site is how the validator ends up
    naming the file while the startup line names only the key, and an operator gets half
    an answer twice.

    The per-call `curated_warning` from corpus-toolkit#143 is the third surface, and it is
    asserted here unchanged."""
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod

    root = _corpus(tmp_path, MALFORMED)
    sentence = _config(root).issuing_body_profiles_fault
    assert sentence, "the fault this test is about"

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        server_mod.build_server(_config(root))
    validated = _validate(_corpus(tmp_path / "again", MALFORMED, with_documents=False))
    curated_warning = _fw(root).issuing_body_profile(DAS)["curated_warning"]

    for surface, said in (("startup stderr", captured.getvalue()),
                          ("validator finding", validated.stdout),
                          ("per-call note", curated_warning)):
        assert sentence in said, f"{surface} re-words the fault: {said}"


def test_the_finding_gates_the_relationships_entry_point_too(tmp_path):
    """A CORPUS-LEVEL FACT IS GATED ON EVERY ENTRY POINT (corpus-toolkit#139). A corpus
    whose CI is trimmed to `--check-relationships` must not be the corpus this finding
    cannot reach — "the gate exists in a command no corpus's CI actually invokes" is the
    shape that made #139 a bug."""
    root = _corpus(tmp_path, MALFORMED, with_documents=False)

    out = subprocess.run(
        [sys.executable, "-m", "corpus_toolkit.validate.frontmatter",
         "--config", "_meta/corpus.yml", "--check-relationships"],
        cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})

    assert out.returncode != 0, out.stdout + out.stderr
    assert "plugins.issuing_body_profiles" in out.stdout, out.stdout
