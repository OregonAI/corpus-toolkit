#!/usr/bin/env python3
"""corpus-validate-frontmatter — validate content-file frontmatter against the
corpus's JSON schema, check id/filename agreement, the non-authoritative
disclaimer, directory<->doc_type placement (from `_meta/corpus.yml`
content_roots), the relationships graph, an optional issuing-body registry,
and any corpus-declared extra schema checks. Ported from
oregon-policy-repo/src/validate_frontmatter.py; Oregon-specific hardcoding
(CONTENT_DIRS, DIR_DOC_TYPE, the agency registry file) replaced by config-
driven equivalents — see docs/reference-architecture.md and MIGRATION.md.

  --check-relationships   run ONLY the relationship-graph resolution (used by
                          the check-links reusable workflow; no --schema needed)
  --changed [REF]         validate only files changed vs REF (merge-base with
                          origin/main if omitted)
  -j N / --jobs N         parallelize per-file checks across N processes
"""
import argparse
import json
import multiprocessing as mp
import os
import re

import jsonschema
import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit.repo import (
    Reporter, changed_content_files, content_files, parse_frontmatter,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")

_VALIDATOR = None
_CONFIG = None
_REGISTRY = None


def _init_worker(schema_dict, config, registry):
    global _VALIDATOR, _CONFIG, _REGISTRY
    _VALIDATOR = jsonschema.Draft202012Validator(schema_dict)
    _CONFIG = config
    _REGISTRY = registry


def check_file(path):
    """Per-file frontmatter checks. Returns (rel, findings, doc_id) where
    findings is a list of ('error'|'warn', message). Relationship-target
    resolution is NOT done here (needs the corpus-wide id set); the caller
    handles it."""
    config = _CONFIG
    rel = path.relative_to(config.root)
    findings = []
    try:
        fm, body = parse_frontmatter(path)
    except ValueError as e:
        return rel, [("error", str(e))], None

    for err in sorted(_VALIDATOR.iter_errors(fm), key=str):
        where = "/".join(str(x) for x in err.path) or "(root)"
        findings.append(("error", f"schema: {where}: {err.message}"))
    if fm.get("id") != path.stem:
        findings.append(("error", f"id '{fm.get('id')}' != filename stem '{path.stem}'"))
    if config.disclaimer_marker not in body:
        findings.append(("error", f"missing '{config.disclaimer_marker}' disclaimer marker in body"))

    parts = rel.parts
    doc_type = fm.get("doc_type")
    expected_here = config.doc_type_for(parts)
    if expected_here is not None:
        if expected_here != doc_type:
            findings.append(("error", f"doc_type '{doc_type}' does not belong under "
                            f"'{'/'.join(parts[:1])}/' here (expected doc_type '{expected_here}')"))
    else:
        er = config.expected_root_for(doc_type)
        if er is not None:
            findings.append(("error", f"doc_type '{doc_type}' belongs under a "
                            f"'{er.path}/' content root, not here"))

    if _REGISTRY is not None and expected_here is not None:
        # only scoped content roots (issuing-body-scoped dirs) carry a slug segment
        for cr in config.content_roots:
            if cr.path == parts[0] and cr.scoped:
                slug = parts[1] if len(parts) > 1 else None
                if slug not in _REGISTRY:
                    findings.append(("error", f"issuing-body slug '{slug}' is not in the "
                                    "issuing-body registry (see plugins.issuing_body_registry "
                                    "in _meta/corpus.yml)"))
                break

    return rel, findings, fm.get("id")


def _relationship_findings(paths, universe, config):
    """Slug-shaped relationship targets must resolve to an in-corpus document id;
    citation-shaped targets (e.g. 'ORS 276A.300') are allowed as forward references
    to not-yet-ingested documents or sibling corpora."""
    out = []
    for path in paths:
        rel = path.relative_to(config.root)
        try:
            fm, _ = parse_frontmatter(path)
        except ValueError:
            continue
        for edge, targets in (fm.get("relationships") or {}).items():
            for t in targets or []:
                if t in universe:
                    continue
                if SLUG_RE.match(t):
                    out.append((rel, "error", f"relationships.{edge}: '{t}' does not resolve to any document"))
                else:
                    out.append((rel, "warn", f"relationships.{edge}: '{t}' is a citation, not yet ingested"))
    return out


def _join_findings(paths, universe, config):
    """`joins[].document_id` must resolve to a document in this corpus.

    The schema validated the SHAPE of a joins entry — {document_id, dataset, key} — and
    nothing anywhere read it. A hybrid corpus could therefore ship joins pointing at
    documents that do not exist and every gate in the platform stayed green, which is the
    worst possible failure mode for this particular field: a join is what lets an agent
    state that *this appropriation* relates to *that spending*, and a join pointing at a
    nonexistent document does not error, it just answers nothing. "No relationship
    recorded" is indistinguishable from "no relationship exists". (oregon-budget alone
    carries 836 join entries across 418 documents; corpus-toolkit#3.)

    A `document_id` is a document reference BY CONSTRUCTION — unlike a relationships
    target, which is legitimately allowed to be a citation string for a sibling corpus —
    so a dangling one is an error, not a warning.

    `{dataset, key}` is deliberately NOT checked here and cannot be: only the corpus
    knows what one of its dataset keys means or which rows it should select. That check
    belongs in the corpus's own `--check` step (see docs/provenance-schema-v1.md), and
    the toolkit says so rather than implying coverage it does not have."""
    out = []
    for path in paths:
        rel = path.relative_to(config.root)
        try:
            fm, _ = parse_frontmatter(path)
        except ValueError:
            continue
        for i, entry in enumerate(fm.get("joins") or []):
            if not isinstance(entry, dict):
                continue                      # shape is the schema's job, not this one's
            target = entry.get("document_id")
            if target and target not in universe:
                out.append((rel, "error",
                            f"joins[{i}].document_id: '{target}' does not resolve to any "
                            f"document in this corpus"))
    return out


SCHEMA_NAME = "document.frontmatter.v1.schema.json"


def bundled_schema() -> dict:
    """The frontmatter schema shipped inside this package.

    It lives here rather than at the repo root so that `pip install corpus-toolkit` is
    enough to validate a corpus. Previously the only copy was reachable at
    `.toolkit/schemas/...`, a path created solely by the reusable workflows' second
    checkout — so every command in every corpus's CONTRIBUTING/AGENTS docs was unrunnable
    for an actual contributor, and the definition-of-done for a content PR could only be
    met by pushing and waiting for CI.
    """
    from importlib.resources import files
    return json.loads(files("corpus_toolkit").joinpath("schemas", SCHEMA_NAME)
                      .read_text(encoding="utf-8"))


def _graph_node_ids(config):
    """All document ids known to the (CI-fresh) authority graph — a fast corpus-wide
    universe for relationship resolution without re-parsing every frontmatter."""
    if not config.graph_path.is_file():
        return set()
    return {n["id"] for n in json.loads(config.graph_path.read_text()).get("nodes", [])}


def _all_content_ids(config):
    """Every document id in the corpus, by parsing frontmatter.

    The fallback when there is no authority graph. It exists because the resolution
    UNIVERSE must be corpus-wide even when the set being VALIDATED is not: scoping both
    to the changed files makes every relationship pointing at an unchanged sibling look
    unresolvable. A corpus with no graph.json — legitimate, and the documented state for a
    corpus that has not built one yet — otherwise gets a universe consisting only of the
    files in the diff, so a one-file PR fails on references that are perfectly valid.

    Slower than reading the graph (it parses every frontmatter), which is why it is a
    fallback rather than the default.
    """
    ids = set()
    for p in content_files(config):
        try:
            fm, _ = parse_frontmatter(p)
        except ValueError:
            continue
        if fm.get("id"):
            ids.add(fm["id"])
    return ids


def _resolution_universe(config, docs):
    """Ids a relationship target may resolve to. Corpus-wide by construction — see
    _all_content_ids for why that matters when validation is scoped to changed files."""
    return (_graph_node_ids(config) or _all_content_ids(config)) | set(docs)


def _load_registry(config):
    if not config.issuing_body_registry:
        return None
    data = yaml.safe_load(config.issuing_body_registry.read_text()) or {}
    return {e["slug"] for e in data.get(config.issuing_body_registry_key, [])}


def _check_config(config, r):
    """Corpus-level config checks — things the per-document schema cannot see.

    `corpus.authoritative_source` is required by docs/mcp-interface-contract.md response
    convention 1 and was carried by none of the four live corpora (corpus-toolkit#6): the
    "call this first" tool told every agent the copy was non-authoritative and to "verify
    at source" without ever saying where the source is.

    WARN, NOT ERROR, and deliberately so. Every existing corpus omits the key, and a hard
    failure here would turn all four CIs red on the next toolkit pin bump — punishing them
    for a gap in the shared layer. Promote it to an error once they have all declared one;
    tracked in corpus-toolkit#11. A corpus adopts it by adding one line under `corpus:` in
    `_meta/corpus.yml`:

        authoritative_source: "https://www.oregonlegislature.gov/bills_laws/Pages/ORS.aspx"
    """
    rel = config.config_path.relative_to(config.root)
    if not config.authoritative_source:
        r.warn(rel, "corpus.authoritative_source is not set — MCP responses will carry "
                    "`authoritative_source: null`, so an agent is told to verify at "
                    "source without being told where the source is (response "
                    "convention 1). Set it to the URL where the official text lives.")
    elif not str(config.authoritative_source).startswith(("http://", "https://")):
        # A non-URL here is worse than nothing: convention 1 says the field IS a URL, so a
        # caller will try to follow it.
        r.error(rel, f"corpus.authoritative_source must be a URL, got "
                     f"{config.authoritative_source!r}")


def _check_extra_schemas(config, r):
    """Validate corpus-declared {path, schema} pairs against a JSON schema.
    `path` may be a glob (e.g. `_meta/sources/*.yml`) to validate many files
    against the same schema — a corpus with per-group source manifests, for
    example, doesn't have just one file to check."""
    for check in config.extra_schema_checks:
        schema_path = (config.root / check["schema"]).resolve()
        try:
            schema = json.loads(schema_path.read_text())
        except FileNotFoundError as e:
            r.error(check["schema"], f"missing schema: {e}")
            continue
        validator = jsonschema.Draft202012Validator(schema)

        if any(ch in check["path"] for ch in "*?["):
            data_paths = sorted(config.root.glob(check["path"]))
            if not data_paths:
                r.warn(check["path"], "glob matched no files")
        else:
            data_paths = [(config.root / check["path"]).resolve()]

        for data_path in data_paths:
            rel = data_path.relative_to(config.root)
            try:
                text = data_path.read_text()
                data = json.loads(text) if data_path.suffix == ".json" else yaml.safe_load(text)
                for err in sorted(validator.iter_errors(data), key=str):
                    r.error(rel, f"schema: {err.message[:200]}")
            except FileNotFoundError as e:
                r.error(rel, f"missing: {e}")


def _run_relationships_only(config, paths, r):
    docs = {}
    for p in paths:
        try:
            fm, _ = parse_frontmatter(p)
        except ValueError as e:
            r.error(p.relative_to(config.root), str(e))
            continue
        if fm.get("id"):
            docs[fm["id"]] = p.relative_to(config.root)
    universe = _resolution_universe(config, docs)
    for rel, level, msg in (_relationship_findings(paths, universe, config)
                            + _join_findings(paths, universe, config)):
        (r.error if level == "error" else r.warn)(rel, msg)
    r.finish(f"OK: relationship graph consistent across {len(paths)} content file(s).")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--schema", help="path to a frontmatter JSON schema. Defaults to the "
                    "one bundled with this package, which is what CI validates against — "
                    "pass this only to validate against a different schema.")
    ap.add_argument("--check-relationships", action="store_true",
                    help="run only the relationship-graph resolution check")
    ap.add_argument("--changed", nargs="?", const="", metavar="REF",
                    help="validate only files changed vs REF (default: merge-base with origin/main)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="worker processes (default: all CPUs)")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    scoped = args.changed is not None
    if scoped:
        paths = changed_content_files(config, args.changed or None)
        if not paths:
            print("No changed content files to validate.")
            if args.check_relationships:
                return
    else:
        paths = list(content_files(config))

    r = Reporter()

    if args.check_relationships:
        _run_relationships_only(config, paths, r)
        return

    doc_schema = json.loads(open(args.schema).read()) if args.schema else bundled_schema()
    registry = _load_registry(config)

    docs = {}
    jobs = max(1, args.jobs)
    if jobs == 1 or len(paths) < 50:
        _init_worker(doc_schema, config, registry)
        results = [check_file(p) for p in paths]
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(jobs, initializer=_init_worker,
                      initargs=(doc_schema, config, registry)) as pool:
            results = list(pool.imap_unordered(check_file, paths, chunksize=64))
    for rel, findings, doc_id in results:
        for level, msg in findings:
            (r.error if level == "error" else r.warn)(rel, msg)
        if doc_id is not None:
            docs[doc_id] = rel

    universe = _resolution_universe(config, docs)
    for rel, level, msg in (_relationship_findings(paths, universe, config)
                            + _join_findings(paths, universe, config)):
        (r.error if level == "error" else r.warn)(rel, msg)

    _check_config(config, r)
    _check_extra_schemas(config, r)

    scope = f"{len(paths)} changed" if scoped else f"{len(paths)}"
    r.finish(f"OK: {scope} content file(s) validated across {', '.join(config.content_dirs)}.")


if __name__ == "__main__":
    main()
