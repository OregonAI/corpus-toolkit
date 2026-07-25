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


def _graph_node_ids(config):
    """All document ids known to the (CI-fresh) authority graph — a fast corpus-wide
    universe for relationship resolution without re-parsing every frontmatter."""
    if not config.graph_path.is_file():
        return set()
    return {n["id"] for n in json.loads(config.graph_path.read_text()).get("nodes", [])}


def _load_registry(config):
    if not config.issuing_body_registry:
        return None
    data = yaml.safe_load(config.issuing_body_registry.read_text()) or {}
    return {e["slug"] for e in data.get(config.issuing_body_registry_key, [])}


def _check_extra_schemas(config, r):
    for check in config.extra_schema_checks:
        data_path = (config.root / check["path"]).resolve()
        schema_path = (config.root / check["schema"]).resolve()
        rel = data_path.relative_to(config.root)
        try:
            schema = json.loads(schema_path.read_text())
            text = data_path.read_text()
            data = json.loads(text) if data_path.suffix == ".json" else yaml.safe_load(text)
            for err in sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=str):
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
    universe = _graph_node_ids(config) | set(docs)
    for rel, level, msg in _relationship_findings(paths, universe, config):
        (r.error if level == "error" else r.warn)(rel, msg)
    r.finish(f"OK: relationship graph consistent across {len(paths)} content file(s).")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--schema", help="path to document.frontmatter.v1.schema.json "
                    "(required unless --check-relationships)")
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

    if not args.schema:
        ap.error("--schema is required unless --check-relationships is set")

    doc_schema = json.loads(open(args.schema).read())
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

    universe = _graph_node_ids(config) | set(docs)
    for rel, level, msg in _relationship_findings(paths, universe, config):
        (r.error if level == "error" else r.warn)(rel, msg)

    _check_extra_schemas(config, r)

    scope = f"{len(paths)} changed" if scoped else f"{len(paths)}"
    r.finish(f"OK: {scope} content file(s) validated across {', '.join(config.content_dirs)}.")


if __name__ == "__main__":
    main()
