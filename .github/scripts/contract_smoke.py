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
  4. build the MCP server the way `corpus-mcp-serve` does and CALL every mandatory core
     tool from docs/mcp-interface-contract.md through the SDK's own tool manager,
     asserting the ANSWER, not merely that a name is registered
  5. push every tool's real answer through the SDK's own result conversion and assert the
     payload a CLIENT receives is the payload the tool returned

Step 4 is the point. A tool that is registered and raises on every call passes a
`tools/list` check and fails a user.

Step 5 is the other half of it, and it exists because step 4 alone was not enough: a tool
that answers correctly and whose answer is then discarded at serialization passes step 4
and still fails a user. v1.24.0 did precisely that — `get_document` returned three envelope
fields and no document body, reported success, and shipped through this gate GREEN, because
every call here goes through `_sdk.call_tool(..., convert_result=False)`
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
    # Must be a real URL: corpus-validate-frontmatter errors on a non-URL here, which is
    # itself something worth exercising on every release.
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


def check_mcp_tools(dest: Path) -> None:
    """Build the server exactly as corpus-mcp-serve does and CALL every mandatory tool.

    Registration is checked AND answers are checked. Asserting only that a name appears
    in tools/list is the cheap version of this test and would have passed throughout the
    entire lifetime of both graph bugs: graph_neighbors was registered, listed, and
    raised KeyError on every call in oregon-records-retention."""
    from corpus_toolkit import config as config_mod
    from corpus_toolkit.mcp import _sdk
    from corpus_toolkit.mcp.server import build_server

    config = config_mod.load(dest / "_meta" / "corpus.yml")
    mcp = build_server(config)
    listed = _sdk.tool_names(mcp)
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
            results[name] = asyncio.run(_sdk.call_tool(mcp, name, args))
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
    `tests/` — goes through `_sdk.call_tool`, which passes `convert_result=False`. That is
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
    from corpus_toolkit.mcp import _sdk
    from corpus_toolkit.mcp.server import build_server

    config = config_mod.load(dest / "_meta" / "corpus.yml")
    mcp = build_server(config)
    tools = _sdk.tools_by_name(mcp)

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
    ]
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
        raw = asyncio.run(_sdk.call_tool(mcp, name, args))
        if name == "search_corpus" and not any(h.get("id") == DOC_ID for h in raw):
            # Otherwise this tool's whole leg is `[] == []`: every assertion below holds
            # for an empty answer. Step 5 asserts hits exist, but against the server it
            # built BEFORE the hybrid flip — this step rebuilds, so it must ask again.
            problems.append(f"search_corpus returned {len(raw)} hit(s), none of them "
                            f"{DOC_ID} — the per-hit assertions below would pass over an "
                            f"empty list and prove nothing")
        try:
            texts, structured = _sdk.serialized_result(tools[name], raw)
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
        if _sdk.declares_list_result(tool) or getattr(tool, "output_schema", None) is None:
            continue
        payload = {"corpus": "smoke-corpus", "archetype": "hybrid",
                   "authoritative_source": None, "detail": "tool-specific payload"}
        try:
            out = _sdk.structured_result(tool, payload)
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

SMOKE_TOOLS = """\
def register(mcp, framework):
    @mcp.tool()
    def list_datasets() -> dict:
        \"\"\"Smoke fixture: the minimal hybrid extension surface.\"\"\"
        return {"datasets": [], "note": "contract-smoke fixture"}
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
         "from corpus_toolkit import config as c; from corpus_toolkit.mcp import _sdk; "
         "from corpus_toolkit.mcp.server import build_server; "
         "m = build_server(c.load('_meta/corpus.yml')); "
         "names = _sdk.tool_names(m); "
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
        ("toolkit CLI gates", lambda: run_cli_gates(dest)),
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
