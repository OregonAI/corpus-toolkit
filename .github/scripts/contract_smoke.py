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

Step 4 is the point. A tool that is registered and raises on every call passes a
`tools/list` check and fails a user.

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
    from corpus_toolkit.mcp.server import build_server

    config = config_mod.load(dest / "_meta" / "corpus.yml")
    mcp = build_server(config)
    listed = {t.name for t in mcp._tool_manager.list_tools()}
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
            results[name] = asyncio.run(mcp._tool_manager.call_tool(name, args))
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
