#!/usr/bin/env python3
"""Release gate: drive a REAL corpus, instantiated from OregonAI/corpus-template, end to
end on the candidate toolkit ref.

WHY THIS EXISTS. `tests/` exercises the toolkit in isolation and passes; nothing checked
that a tag actually drives a corpus. That gap is not theoretical — measured on
2026-07-28, corpus-toolkit had the worst CI pass rate in the org (36.8% over 19 runs)
while every corpus pins a toolkit tag, so one defect here is one bug per corpus,
discovered N times and fixed N times. corpus-toolkit#1 is literally titled "CI has been
red since v1.5.0".

It is aimed squarely at the class of bug the unit tests structurally cannot see. Both
graph defects filed on 2026-07-28 (#4 external edge targets raising KeyError, #5 the
false "no document with id X") were live on four servers, and neither was caught by 73
passing unit tests, because no test ever built a corpus and asked the tools a question.
This does exactly that:

  1. instantiate the template into a scratch corpus and fill every placeholder
  2. write one real verbatim document + its snapshot, so provenance has something to
     verify rather than passing vacuously over an empty tree
  3. run the same CLIs the reusable workflows run — validate-frontmatter,
     verify-provenance, generate-index --check — against it
     and, since ADR 0015, corpus-detect-changes against two locally served sources:
     seed, change one, detect exactly that one, re-render the report from state
  4. run the template's OWN Dockerfile build commands against the candidate toolkit,
     extracted from its `RUN` instructions rather than copied (corpus-toolkit#100)
  5. build the MCP server the way `corpus-mcp-serve` does and CALL every mandatory core
     tool from docs/mcp-interface-contract.md through the SDK's own tool manager,
     asserting the ANSWER, not merely that a name is registered
  6. push every tool's real answer through the SDK's own result conversion and assert the
     payload a CLIENT receives is the payload the tool returned

The numbers above are the shape of the run, not the printed `[N/9]` labels -- the hybrid
leg splits some of these into inner steps.

Step 5 is the point. A tool that is registered and raises on every call passes a
`tools/list` check and fails a user.

Step 4 exists because the gate had the template checked out and reached past the one file
that encodes how a corpus actually STARTS. corpus-toolkit#75 deleted a method the
template's Dockerfile calls, this gate went green, and every corpus image build failed for
two releases (corpus-toolkit#100).

Step 5 is the other half of it, and it exists because step 4 alone was not enough: a tool
that answers correctly and whose answer is then discarded at serialization passes step 4
and still fails a user. v1.24.0 did precisely that — `get_document` returned three envelope
fields and no document body, reported success, and shipped through this gate GREEN, because
every call here goes through `sdk.call_tool(..., convert_result=False)`
(corpus-toolkit#61, #63).

  python3 .github/scripts/contract_smoke.py --template /path/to/corpus-template
  python3 .github/scripts/contract_smoke.py --template ... --keep   # leave the scratch corpus
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# docs/mcp-interface-contract.md, "Core tools (mandatory, all archetypes)".
MANDATORY_CORE_TOOLS = ["corpus_overview", "search_corpus", "get_document",
                        "resolve_citation", "graph_neighbors"]

DOC_ID = "schedule-smoke-0001"
SNAPSHOT_TEXT = (
    "OREGON SECRETARY OF STATE ARCHIVES DIVISION\n"
    "Retention Schedule 166-300-0001 — Administrative Records\n\n"
    "(1) Correspondence documenting agency policy decisions shall be retained "
    "permanently by the agency of record.\n"
    "(2) Routine transmittal correspondence may be destroyed when three years "
    "have elapsed from the date of creation.\n"
    "(3) Records covered by an active litigation hold shall not be destroyed "
    "regardless of the retention period stated above.\n"
    "(4) An agency that transfers records to the Archives Division retains "
    "responsibility for their accuracy at the time of transfer.\n")

# Placeholder -> value. Every {{...}} in the template must appear here; an unfilled one
# is a hard failure below, because a corpus that ships with a live placeholder is the
# defect the template's own setup checklist exists to prevent.
PLACEHOLDERS = {
    "CORPUS_ID": "smoke-corpus",
    "CORPUS_NAME": "Smoke Corpus",
    "JURISDICTION": "oregon",
    "ARCHETYPE": "document",
    "DOC_TYPE": "schedule",
    "CORPUS_SCOPE_DESCRIPTION": "a scratch corpus built by the toolkit release gate",
    "OWNER-PLACEHOLDER": "OregonAI/maintainers",
    # Must be a real URL, and no longer a `{{...}}` substitution: the template ships a
    # URL-SHAPED `.invalid` placeholder (see fill_front_door), because a bare placeholder
    # is not a URL and corpus-validate-frontmatter has errored on a non-URL here since
    # v1.10.0. This value is what fill_front_door writes over it.
    "AUTHORITATIVE_SOURCE_URL": "https://sos.oregon.gov/archives/records/Pages/default.aspx",
}


class GateFailure(RuntimeError):
    """A release-blocking failure. Carries the step name so the report names it."""


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], cwd: Path, step: str, *, expect_zero: bool = True) -> str:
    say(f"  $ {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    for line in out.splitlines():
        say(f"    | {line}")
    if expect_zero and p.returncode != 0:
        raise GateFailure(f"{step}: exited {p.returncode}")
    return out


# ---------------------------------------------------------------- instantiate

def instantiate(template: Path, dest: Path) -> None:
    """Copy the template and fill it in, the way a human instantiating it would."""
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns(".git"))

    for path in sorted(dest.rglob("*")):
        if not path.is_file() or path.suffix in (".png", ".pdf", ".db"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = text
        for key, value in PLACEHOLDERS.items():
            new = new.replace("{{" + key + "}}", value).replace("@" + key, "@" + value)
        if new != text:
            path.write_text(new, encoding="utf-8")

    leftovers = []
    for path in sorted(dest.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in re.finditer(r"\{\{[A-Z_]+\}\}", text):
            leftovers.append(f"{path.relative_to(dest)}: {m.group(0)}")
    if leftovers:
        raise GateFailure("instantiate: template placeholders left unfilled — this "
                          "script's PLACEHOLDERS map has drifted from the template:\n    "
                          + "\n    ".join(leftovers))
    fill_front_door(dest)


def fill_front_door(dest: Path) -> None:
    """Give the scratch corpus a real `corpus.authoritative_source`, as a human would.

    NOT A `{{...}}` PLACEHOLDER, which is why it needs its own step. The template ships
    `https://REPLACE-ME.invalid/where-the-official-text-lives` — URL-shaped so the
    template can validate itself, under a host RFC 2606 guarantees can never resolve. A
    corpus that has a name and holds documents may not ship that value
    (corpus-toolkit#11), and this gate instantiates exactly such a corpus. Leaving it
    would gate every release on a corpus no corpus is allowed to be.

    Loud on drift, like the leftover-placeholder check above: if the template stops
    carrying exactly one `authoritative_source:` line, this gate stops filling it in and
    would otherwise go quietly back to testing the placeholder.
    """
    cy = dest / "_meta" / "corpus.yml"
    text = cy.read_text(encoding="utf-8")
    filled, n = re.subn(
        r"(?m)^(\s*authoritative_source:).*$",
        lambda m: f'{m.group(1)} "{PLACEHOLDERS["AUTHORITATIVE_SOURCE_URL"]}"', text)
    if n != 1:
        raise GateFailure(f"instantiate: the template's corpus.yml carries {n} "
                          f"`authoritative_source:` lines, expected exactly 1 — this "
                          f"script can no longer fill in the corpus's front door")
    cy.write_text(filled, encoding="utf-8")


def write_document(dest: Path) -> None:
    """One real verbatim document, its snapshot, and the manifest entry.

    Deliberately verbatim rather than summary: the summary path skips the full-text /
    snapshot comparison entirely, so a gate built on a summary document would run
    corpus-verify-provenance over a corpus where it has nothing to verify — green, and
    proving nothing. That is the exact failure shape this whole gate exists to catch."""
    from corpus_toolkit.repo import hash_snapshot

    snapshots = dest / "_meta" / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{DOC_ID}.html").write_text(
        "<html><body><pre>" + SNAPSHOT_TEXT + "</pre></body></html>", encoding="utf-8")
    (snapshots / f"{DOC_ID}.txt").write_text(SNAPSHOT_TEXT, encoding="utf-8")
    digest = hash_snapshot(DOC_ID, "html", snapshots)

    # DECLARE THE SLUG FIELD. Without it the smoke document resolves to no issuing-body
    # slug — it sits under an unscoped root and its `issuing_body` is free text — so
    # `documents_by_agency` had nothing to find and its round-trip compared `[] == []`,
    # asserting nothing about the documents list, the one field serialization could drop
    # (corpus-toolkit#46). Every live corpus that answers this tool declares the field; the
    # gate's corpus should look like one.
    cy = dest / "_meta" / "corpus.yml"
    text = cy.read_text(encoding="utf-8")
    if "plugins:" not in text:
        raise GateFailure("the template's corpus.yml has no `plugins:` block to extend")
    cy.write_text(text.replace(
        "plugins:", 'plugins:\n  issuing_body_slug_field: "issuing_body_slug"', 1),
        encoding="utf-8")

    body_lines = "\n".join(ln for ln in SNAPSHOT_TEXT.splitlines())
    (dest / "documents" / f"{DOC_ID}.md").write_text(f"""\
---
schema_version: 1
corpus: "{PLACEHOLDERS['CORPUS_ID']}"
jurisdiction: "{PLACEHOLDERS['JURISDICTION']}"
id: {DOC_ID}
title: "Retention Schedule 166-300-0001 — Administrative Records"
doc_type: schedule
citation: "Schedule 166-300-0001"
authority_level: administrative_rule
issuing_body: "Secretary of State Archives Division"
issuing_body_slug: "secretary-of-state-archives-division"
legal_authority: []
source_url: "https://sos.oregon.gov/archives/records/Pages/166-300-0001.aspx"
source_format: html
retrieved: 2026-07-28
source_sha256: "{digest}"
effective_date:
last_reviewed:
source_version: ""
status: current
content_mode: verbatim
conversion_notes: ""
last_verified: ""
verified_by: ""
maintainer: "@{PLACEHOLDERS['OWNER-PLACEHOLDER']}"
relationships:
  implements: []
  implemented_by: []
  references_external: ["OAR 166-300-0015"]
  related: []
  supersedes: []
tags: [retention, smoke]
---

> **NON-AUTHORITATIVE — AI-friendly reference only.** This is a curated
> copy, not the official text. Verify against the official source:
> https://sos.oregon.gov/archives/records/Pages/166-300-0001.aspx (retrieved 2026-07-28).

# Retention Schedule 166-300-0001 — Administrative Records (Schedule 166-300-0001)

## At a glance

Retention periods for agency administrative correspondence, written by the release
gate so provenance verification has real text to compare against.

## Full text

{body_lines}
""", encoding="utf-8")

    manifest = dest / "_meta" / "source-manifest.yml"
    manifest.write_text(f"""\
# Written by the toolkit release gate (.github/scripts/contract_smoke.py).
sources:
  - id: {DOC_ID}
    url: "https://sos.oregon.gov/archives/records/Pages/166-300-0001.aspx"
    sha256: "{digest}"
    recheck: quarterly
""", encoding="utf-8")


def git_init(dest: Path) -> None:
    """The FTS cache key and corpus_overview's `commit` field both shell out to git, and
    an un-inited directory makes both silently empty. Commit so the corpus is a corpus."""
    run(["git", "init", "-q", "-b", "main"], dest, "git init")
    run(["git", "config", "user.email", "gate@example.invalid"], dest, "git config")
    run(["git", "config", "user.name", "release gate"], dest, "git config")
    run(["git", "add", "-A"], dest, "git add")
    run(["git", "commit", "-qm", "scratch corpus"], dest, "git commit")


# ---------------------------------------------------------------- checks

def run_cli_gates(dest: Path) -> None:
    """The same commands the reusable workflows run, against a real corpus.

    Invoked as CLIs rather than by calling the reusable workflows, because a workflow
    cannot be pointed at a scratch directory — but these ARE the workflows' payload;
    validate-frontmatter.yml et al. are thin wrappers around exactly these console
    scripts."""
    cfg = str(dest / "_meta" / "corpus.yml")
    run([sys.executable, "src/build_graph.py"], dest, "build_graph")
    run(["corpus-validate-frontmatter", "--config", cfg], dest, "validate-frontmatter")
    run(["corpus-verify-provenance", "--config", cfg], dest, "verify-provenance")
    run(["corpus-generate-index", "--config", cfg], dest, "generate-index")
    run(["corpus-generate-index", "--config", cfg, "--check"], dest,
        "generate-index --check")
    # The template's own generated-artifact gate must pass on a freshly built corpus.
    run([sys.executable, "src/build_graph.py", "--check"], dest, "build_graph --check")


def check_drift(dest: Path) -> None:
    """The one CLI the gate never ran: `corpus-detect-changes`, end to end (ADR 0015).

    The largest module on the platform, the only one that rewrites curated data (the
    manifest's baselines) and the one whose failure mode — inert for weeks, reporting
    success — is the hardest to see from a unit test. Two sources are served from a local
    HTTP server; the first run must SEED both (no baseline recorded) and write DRIFT.md;
    one file is then changed and the second run must report exactly that one as changed,
    with a first-observed date, and the other as unchanged. Both runs exit 0: drift is a
    signal, not an error. Finally `corpus-drift-report` must re-render the same report
    from the state files alone.
    """
    import http.server
    import socketserver
    import threading
    import yaml as _yaml

    served = dest / "_scratch_upstream"
    served.mkdir()
    (served / "a.html").write_text("<html><body><p>Rule A, version one.</p></body></html>")
    (served / "b.html").write_text("<html><body><p>Rule B, version one.</p></body></html>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # keep the gate log readable
            pass
    handler = lambda *a, **k: Quiet(*a, directory=str(served), **k)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        cfg_path = dest / "_meta" / "corpus.yml"
        cfg = _yaml.safe_load(cfg_path.read_text())
        manifest = dest / cfg["source_manifest_path"]
        manifest.write_text(
            "sources:\n"
            f"  - id: rule-a\n    url: http://127.0.0.1:{port}/a.html\n    format: html\n    sha256: \"\"\n"
            f"  - id: rule-b\n    url: http://127.0.0.1:{port}/b.html\n    format: html\n    sha256: \"\"\n")
        cfg_s = str(cfg_path)
        out = run(["corpus-detect-changes", "--config", cfg_s], dest, "detect-changes (seed run)")
        if "2 source(s) had no recorded baseline and were SEEDED" not in out:
            raise GateFailure("seed run: the two unseeded sources were not reported as seeded")
        seeded = _yaml.safe_load(manifest.read_text())["sources"]
        if not all(s["sha256"] for s in seeded):
            raise GateFailure(f"seed run: manifest baselines not written: {seeded}")
        drift_md = dest / "DRIFT.md"
        state_p = dest / "drift-state.json"
        if not drift_md.is_file() or not state_p.is_file():
            raise GateFailure("seed run: DRIFT.md / drift-state.json not written")
        if "## Seeded this run (2)" not in drift_md.read_text():
            raise GateFailure("seed run: DRIFT.md does not list the two seeded sources")

        (served / "b.html").write_text("<html><body><p>Rule B, version TWO.</p></body></html>")
        out = run(["corpus-detect-changes", "--config", cfg_s], dest, "detect-changes (drift run)")
        if "1 changed, 0 fetch failure(s)" not in out:
            raise GateFailure(f"drift run: expected exactly one changed source, got:\n{out}")
        state = json.loads(state_p.read_text())
        by_id = {s["id"]: s for s in state["sources"]}
        if by_id["rule-b"]["outcome"] != "changed" or not by_id["rule-b"]["first_changed_at"]:
            raise GateFailure(f"drift run: rule-b not recorded as changed-since: {by_id['rule-b']}")
        if by_id["rule-a"]["outcome"] != "unchanged":
            raise GateFailure(f"drift run: rule-a should be unchanged: {by_id['rule-a']}")
        if state["last_run"]["red_reasons"]:
            raise GateFailure(f"drift run: verdict red for no reason: {state['last_run']}")
        text = drift_md.read_text()
        if "## Changed since baseline (1)" not in text or "`rule-b`" not in text:
            raise GateFailure("drift run: DRIFT.md does not list rule-b under changed since baseline")

        before = text
        run(["corpus-drift-report", "--config", cfg_s], dest, "drift-report (re-render)")
        after = drift_md.read_text()
        strip = lambda s: "\n".join(l for l in s.splitlines() if not l.startswith("**Generated by"))
        if strip(before) != strip(after):
            raise GateFailure("corpus-drift-report re-rendered a different report from the same state")
        run(["corpus-drift-report", "--config", cfg_s, "--check"], dest, "drift-report --check")
        # Leave the scratch corpus's manifest as the later legs expect it (no sources).
        manifest.write_text("sources: []\n")
        for f in ("DRIFT.md", "drift-state.json", "source-outcomes.json",
                  "access-failures.json", "changed-sources.tsv"):
            (dest / f).unlink(missing_ok=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(served, ignore_errors=True)


def check_mcp_tools(dest: Path) -> None:
    """Build the server exactly as corpus-mcp-serve does and CALL every mandatory tool.

    Registration is checked AND answers are checked. Asserting only that a name appears
    in tools/list is the cheap version of this test and would have passed throughout the
    entire lifetime of both graph bugs: graph_neighbors was registered, listed, and
    raised KeyError on every call in oregon-records-retention."""
    from corpus_toolkit import config as config_mod
    from corpus_toolkit.mcp import sdk
    from corpus_toolkit.mcp.server import build_server

    config = config_mod.load(dest / "_meta" / "corpus.yml")
    mcp = build_server(config)
    listed = sdk.tool_names(mcp)
    say(f"  tools/list: {', '.join(sorted(listed))}")

    missing = [t for t in MANDATORY_CORE_TOOLS if t not in listed]
    if missing:
        raise GateFailure(f"mcp tools: mandatory core tool(s) not registered: {missing}")

    calls = [
        ("corpus_overview", {}),
        ("search_corpus", {"query": "correspondence"}),
        ("get_document", {"doc_id": DOC_ID}),
        ("resolve_citation", {"citation": "Schedule 166-300-0001"}),
        ("graph_neighbors", {"doc_id": DOC_ID}),
        # Not a mandatory core tool (document/hybrid extension), but it shares the
        # graph code path that broke, and a corpus built from the template has it.
        ("authority_chain", {"doc_id": DOC_ID}),
    ]
    results = {}
    failures = []
    for name, args in calls:
        if name not in listed:
            continue
        try:
            results[name] = asyncio.run(sdk.call_tool(mcp, name, args))
        except Exception as e:                                   # noqa: BLE001
            # Record and continue. A raising tool must not abort the run before the
            # others report — that turns one broken tool into an unreadable crash and
            # hides everything after it.
            failures.append(f"{name}{args} raised {type(e).__name__}: {e}")
    for f in failures:
        say(f"  FAIL {f}")
    if failures:
        raise GateFailure("mcp tools: " + "; ".join(failures))

    # ---- the answers themselves ----
    problems = []

    overview = results["corpus_overview"]
    if overview.get("documents_by_type", {}).get("schedule") != 1:
        problems.append(f"corpus_overview reports {overview.get('documents_by_type')!r}, "
                        f"expected one schedule — the corpus is not being indexed")
    for field in ("corpus", "archetype", "contract_version"):
        if not overview.get(field) and overview.get(field) != 0:
            problems.append(f"corpus_overview carries no {field!r} (response convention 1)")

    hits = results["search_corpus"]
    if not any(h.get("id") == DOC_ID for h in hits):
        problems.append(f"search_corpus('correspondence') returned {len(hits)} hit(s), "
                        f"none of them {DOC_ID} — full-text search is not working")

    doc = results["get_document"]
    if doc.get("id") != DOC_ID or "correspondence" not in (doc.get("body") or ""):
        problems.append(f"get_document({DOC_ID}) did not return the document body")
    if not doc.get("source_url"):
        problems.append("get_document carries no source_url (response convention 2)")

    cite = results["resolve_citation"]
    if "citation" not in cite:
        problems.append(f"resolve_citation returned {cite!r}")

    nb = results["graph_neighbors"]
    if nb.get("error"):
        problems.append(f"graph_neighbors({DOC_ID}) errored: {nb['error']}")
    elif nb.get("references_external") != [{"citation": "OAR 166-300-0015",
                                            "external": True}]:
        # The regression that motivated the gate: an edge target that is not a local
        # node. The fixture document cites one deliberately.
        problems.append(f"graph_neighbors did not report the external edge target as "
                        f"external: {nb.get('references_external')!r}")

    if "authority_chain" in results and results["authority_chain"].get("error"):
        problems.append(f"authority_chain errored: {results['authority_chain']['error']}")

    for p in problems:
        say(f"  FAIL {p}")
    if problems:
        raise GateFailure(f"mcp tools: {len(problems)} contract violation(s)")
    say(f"  OK: {len(results)} tool(s) called, every answer checked")


# --------------------------------------- serialization leg (corpus-toolkit#63)

def check_result_marshalling(dest: Path) -> None:
    """The SECOND assertion on the same tools: not what the tool RETURNED, but what a
    CLIENT would RECEIVE.

    Everything above this line — every call in `check_mcp_tools`, and every test in
    `tests/` — goes through `sdk.call_tool`, which passes `convert_result=False`. That is
    deliberate and its reasoning holds: this gate asserts that an external graph neighbour
    comes back `{citation, external: true}`, and asserting that through the SDK's
    marshalling would test the SDK rather than the toolkit. The gap it left is that nothing
    asserted the marshalling EITHER, and that gap is exactly what v1.24.0 shipped through:
    a declared output schema dropped every document body on the way out, the call still
    reported success, and THIS GATE PASSED — it builds a real corpus and calls real tools,
    with conversion switched off (corpus-toolkit#61, #63).

    So the leg is added rather than the flag flipped. Two assertions on one call: the first
    says the toolkit computed the wrong answer, this one says the right answer did not
    survive the trip, and a single merged assertion could say neither.

    A SCHEMA CHECK CANNOT REPLACE THIS. v1.24.0 shipped with tests that measured
    `additionalProperties` on the emitted schema — absent, so extras validate — and
    concluded extras were safe. Wrong layer: extras clear validation and are then discarded
    by the model that serializes. Only a payload going in one end and being compared at the
    other can see it.
    """
    from corpus_toolkit import config as config_mod
    from corpus_toolkit.mcp import sdk
    from corpus_toolkit.mcp.server import build_server

    config = config_mod.load(dest / "_meta" / "corpus.yml")
    mcp = build_server(config)
    tools = sdk.tools_by_name(mcp)

    # The same calls the behaviour leg makes, plus the hybrid extension tool this corpus
    # has by now (this step runs after `hybridize`) — the tools_module surface no test
    # reached at all, and the one a corpus writes itself.
    calls = [
        ("corpus_overview", {}),
        ("search_corpus", {"query": "correspondence"}),
        ("get_document", {"doc_id": DOC_ID}),
        ("resolve_citation", {"citation": "Schedule 166-300-0001"}),
        ("graph_neighbors", {"doc_id": DOC_ID}),
        ("authority_chain", {"doc_id": DOC_ID}),
        ("list_datasets", {}),
        # The CONFORMING extension tool (corpus-toolkit#96). Registering it without adding
        # it here is what this step's own uncovered-tool check caught during development —
        # which is the check doing its job, and the reason it exists.
        ("join_lookup", {"document_id": DOC_ID}),
        # corpus-toolkit#46. The slug is read from the indexed document rather than
        # hardcoded, because a slug this corpus does not hold makes the whole leg `[] == []`
        # — the documents list is the one field serialization could drop, and an empty one
        # round-trips whatever the code does. That is the vacuity the `search_corpus`
        # special-case below guards against, and the first version of this line had it.
        ("documents_by_agency", {"slug": _smoke_slug(dest)}),
    ]
    # PRESENT, NOT MERELY COVERED. The loop below skips any tool that is not registered, and
    # `documents_by_agency` is not in MANDATORY_CORE_TOOLS — so if its registration gate
    # inverts, or `FileBackend.documents_for_slug` is lost, the whole leg is skipped and the
    # gate goes green having asserted nothing. The `uncovered` check catches only the
    # opposite direction. The gate's corpus is always file-backed, so its absence is a fault.
    if "documents_by_agency" not in tools:
        raise GateFailure(
            "documents_by_agency is not registered on a file-backed corpus. Either the "
            "capability gate in server.py inverted or FileBackend lost documents_for_slug; "
            "either way the tool every corpus-gateway agency lookup depends on is gone, and "
            "the calls below would have skipped it silently.")
    for name, args in calls:
        if name == "documents_by_agency" and not args.get("slug"):
            raise GateFailure(
                "the smoke corpus resolved no issuing-body slug, so documents_by_agency "
                "would round-trip an empty document list and assert nothing about the one "
                "field serialization could drop.")
    uncovered = sorted(set(tools) - {name for name, _ in calls})
    if uncovered:
        raise GateFailure(
            f"tool(s) registered with no serialization coverage: {uncovered}. Add each to "
            f"this step's call list; a tool whose result is never round-tripped can start "
            f"returning an empty envelope and keep reporting success (corpus-toolkit#61).")

    problems = []
    for name, args in calls:
        if name not in tools:
            continue
        raw = asyncio.run(sdk.call_tool(mcp, name, args))
        if name == "documents_by_agency" and not raw.get("documents"):
            # NOT ENOUGH TO ASK FOR A REAL SLUG. Giving the smoke corpus a slug removed one
            # way this leg could be empty; it did not make the leg assert anything, and a
            # mutation returning `documents: []` still passed the gate. `documents` is the
            # only field here serialization could drop, so an empty one round-trips whatever
            # the code does — the same `[] == []` the search_corpus check below exists for.
            problems.append(f"documents_by_agency returned {len(raw.get('documents', []))} "
                            f"document(s) for {args['slug']!r}, so its per-document "
                            f"assertions would pass over an empty list")
        if name == "search_corpus" and not any(h.get("id") == DOC_ID for h in raw):
            # Otherwise this tool's whole leg is `[] == []`: every assertion below holds
            # for an empty answer. Step 5 asserts hits exist, but against the server it
            # built BEFORE the hybrid flip — this step rebuilds, so it must ask again.
            problems.append(f"search_corpus returned {len(raw)} hit(s), none of them "
                            f"{DOC_ID} — the per-hit assertions below would pass over an "
                            f"empty list and prove nothing")
        try:
            texts, structured = sdk.serialized_result(tools[name], raw)
        except Exception as e:                                   # noqa: BLE001
            # A ValidationError here is bug 1 of #61 verbatim — a declared shape refusing a
            # value the toolkit documents. Collect, so one rejecting tool does not hide the
            # rest.
            problems.append(f"{name}{args} could not be serialized at all: "
                            f"{type(e).__name__}: {e}")
            continue

        # The content blocks: one per item for a list answer, one for an object. Decoded,
        # because a block that is not the JSON its payload was is a block a client cannot
        # parse.
        expected_blocks = raw if isinstance(raw, list) else [raw]
        try:
            blocks = [json.loads(t) for t in texts]
        except ValueError as e:
            problems.append(f"{name}: a content block is not valid JSON ({e})")
            blocks = None
        if blocks is not None and blocks != json.loads(json.dumps(expected_blocks,
                                                                 default=str)):
            problems.append(
                f"{name}{args}: the content blocks a client renders are not the answer "
                f"the tool returned ({len(blocks)} block(s) for "
                f"{len(expected_blocks)} item(s))")

        # The structured half. None is legitimate ONLY when the tool declared no output
        # schema — the shape a bare `-> dict` annotation produces on both majors, which is
        # how the live hybrid corpora write their extension tools (corpus-toolkit#96). A
        # tool that DECLARES a schema and then serializes nothing is #61 itself, so the
        # exemption keys on the declaration and never on the result coming back empty.
        declared = getattr(tools[name], "output_schema", None)
        if structured is None:
            if declared is None:
                continue
            problems.append(
                f"{name}{args}: declares an output schema and serialized NO structured "
                f"content — the half a schema-driven client parses is absent, with the "
                f"call still reporting success")
            continue
        want = {"result": raw} if isinstance(raw, list) else raw
        want = json.loads(json.dumps(want, default=str))
        if structured != want:
            lost = sorted(set(want) - set(structured))
            problems.append(
                f"{name}{args}: the structured content a client parses is not the answer "
                f"the tool returned"
                + (f"; keys DROPPED at serialization: {lost}" if lost else ""))

    # Bug 1 of #61 directly: a corpus that declares no `authoritative_source` emits null
    # there by design (response convention 1), and this gate's corpus declares one — so the
    # null is asserted against each object tool's own converter rather than by rebuilding
    # the corpus without a source.
    for name, tool in sorted(tools.items()):
        # Object-shaped tools only. Skipping by SHAPE rather than by the name
        # `search_corpus`: feeding an object probe to a list-shaped tool raises a
        # ValidationError that reads exactly like a rejected payload, so the first
        # `query_dataset` a corpus adds to SMOKE_TOOLS would produce a bogus "rejected
        # authoritative_source: null" here and the cheap fix would be to weaken this.
        if sdk.declares_list_result(tool) or getattr(tool, "output_schema", None) is None:
            continue

        # WHAT THE SCHEMA SAYS, not only what it survives (corpus-toolkit#15). The three
        # fields were in every response body and named by no declaration, so a validating
        # client could check nothing. Asserted here as well as in the suite because this
        # gate builds a REAL corpus from corpus-template and reads the schemas the SDK
        # actually emitted, which is the artifact a client consumes.
        #
        # Keyed on the schema HAVING properties at all: a tool annotated bare `-> dict`
        # declares no schema, and one annotated `dict[str, Any]` emits
        # `{"additionalProperties": true}` with no properties, so neither is in scope here
        # — its declaration says nothing. Silence is the exemption; a schema that describes
        # fields and omits these three is not.
        #
        # An extension tool CAN now declare the convention (corpus-toolkit#96) via
        # `framework.with_envelope` and `-> ResponseEnvelope`, and SMOKE_TOOLS registers one
        # so this assertion actually runs against a corpus-supplied tool rather than only
        # the built-ins. The bare tool beside it keeps the exemption honest: it is what the
        # live corpora still ship.
        props = (getattr(tool, "output_schema", None) or {}).get("properties") or {}
        absent = [f for f in ("corpus", "archetype", "authoritative_source")
                  if props and f not in props]
        if absent:
            problems.append(f"{name} declares an output schema describing "
                            f"{sorted(props)} and does not name {absent} — response "
                            f"convention 1 is invisible to schema-driven validation")

        payload = {"corpus": "smoke-corpus", "archetype": "hybrid",
                   "authoritative_source": None, "detail": "tool-specific payload"}
        try:
            out = sdk.structured_result(tool, payload)
        except Exception as e:                                   # noqa: BLE001
            problems.append(f"{name} rejected `authoritative_source: null`, the documented "
                            f"value for a corpus declaring no source: "
                            f"{type(e).__name__}: {e}")
            continue
        if out.get("authoritative_source", "missing") is not None or "detail" not in out:
            problems.append(f"{name} did not round-trip a null source plus a tool-specific "
                            f"key: {out!r}")

    for p in problems:
        say(f"  FAIL {p}")
    if problems:
        raise GateFailure(f"result marshalling: {len(problems)} violation(s)")
    say(f"  OK: {len(calls)} tool result(s) round-tripped, blocks and structured content "
        f"both intact")


# ------------------------------------------------- hybrid leg (corpus-toolkit#38)


# ------------------------------------------------- the template's own build commands (#100)

# Step shapes this gate recognises inside a Dockerfile `RUN`. NARROW ON PURPOSE.
#
# corpus-toolkit#75 deleted `CorpusFramework.ensure_index` after finding no caller in this
# repo. corpus-template's Dockerfile calls it at image build, nothing in CI ran that file,
# the gate went green, and every corpus image build failed until v1.26.1 -- while a
# reconcile loop retried the failing build every ten minutes.
#
# So the gate runs those commands. It EXTRACTS them rather than copying them: a duplicate
# in CI drifts from the Dockerfile and then asserts nothing, which is the same species of
# bug. And it REFUSES a step it does not recognise rather than skipping it, because an
# extractor that quietly matches nothing reproduces the exact green this exists to remove.
_RUNNABLE_PREFIXES = ("python3 -c", "python -c", "corpus-")
_CONTAINER_ONLY_PREFIXES = ("apt-get", "rm -rf /var/lib/apt", "pip install", "pip3 install")


def _refuse_shell_tail(dockerfile: Path, step: str) -> None:
    """Refuse a recognised step carrying shell control operators after its payload.

    Recognition is by PREFIX, so without this anything appended to a known-good command ran
    unexamined -- including a tail that makes the step always succeed:

        python3 -c "...ensure_index()" || echo "WARNING: not warmed"

    That prints `OK: 1 build command(s)` having asserted nothing, which is corpus-toolkit
    #100's own defect rebuilt inside its fix. `oregon-counties` already ships exactly that
    shape (deliberately non-fatal there), and corpus Dockerfiles derive from this template.

    Quoted payloads are excised first: the template's `python3 -c` string legitimately
    contains `;` separators, and only operators OUTSIDE the quotes are shell control.
    """
    outside = re.sub(r"'[^']*'|\"[^\"]*\"", "", step)
    for op in ("||", "&", ";", "#", "`", "$("):
        if op in outside:
            raise GateFailure(
                f"{dockerfile}: build step {step!r} contains {op!r} outside its quoted "
                f"payload. This gate refuses shell control operators in an extracted step, "
                f"because a tail like `|| echo WARNING` makes it always succeed and the "
                f"gate then reports OK having asserted nothing. Split the step, or move it "
                f"out of the RUN.")


def dockerfile_cmd_argv(path: Path) -> list[str]:
    """The template's `CMD`, as argv (corpus-toolkit#116).

    THE CMD IS HOW THE CONTAINER ACTUALLY STARTS, and nothing validated it. The gate asserts
    `corpus-mcp-serve --help`, which argparse answers with exit 0 regardless of which options
    exist -- so renaming a flag left the unit suite green (test_mount_path.py builds the app
    through `sdk.http_kwargs` and never touches the parser), the entrypoints job green (it
    asserts `hasattr(module, "main")`), and this gate green, while every corpus container
    crash-looped on `unrecognized arguments`. That CMD is identical across all seven live
    corpora.

    EXTRACTED, NOT COPIED -- corpus-toolkit#100's rule. A hardcoded duplicate in CI drifts
    from the Dockerfile and then asserts nothing, which is the species of bug this closes.

    JSON-ARRAY FORM ONLY, and shell form is REFUSED rather than skipped: silence is how a
    gate ends up asserting less than it appears to. Continuations are removed the way a shell
    removes them (`\\<newline>` deleted, not replaced with a space) -- #100 hit that exact
    trap and produced an IndentationError that read as "this release breaks the template".
    """
    text = path.read_text(encoding="utf-8")
    # Comments stripped BEFORE joining continuations: Docker does it in that order, and a
    # comment line ending in a backslash otherwise glues the next instruction onto it.
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    text = re.sub(r"\\\n", "", text)
    for line in text.splitlines():
        if not re.match(r"(?i)^\s*CMD\s", line):
            continue
        payload = line.split(None, 1)[1].strip()
        if not payload.startswith("["):
            raise GateFailure(
                f"{path.name}: CMD is in shell form ({payload[:60]!r}). This gate parses the "
                f"JSON-array (exec) form only, and refuses rather than skipping — a CMD it "
                f"cannot read is a CMD nothing validates.")
        try:
            argv = json.loads(payload)
        except json.JSONDecodeError as e:
            raise GateFailure(f"{path.name}: CMD is not parseable JSON ({e}): {payload[:80]!r}")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise GateFailure(f"{path.name}: CMD is not a list of strings: {argv!r}")
        return argv
    raise GateFailure(
        f"{path.name}: found no CMD. The container's start command is the one artifact "
        f"describing how a corpus actually runs; refusing to report zero as success.")


def requirements_extras(path: Path) -> set[str]:
    """The extras named in a `requirements.txt` line for corpus-toolkit.

    THE EXTRAS ARE NAMES A CORPUS DEPENDS ON. The gate classifies
    `pip install -r requirements.txt` as container-only and skips it, so nothing checked
    them -- and pip only WARNS on an unknown extra. Delete or rename `semantic` and the image
    builds, this gate is green, and every corpus loses numpy: `semantic.available()` returns
    False and the corpus serves keyword-only WHILE REPORTING HEALTHY. That is the
    federal-reference incident `pyproject.toml`'s own comment records.
    """
    extras: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^corpus-toolkit\s*\[([^\]]*)\]", line)
        if m:
            extras |= {e.strip() for e in m.group(1).split(",") if e.strip()}
    return extras


def _read_toml(path: Path) -> dict:
    """Parse a TOML file on any Python this project supports.

    `tomllib` is 3.11+, and `requires-python` is `>=3.10` with 3.10 in the test matrix -- so
    the first version of this raised `ModuleNotFoundError: No module named 'tomllib'` there,
    which surfaced as the extras check failing for a reason that had nothing to do with
    extras. `tomli` is the same parser under its pre-stdlib name and is carried in the `test`
    extra for 3.10 only.
    """
    try:
        import tomllib
    except ModuleNotFoundError:                       # Python 3.10
        try:
            import tomli as tomllib                   # type: ignore[no-redef]
        except ModuleNotFoundError:
            raise GateFailure(
                "cannot parse pyproject.toml: this Python has no `tomllib` (3.11+) and "
                "`tomli` is not installed. Install the toolkit's `test` extra, which "
                "carries it for 3.10.")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def check_requirements_extras(requirements: Path, pyproject: Path) -> set[str]:
    """Every extra the template depends on must be declared. Returns them."""
    extras = requirements_extras(requirements)
    if not extras:
        raise GateFailure(
            f"{requirements.name}: found no `corpus-toolkit[...]` extras. The template pins "
            f"the toolkit with extras every corpus depends on; finding none means this "
            f"check is reading the wrong line, not that none are needed.")
    declared = set(_read_toml(pyproject).get("project", {})
                   .get("optional-dependencies", {}))
    missing = sorted(extras - declared)
    if missing:
        raise GateFailure(
            f"{requirements.name} depends on extra(s) {missing} that pyproject.toml does not "
            f"declare (it has {sorted(declared)}). pip only WARNS on an unknown extra, so "
            f"every corpus image would build green and silently lose what the extra carries.")
    return extras


def check_template_start_surface(template: Path) -> None:
    """The template's `CMD` argv parses, and every extra it names is declared.

    Both are consumed surface in the same file corpus-toolkit#100 fixed the `RUN` in, and
    both fail identically: unit tests green, entrypoints green, this gate green, and every
    corpus broken (corpus-toolkit#116).
    """
    from corpus_toolkit.mcp.server import build_arg_parser

    argv = dockerfile_cmd_argv(template / "Dockerfile")
    if argv[0] != "corpus-mcp-serve":
        raise GateFailure(
            f"the template's CMD starts {argv[0]!r}, not `corpus-mcp-serve`. If that is "
            f"deliberate this check needs revisiting; refusing rather than validating an "
            f"argv against a parser that does not belong to it.")
    # The template's `{{CORPUS_ID}}` is still unfilled here -- this step runs against the
    # TEMPLATE, not the instantiated copy, so the placeholder is part of a path value and
    # argparse neither knows nor cares.
    try:
        build_arg_parser().parse_args(argv[1:])
    except SystemExit as e:
        raise GateFailure(
            f"the template's CMD is not accepted by `corpus-mcp-serve`'s own parser "
            f"(argparse exited {e.code}). Every corpus container starts with this argv, so "
            f"this is a crash loop on every corpus — and `--help` would still have exited 0.")
    say(f"  CMD accepted: {' '.join(argv[1:])}")

    extras = check_requirements_extras(
        template / "requirements.txt",
        Path(__file__).resolve().parent.parent.parent / "pyproject.toml")
    say(f"  requirements extras all declared: {sorted(extras)}")


def _smoke_slug(root) -> str:
    """The issuing-body slug the smoke corpus actually resolved, read from its own index.

    Hardcoding one is how this step's `documents_by_agency` leg came to assert nothing: the
    template's single document lives under an unscoped root and carries a free-text
    `issuing_body`, so any guessed slug matches zero documents and the round-trip compares
    `[] == []`.
    """
    from corpus_toolkit.config import load as load_config
    from corpus_toolkit.mcp.backends import FileBackend

    con = FileBackend(load_config(root / "_meta" / "corpus.yml")).ensure_index()
    row = con.execute("SELECT issuing_body_slug FROM docs "
                      "WHERE issuing_body_slug != '' LIMIT 1").fetchone()
    return row[0] if row else ""


def dockerfile_build_commands(dockerfile: Path) -> tuple[list[str], list[str]]:
    """(commands to run, steps deliberately skipped) from a Dockerfile's RUN instructions.

    Raises GateFailure on a step it does not recognise, and on a Dockerfile that yields no
    runnable step at all -- finding nothing and reporting success is the defect, not a
    quiet pass.
    """
    text = dockerfile.read_text(encoding="utf-8")

    # STRIP COMMENTS FIRST, because that is the order Docker uses. Joining first lets a
    # comment line ending in `\` glue the next instruction into the comment, and the whole
    # RUN disappears -- silently, because the OTHER RUN still yields a command so the
    # emptiness guard below never fires. These Dockerfiles carry long comment blocks with
    # embedded shell examples directly above the toolkit RUN, and a comment INSIDE a
    # continued RUN is legal Docker and truncated the chain the same way.
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))

    # Join line continuations THE WAY A SHELL DOES: remove `\<newline>` entirely, and do
    # not touch the whitespace around it. Replacing it with a space instead put a leading
    # space inside the `python3 -c` payload, which is an IndentationError -- the command
    # then failed for a reason unrelated to the toolkit, on every run.
    joined = re.sub(r"\\\n", "", text)

    run_steps: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        # Any whitespace after the instruction, not a literal space: `RUN\tpython3 ...` is
        # legal Docker, and requiring a space made this blind to the step rather than
        # refusing it. A step the gate cannot SEE is worse than one it refuses.
        m = re.match(r"(?i)RUN\s+(.*)", stripped)
        if not m:
            continue
        body = m.group(1).strip()
        # `&&` inside the quoted `python3 -c` payload would break this split. The template
        # uses `;` inside those strings, and an unrecognised fragment fails loudly below,
        # so a future payload containing `&&` surfaces as a refusal rather than a misparse.
        run_steps.extend(part.strip() for part in body.split("&&") if part.strip())

    to_run, skipped = [], []
    for step in run_steps:
        if step.startswith(_CONTAINER_ONLY_PREFIXES):
            skipped.append(step)
        elif step.startswith(_RUNNABLE_PREFIXES):
            _refuse_shell_tail(dockerfile, step)
            to_run.append(step)
        else:
            raise GateFailure(
                f"{dockerfile}: unrecognised build step {step!r}. This gate runs the "
                f"template's toolkit-facing build commands and refuses steps it does not "
                f"understand, because skipping one silently is how corpus-toolkit#75 "
                f"shipped. Teach it this shape, or move the step out of the RUN.")

    if not to_run:
        raise GateFailure(
            f"{dockerfile}: found no toolkit-facing build command to run. The template is "
            f"expected to exercise the toolkit at image build; a gate that extracts "
            f"nothing and passes is corpus-toolkit#100 all over again.")
    return to_run, skipped


def check_template_build_commands(dest: Path, template: Path) -> None:
    """Run corpus-template's own build-time commands against the candidate toolkit.

    At `cwd = dest` -- the instantiated scratch corpus -- because that is what the
    Dockerfile's WORKDIR is at image build: the corpus root, with `_meta/corpus.yml`
    beside it.

    Reads the INSTANTIATED Dockerfile from `dest`, not the raw one from `template`. The raw
    copy still carries `{{CORPUS_ID}}` placeholders, so a RUN that ever referenced one would
    execute the literal brace text while running in a directory where it is filled.
    """
    # A shell `python3` is not necessarily this interpreter. In the GitHub job they
    # coincide; run locally from a venv it is the SYSTEM python, which on a machine with a
    # released corpus-toolkit installed imports THAT one -- and the acceptance test for
    # #100 ("delete ensure_index, the gate goes red") would then pass green against the
    # wrong toolkit, which is worse than not running it.
    probe = subprocess.run(
        "python3 -c \"import corpus_toolkit,sys;print(corpus_toolkit.__file__)\"",
        shell=True, capture_output=True, text=True, timeout=60)
    import corpus_toolkit as _ct
    if probe.returncode != 0 or Path(probe.stdout.strip()).resolve() != Path(_ct.__file__).resolve():
        raise GateFailure(
            f"the shell's `python3` does not import the toolkit under test.\n"
            f"    shell python3 -> {probe.stdout.strip() or probe.stderr.strip()}\n"
            f"    this gate     -> {_ct.__file__}\n"
            f"  The template's build commands would run against the wrong toolkit, so a "
            f"deleted method would not be caught. Run the gate with the candidate toolkit "
            f"on the same interpreter `python3` resolves to.")

    to_run, skipped = dockerfile_build_commands(dest / "Dockerfile")
    for step in skipped:
        say(f"  skipped (container-only): {step}")
    for step in to_run:
        say(f"  running: {step}")
        # Bounded: `_RUNNABLE_PREFIXES` admits any `corpus-*` console script, and one that
        # blocks (a server, a network fetch) would hang the gate until the job's cap,
        # losing every step after it.
        proc = subprocess.run(step, shell=True, cwd=dest, capture_output=True, text=True,
                              timeout=300)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
            raise GateFailure(
                f"the template's own build command failed against this toolkit:\n"
                f"    {step}\n  " + "\n  ".join(tail))
    say(f"  OK: {len(to_run)} build command(s) from the template's Dockerfile")


SMOKE_TOOLS = """\
from corpus_toolkit.mcp.responses import ResponseEnvelope


def register(mcp, framework):
    @mcp.tool()
    def list_datasets() -> dict:
        \"\"\"Smoke fixture: the extension surface as the live corpora ship it TODAY —
        bare `-> dict`, no envelope. Kept so the gate keeps covering what is deployed.\"\"\"
        return {"datasets": [], "note": "contract-smoke fixture"}

    @mcp.tool()
    def join_lookup(document_id: str = "") -> ResponseEnvelope:
        \"\"\"Smoke fixture: the CONFORMING extension surface (corpus-toolkit#96).

        Here because the gate could not otherwise notice this path exists. The marshalling
        step skips a tool with no declared schema, and the convention-1 check below is
        keyed on the schema having properties at all — so a corpus registering only
        bare-`dict` tools is exempt by construction, and nothing end-to-end ever built a
        conforming extension tool against a real template-instantiated corpus on either SDK
        major. That is exactly what this gate exists for.\"\"\"
        return framework.with_envelope({"document_id": document_id, "rows": []})
"""


def hybridize(dest: Path) -> None:
    """Turn the scratch corpus hybrid: archetype + a minimal tools_module."""
    cy = dest / "_meta" / "corpus.yml"
    text = cy.read_text(encoding="utf-8")
    text = text.replace("archetype: \"document\"", "archetype: \"hybrid\"", 1)
    text = text.replace('archetype: document', 'archetype: hybrid', 1)
    if "hybrid" not in text:
        raise GateFailure("could not flip the template's archetype to hybrid")
    text = text.replace("plugins:", "plugins:\n  tools_module: \"src.smoke_tools:register\"", 1)
    cy.write_text(text, encoding="utf-8")
    (dest / "src" / "smoke_tools.py").write_text(SMOKE_TOOLS, encoding="utf-8")


def check_hybrid_enforcement(dest: Path) -> None:
    """The v1.19.0 promise, both directions: a hybrid WITHOUT a tools_module must refuse
    to start (a declared archetype is a promise about the tool surface —
    oregon-legislature#11 served six tools under a hybrid banner for a week), and a
    hybrid WITH one must serve the extension tools alongside the core set."""
    import subprocess
    # Negative: strip the tools_module, expect a refusal naming the contract.
    cy = dest / "_meta" / "corpus.yml"
    with_tools = cy.read_text(encoding="utf-8")
    cy.write_text(with_tools.replace(
        '  tools_module: "src.smoke_tools:register"\n', ""), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-c",
         "from corpus_toolkit import config as c; from corpus_toolkit.mcp.server import "
         "build_server; build_server(c.load('_meta/corpus.yml'))"],
        cwd=dest, capture_output=True, text=True)
    if r.returncode == 0:
        raise GateFailure("a hybrid corpus with NO tools_module built a server cleanly — "
                          "the #38 enforcement is not firing")
    if "tools_module" not in (r.stderr + r.stdout):
        raise GateFailure(f"hybrid-without-tools failed for the wrong reason: {r.stderr[-400:]}")
    cy.write_text(with_tools, encoding="utf-8")
    # Positive: with the fixture, the extension tool must be on the surface.
    r = subprocess.run(
        [sys.executable, "-c",
         "from corpus_toolkit import config as c; from corpus_toolkit.mcp import sdk; "
         "from corpus_toolkit.mcp.server import build_server; "
         "m = build_server(c.load('_meta/corpus.yml')); "
         "names = sdk.tool_names(m); "
         "assert 'list_datasets' in names, names; print(sorted(names))"],
        cwd=dest, capture_output=True, text=True)
    if r.returncode != 0:
        raise GateFailure(f"hybrid corpus failed to serve its extension tools:\n{r.stderr[-600:]}")
    say(f"  hybrid surface: {r.stdout.strip()}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", required=True, type=Path,
                    help="path to a corpus-template checkout")
    ap.add_argument("--keep", action="store_true",
                    help="leave the scratch corpus on disk for inspection")
    args = ap.parse_args()

    if not (args.template / "_meta" / "corpus.yml").is_file():
        say(f"ERROR: {args.template} does not look like corpus-template "
            f"(no _meta/corpus.yml)")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="contract-smoke-"))
    dest = workdir / "smoke-corpus"
    steps = [
        ("instantiate the template", lambda: instantiate(args.template, dest)),
        ("write a real document + snapshot", lambda: write_document(dest)),
        ("make it a git repo", lambda: git_init(dest)),
        # BEFORE the hybrid flip, on the pristine document corpus, and early because
        # "can a corpus built from this template start at all" is the most
        # fundamental thing here -- and the one that broke every image for two
        # releases (corpus-toolkit#100).
        ("the template's own Dockerfile build commands",
         lambda: check_template_build_commands(dest, args.template)),
        # BESIDE the RUN check, because they are the same class: consumed surface in the
        # same file that nothing executed. The RUN is how the image is BUILT; the CMD is
        # how the container STARTS, and `--help` answers 0 whatever options exist
        # (corpus-toolkit#116).
        ("the template's CMD argv and requirements extras",
         lambda: check_template_start_surface(args.template)),
        ("toolkit CLI gates", lambda: run_cli_gates(dest)),
        ("drift: seed, detect, report (ADR 0015)", lambda: check_drift(dest)),
        ("mandatory MCP contract tools", lambda: check_mcp_tools(dest)),
        # The hybrid leg reuses the SAME scratch corpus: flip the archetype, add the
        # minimal tools fixture, and assert #38's enforcement in both directions. An
        # inner step rather than a matrix axis, so the gate stays one job per SDK major.
        ("hybrid: archetype flip + tools fixture", lambda: hybridize(dest)),
        ("hybrid: enforcement refuses/serves correctly", lambda: check_hybrid_enforcement(dest)),
        # LAST, and on the hybrid corpus deliberately: by this point the scratch corpus
        # serves the core tools AND a tools_module extension tool, so one pass covers both
        # surfaces. Every step above asserts the answer a tool computed; this one asserts
        # the answer a client receives (corpus-toolkit#63).
        ("every tool result survives serialization", lambda: check_result_marshalling(dest)),
    ]
    try:
        for i, (label, fn) in enumerate(steps, 1):
            say(f"\n[{i}/{len(steps)}] {label}")
            try:
                fn()
            except GateFailure as e:
                say(f"\nRELEASE GATE FAILED at step {i} ({label}):\n  {e}")
                return 1
            except Exception as e:                               # noqa: BLE001
                say(f"\nRELEASE GATE FAILED at step {i} ({label}): "
                    f"{type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                return 1
        say("\nRELEASE GATE PASSED: a corpus instantiated from corpus-template validates "
            "and serves every mandatory contract tool on this ref.")
        return 0
    finally:
        if args.keep:
            say(f"scratch corpus kept at {dest}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
