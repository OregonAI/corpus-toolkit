"""A declared issuing-body registry this corpus CANNOT READ is one condition, however it
fails, and it is reported rather than raised (corpus-toolkit#136).

THE FAILURE THIS CLOSES. `CorpusConfig.issuing_body_slugs` answered `None` — the
documented "could not check" value that `documents_by_agency`'s `slug_in_registry: null`
and `search_corpus`'s `registry_checked: false` both rest on — only when the declared path
was NOT A FILE. A registry that IS a file and does not parse raised `yaml.ParserError`
straight out of whatever asked, including a live MCP tool call. Same input class from a
caller's point of view (this corpus declares a registry it cannot read), two answers: one
a clean finding, one a traceback out of a running server.

THREE FINDINGS THAT MUST NOT COLLAPSE INTO EACH OTHER, and this file's job is that they
stay apart (CONTEXT.md: "could not check" is never reported as "is not there"):

  * the registry COULD NOT BE READ  -> unknown, and the reason names the file and the key
  * the registry is EMPTY           -> read, and it holds no bodies
  * this body IS NOT IN it          -> read, and the answer is no

`corpus-validate-frontmatter` has answered this question since corpus-toolkit#129 through
`RegistryRead`. The runtime path now asks the SAME reader rather than growing a second
spelling of "unreadable" — one fact declared twice with nothing gating agreement is the
shape of five separate defects in this project.
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
      id: test-unreadable
      name: Test Unreadable
      jurisdiction: oregon
      archetype: document
      authoritative_source: "https://sos.oregon.gov/archives/"
    content_roots:
      - path: "policies"
        doc_type: "policy"
    plugins:
      issuing_body_registry: "_meta/registry.yml"
      issuing_body_registry_key: "entries"
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

# Unclosed flow mapping — the exact shape #136 was reproduced with.
MALFORMED = "entries: [ {slug: a\n"


def _corpus(tmp_path: Path, registry: str | None) -> Path:
    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policies" / "p-1.md").write_text(DOC)
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent(CONFIG).strip() + "\n")
    if registry is not None:
        (tmp_path / "_meta" / "registry.yml").write_text(registry)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "corpus"], cwd=tmp_path, check=True)
    return tmp_path


def _config(root: Path):
    return load_config(str(root / "_meta" / "corpus.yml"))


def _fw(root: Path) -> CorpusFramework:
    return CorpusFramework(_config(root))


def test_a_malformed_registry_answers_unknown_rather_than_raising(tmp_path):
    """THE BUG. `None` is this property's documented "there is nothing readable to check
    against"; a raise is not an answer at all, and every reader of the registry — the index
    build, `issuing_body_profile`, `documents_by_agency`, `search_corpus`'s filter — got the
    raise instead."""
    config = _config(_corpus(tmp_path, MALFORMED))

    assert config.issuing_body_slugs is None


def test_the_reason_names_the_file_and_the_key_that_declared_it(tmp_path):
    """An operator reading a traceback learns nothing about WHICH file is at fault. The
    missing-registry message says so plainly, and this is the same condition, so it says the
    same things: the path, the config key that declared it, and what went wrong."""
    config = _config(_corpus(tmp_path, MALFORMED))

    problem = config.issuing_body_registry_read.problem

    assert problem, "an unreadable registry must carry its reason, not just answer unknown"
    assert "could not be read" in problem
    assert "ParserError" in problem, problem


def test_a_readable_registry_still_answers_with_its_slugs(tmp_path):
    """THE GUARD MUST NOT FIRE ON A GOOD REGISTRY. A reader that answered "unknown" for
    every corpus would satisfy the paragraph above and check nothing."""
    config = _config(_corpus(tmp_path, json.dumps(
        {"entries": [{"slug": DAS, "name": "Administrative Services, Department of"}]})))

    assert config.issuing_body_slugs == frozenset({DAS})
    assert config.issuing_body_registry_read.problem is None


def test_an_empty_registry_is_read_and_is_not_unknown(tmp_path):
    """A REGISTRY THAT HOLDS NOTHING IS NOT A REGISTRY NOBODY COULD OPEN. It was read; the
    answer to "is this a slug" is a genuine no. Collapsing the two would report a corpus
    whose registry is empty as one whose registry is broken, and vice versa."""
    config = _config(_corpus(tmp_path, "entries: []\n"))

    assert config.issuing_body_slugs == frozenset()
    assert config.issuing_body_registry_read.problem is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file anyway")
def test_a_registry_that_cannot_be_opened_answers_unknown_too(tmp_path):
    """PARSE FAILURE IS NOT THE ONLY WAY TO FAIL A READ. Permission is the other one that
    happens in a container, and it must land in the same place — the condition is "this
    corpus declares a registry it cannot read", not "the YAML is bad"."""
    root = _corpus(tmp_path, json.dumps({"entries": [{"slug": DAS}]}))
    (root / "_meta" / "registry.yml").chmod(0o000)

    config = _config(root)

    assert config.issuing_body_slugs is None
    assert "PermissionError" in (config.issuing_body_registry_read.problem or "")


# ---- the live tools: an answer that states the limit, never a traceback ----------------

def test_a_filtered_search_answers_and_says_the_registry_could_not_be_read(tmp_path):
    """THE REPORTED SYMPTOM. `search_corpus`'s `registry_checked` is documented as covering
    exactly this question, and a third outcome — the registry raised — was not covered by
    either of its two values. An agent got a failed tool call instead of a finding."""
    hits = _fw(_corpus(tmp_path, MALFORMED)).search_corpus("permit", issuing_body=DAS)

    filt = hits[0]["issuing_body_filter"]
    assert filt["registry_checked"] is False
    assert "could not be read" in filt["note"], filt["note"]
    assert "_meta/registry.yml" in filt["note"], filt["note"]
    assert "not checked is not the same" in filt["note"].lower()


def test_the_unreadable_note_is_not_the_declares_no_registry_note(tmp_path):
    """A FAULT IS NOT A CHOICE. Declaring no registry is a corpus deciding it has no bodies
    to check against; declaring one that cannot be read is a broken configuration somebody
    has to fix. Serving the second as the first absorbs the last signal that anything is
    wrong into a positive statement about intent."""
    note = _fw(_corpus(tmp_path, MALFORMED)).search_corpus(
        "permit", issuing_body=DAS)[0]["issuing_body_filter"]["note"]

    assert "declares no issuing-body registry" not in note, note


def test_issuing_body_profile_states_the_limit_instead_of_raising(tmp_path):
    """The tool whose whole subject is the registry. It read the file a second way and so
    raised even after the config stopped doing so — the answer has to come from the same
    read as everything else."""
    out = _fw(_corpus(tmp_path, MALFORMED)).issuing_body_profile(DAS)

    assert "could not be read" in out["error"], out
    assert "_meta/registry.yml" in out["error"], out
    assert out["corpus"], "every return path carries the envelope (corpus-toolkit#38)"


def test_an_unreadable_registry_is_not_reported_as_this_body_being_unregistered(tmp_path):
    """THE THREE FINDINGS, KEPT APART. "Could not read the registry", "the registry is
    empty" and "this body is not in the registry" have different causes and different
    fixes, and only the first one is true here. `issuing_body_profile`'s no-match wording
    is what an unreadable registry must never degrade into."""
    out = _fw(_corpus(tmp_path, MALFORMED)).issuing_body_profile(DAS)

    assert "no unique issuing body match" not in out["error"], out
    assert "candidates" not in out, out


def test_documents_by_agency_answers_with_the_slug_unchecked(tmp_path):
    """`slug_in_registry: null` is this tool's "was not checked here", and the tool is
    deliberately not registry-gated — so a broken registry must degrade it, not kill it."""
    out = _fw(_corpus(tmp_path, MALFORMED)).documents_by_agency(DAS)

    assert out["slug_in_registry"] is None
    assert out["total"] == 0
    assert "could not be read" in out["attribution"]["note"], out["attribution"]


def test_the_index_build_survives_a_registry_it_cannot_read(tmp_path):
    """One mistyped YAML line took out a whole server: the index build reads the registry
    once per document, so the raise arrived before any tool could answer at all."""
    overview = _fw(_corpus(tmp_path, MALFORMED)).corpus_overview()

    assert overview["documents_by_type"] == {"policy": 1}


def test_a_registry_row_with_no_slug_does_not_take_the_tool_down(tmp_path):
    """A REGISTRY THIS CORPUS CAN PARTLY READ IS STILL READ. One row missing its `slug`
    raised `KeyError: 'slug'` out of `issuing_body_profile` — a traceback naming neither the
    file nor the row, for a registry every other reader on the platform handles by skipping
    that row. The validator reports those rows by count (corpus-toolkit#129); the tool
    serves the bodies that do have a slug."""
    out = _fw(_corpus(tmp_path, json.dumps({"entries": [
        {"slug": DAS, "name": "Administrative Services, Department of"},
        {"name": "A body nobody gave a slug"}]}))).issuing_body_profile(DAS)

    assert out["slug"] == DAS
    assert out["registry"]["name"] == "Administrative Services, Department of"


def test_the_operator_is_told_at_startup_which_file_is_broken(tmp_path):
    """THE OTHER HALF OF corpus-toolkit#136. A corpus whose registry cannot be read still
    starts and still answers — every body-shaped tool degrades to "could not check" — and
    nothing in a per-call note reaches the person who can fix the file. `_load_backend`
    already validates its plug-in at startup for this reason; a declared-but-unreadable
    registry is the same class of fault, so the server says so once, on stderr, naming the
    file and the key."""
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        server_mod.build_server(_config(_corpus(tmp_path, MALFORMED)))

    said = captured.getvalue()
    assert "plugins.issuing_body_registry" in said, said
    assert "_meta/registry.yml" in said, said
    assert "could not be read" in said, said


def test_a_corpus_whose_registry_reads_gets_no_startup_warning(tmp_path):
    """THE GUARD MUST NOT FIRE ON A WORKING CORPUS. A startup line printed for every corpus
    is a line every operator learns to ignore, which is the same as printing nothing."""
    pytest.importorskip("mcp")
    from corpus_toolkit.mcp import server as server_mod

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        server_mod.build_server(_config(_corpus(tmp_path, json.dumps(
            {"entries": [{"slug": DAS, "name": "Administrative Services, Department of"}]}))))

    assert "issuing_body_registry" not in captured.getvalue(), captured.getvalue()


def test_a_registry_that_is_not_text_answers_unknown_too(tmp_path):
    """A REGISTRY IS READ AS TEXT, AND NOT EVERY FILE IS. A latin-1 or binary file raises
    `UnicodeDecodeError` out of `read_text()` — a `ValueError`, so an `OSError`/`YAMLError`
    catch misses it and the raise escapes into whatever asked, which is the bug this file
    is about wearing a third coat."""
    root = _corpus(tmp_path, "entries: []\n")
    (root / "_meta" / "registry.yml").write_bytes(b"entries:\n  - slug: caf\xe9\n")

    config = _config(root)

    assert config.issuing_body_slugs is None
    assert "UnicodeDecodeError" in (config.issuing_body_registry_read.problem or "")


def test_a_row_that_is_not_an_entry_at_all_is_counted_as_having_no_slug(tmp_path):
    """A ROW NOTHING CAN BE ATTRIBUTED TO IS ONE FINDING, whatever shape it is in. A bare
    string or a number under `entries:` was dropped before anything counted it, so the
    registry read clean, the validator reported nothing, and every document naming that
    body was reported as unregistered — a check that passed without checking (AGENTS.md)."""
    read = _config(_corpus(tmp_path, json.dumps({"entries": [
        {"slug": DAS, "name": "Administrative Services, Department of"},
        "department-of-something",
        {"name": "A body nobody gave a slug"}]}))).issuing_body_registry_read

    assert read.readable, "a malformed ROW is not an unreadable FILE"
    assert read.slugs == frozenset({DAS})
    assert read.without_slug == 2


def test_every_message_about_a_broken_registry_names_the_key_that_declared_it(tmp_path):
    """"WHICH FILE" IS HALF AN ANSWER — an operator needs the config key to change. The
    missing-case message has always named it; these are the same condition, so they say the
    same things, and they say them the same way."""
    fw = _fw(_corpus(tmp_path, MALFORMED))

    said = [fw.search_corpus("permit", issuing_body=DAS)[0]["issuing_body_filter"]["note"],
            fw.issuing_body_profile(DAS)["error"],
            fw.documents_by_agency(DAS)["attribution"]["note"]]

    for message in said:
        assert "plugins.issuing_body_registry" in message, message
        assert "_meta/registry.yml" in message, message
