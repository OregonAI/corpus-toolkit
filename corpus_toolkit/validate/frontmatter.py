#!/usr/bin/env python3
"""corpus-validate-frontmatter — validate content-file frontmatter against the
corpus's JSON schema, check id/filename agreement, the non-authoritative
disclaimer, directory<->doc_type placement (from `_meta/corpus.yml`
content_roots), the relationships graph, an optional issuing-body registry,
and any corpus-declared extra schema checks. Ported from
oregon-policy-repo/src/validate_frontmatter.py; Oregon-specific hardcoding
(CONTENT_DIRS, DIR_DOC_TYPE, the agency registry file) replaced by config-
driven equivalents — see docs/reference-architecture.md and MIGRATION.md.

  --check-relationships   skip the per-document schema checks: run the
                          relationship-graph and join resolution plus the
                          corpus-level config checks (used by the check-links
                          reusable workflow; no --schema needed). The config
                          checks are NOT skipped — see the flag's definition in
                          main() for why (corpus-toolkit#139)
  --changed [REF]         validate only files changed vs REF (merge-base with
                          origin/main if omitted)
  -j N / --jobs N         parallelize per-file checks across N processes
"""
import argparse
import json
import os
import re

import jsonschema
import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit.config import name_values
from corpus_toolkit.repo import (
    Reporter, changed_content_files, content_files, map_documents, parse_frontmatter,
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

    # THE DECLARED HALF OF THE JOIN, checked (corpus-toolkit#94). The path-derived half
    # below has failed CI since it existed; `plugins.issuing_body_slug_field` had no
    # equivalent, so a misspelling attributed a document to a body that does not exist and
    # nothing said so — it simply reached no per-agency count.
    #
    # This is also what makes `issuing_body_slug_sentinels` safe rather than a mute button.
    # Without it, declaring sentinels would be a way to silence the coverage warning instead
    # of answering it, and a genuine typo would stay invisible in the same bucket.
    #
    # Checked for EVERY document, not only those under a scoped root: the declared field is
    # the only join for a chapter-organised corpus, which is 98.7% of ERF.
    if _REGISTRY is not None and config.issuing_body_slug_field:
        declared = str(fm.get(config.issuing_body_slug_field) or "").strip()
        if declared and declared not in _REGISTRY:
            if declared not in config.issuing_body_slug_sentinels:
                findings.append(("error", (
                    f"{config.issuing_body_slug_field} '{declared}' is not in the "
                    f"issuing-body registry. If it names a body, fix the spelling or add "
                    f"it to the registry; if it deliberately means 'no issuing body', "
                    f"declare it in plugins.issuing_body_slug_sentinels")))

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


def schema_with_extensions(doc_schema, config):
    """The shared schema, plus this corpus's declared doc_types (corpus-toolkit#40).

    Extends exactly two spots: the doc_type enum, and — for types declared
    verbatim: true — the allOf conditional that makes provenance fields required.
    Everything else about an extended type behaves like any other document. Before
    this, "extended per corpus in corpus.yml" was a docs claim with no mechanism,
    and each new vertical cost a toolkit release."""
    extras = getattr(config, "extra_doc_types", {}) or {}
    if not extras:
        return doc_schema
    import copy
    s = copy.deepcopy(doc_schema)
    s["properties"]["doc_type"]["enum"].extend(
        n for n in extras if n not in s["properties"]["doc_type"]["enum"])
    verbatim = [n for n, v in extras.items() if v]
    for clause in s.get("allOf", []):
        cond = (clause.get("if", {}).get("properties", {}).get("doc_type", {}))
        if "enum" in cond:
            cond["enum"].extend(n for n in verbatim if n not in cond["enum"])
    return s


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


# THE NAME STAYS IMPORTABLE FROM HERE. `RegistryRead` was defined in this module until
# corpus-toolkit#136 moved it to `config`, and a name a corpus repo can import is public
# surface wherever it is defined (AGENTS.md). One binding, so there is still one class.
RegistryRead = config_mod.RegistryRead


def _read_registry(config) -> config_mod.RegistryRead:
    """This run's one read of the issuing-body registry, for every check that asks about it.

    A THIN NAME OVER `config.issuing_body_registry_read`, NOT A SECOND READER. The validator
    grew `RegistryRead` first (corpus-toolkit#129) while the runtime parsed the same file
    separately and RAISED where this reported (corpus-toolkit#136) — the same registry
    answering two ways depending on who asked. The shape moved to `config`, which both `mcp`
    and `validate` already import, so "unreadable" is decided once. This function stays
    because a corpus's own scripts may call it, and because `_check_config` reads better
    taking the read as an argument than reaching into config for it.
    """
    return config.issuing_body_registry_read


# `{{CORPUS_ID}}` and friends — corpus-template's unfilled find/replace placeholders
# (docs/replication-guide.md step 1).
_UNFILLED_PLACEHOLDER = re.compile(r"\{\{[A-Za-z0-9_]+\}\}")


def _uninstantiated_template(config) -> bool:
    """Is this corpus.yml still corpus-template's, rather than a corpus's?

    TWO CONDITIONS, AND BOTH ARE LOAD-BEARING. The id is still the unfilled
    `{{CORPUS_ID}}` — step 1 of the replication guide replaces it, and a served corpus
    cannot keep it (it is the MCP server name and the `corpus` field of every response) —
    AND the repo holds no documents, because a repo that forked the template and started
    ingesting is a corpus whatever its config still says. Either half alone would be an
    exemption a real corpus could sit in: an empty repo mid-setup has a name, and a
    populated one that never edited its config is the exact "shipped the template
    unedited" case corpus-toolkit#11 exists to catch.

    CORPUS-WIDE, not the `--changed` scope: under `--changed` a PR touching no content
    file would otherwise look like an empty repo and suspend the gate on every corpus.
    """
    if not _UNFILLED_PLACEHOLDER.search(str(config.id or "")):
        return False
    return next(iter(content_files(config)), None) is None


def _check_config(config, r, registry):
    """Corpus-level config checks — things the per-document schema cannot see.

    `corpus.authoritative_source` is required by docs/mcp-interface-contract.md response
    convention 1 and was carried by none of the four live corpora (corpus-toolkit#6): the
    "call this first" tool told every agent the copy was non-authoritative and to "verify
    at source" without ever saying where the source is.

    ERROR, NOT WARNING, SINCE corpus-toolkit#11. It shipped as a warning because every
    corpus then omitted the key and a hard failure would have turned their CIs red on the
    next pin bump — punishing them for a gap in the shared layer. That condition is spent:
    all nine live corpora declare one (the last three landed the day this changed), so the
    gate now falls only on a corpus that has not adopted it, which is what #6 asked for —
    "so new corpora cannot ship without one". A corpus adopts it by adding one line under
    `corpus:` in `_meta/corpus.yml`:

        authoritative_source: "https://www.oregonlegislature.gov/bills_laws/Pages/ORS.aspx"

    MORE THAN TWO STATES, because a value can be present and still answer nothing. A
    missing key and a value under an RFC 2606 reserved name are the same defect wearing
    different clothes: every MCP response carries the field, so both end with an agent
    told to verify at a place that does not exist. The reserved-name half is not
    hypothetical — it is what `corpus-template` ships, on purpose (see
    `_uninstantiated_template`), and an omission-only check would wave through a corpus
    that forked the template and never edited the line.

    WHAT THE STATES ARE, AND WHAT EACH ONE SAYS, IS NO LONGER DECIDED HERE. It is
    `config.front_door_fault`, which `corpus_overview` reads too (corpus-toolkit#140); this
    function chooses severity and nothing else. A value that is not a URL at all stays the
    error it has been since v1.10.0, and stays one even for the template — a caller follows
    this field, and a non-URL is the one shape nothing downstream can use.

    The message says FRONT DOOR rather than "the URL where the official text lives", and the
    difference is not cosmetic: the second phrasing reads as a promise that every document in
    the corpus sits under that URL, which for a corpus spanning several publishers is false.
    `executive-regulatory-frameworks` has never carried the key, and corpus-toolkit#70's
    triage attributes that to exactly this problem — no single URL it could name without
    being wrong about most of its sources (measurement dated on the contract page, so it is
    not restated here to go stale). The field is per-corpus and coarse by design;
    `get_document` answers per document from that document's own `source_url`
    (corpus-toolkit#70, and the contract's response convention 1).
    """
    rel = config.config_path.relative_to(config.root)
    template = _uninstantiated_template(config)
    if template:
        # SAID EVERY TIME, so the exemption is never silent — a repo sitting in this state
        # is told what it is and what it costs, whether or not it has a front door yet.
        r.warn(rel, f"corpus.id is still the template's unfilled placeholder "
                    f"{config.id!r} and this repo holds no documents, so this is "
                    f"corpus-template rather than a corpus. Any finding about "
                    f"corpus.authoritative_source is reported as a WARNING for that "
                    f"reason alone, and becomes an error the moment either half changes "
                    f"— when you fill in corpus.id, or when the first document lands.")
    if (fault := config.front_door_fault):
        # THE FINDING AND ITS WORDING COME FROM `config`, AND THIS FUNCTION ONLY DECIDES
        # HOW LOUD (corpus-toolkit#140). The other reader of this field is
        # `corpus_overview`, which used to carry a placeholder front door in silence while
        # this gate refused it — two readers of one field answering differently. They now
        # read one declaration, so the only thing left to disagree about is severity, and
        # severity is genuinely this caller's: a gate can refuse, a running server can only
        # mention.
        say = r.warn if (template and fault.exempt_while_uninstantiated) else r.error
        say(rel, fault.message)
    _check_registry(config, r, rel, registry)
    _check_profiles(config, r, rel)


def _check_profiles(config, r, rel):
    """A declared curated-profiles overlay this corpus cannot read, as a finding
    (corpus-toolkit#150).

    THE SENTENCE COMES FROM `config.issuing_body_profiles_fault`; this function decides
    severity and nothing else — the arrangement the front door and the registry already
    use above. It is a DIFFERENT sentence from the registry's, deliberately: different key,
    different file, different fix, and reporting a broken overlay in the registry's words
    sends an operator to edit the file that was never at fault (corpus-toolkit#143).

    ERROR, LIKE THE REGISTRY'S, AND FOR THE REGISTRY'S REASON. Declaring
    `plugins.issuing_body_profiles` is optional — a corpus that declares none serves
    `curated: {}` quite happily and is not reported here — but a file a corpus DID declare
    and cannot read is a config defect, not a corpus with nothing curated. Before this the
    fault had one reader, the per-call `curated_warning`, which reaches the AGENT holding
    an answer and never the person who can edit the file: a malformed overlay merged on
    green CI, deployed, and silently stopped serving every curated note on every body.

    REPORTED, NOT FATAL AT LOAD. `load()` stays tolerant so a pin bump degrades a corpus
    rather than taking it down; this is the gate that gets to refuse, and `build_server`
    is the same fault said once to the operator while the server still starts.
    """
    if (fault := config.issuing_body_profiles_fault):
        r.error(rel, f"{fault} — so no body's curated notes are served: every "
                     f"`issuing_body_profile` answer carries an empty curated block and "
                     f"says why. This is 'could not be read', not 'this corpus curates "
                     f"nothing' — fix the file, or drop the key if the overlay is gone.")


def _check_registry(config, r, rel, registry):
    """What this run learned about the issuing-body registry, as findings (corpus-toolkit#129).

    Four conditions, and keeping them apart is the whole job — each has a different cause
    and a different fix, and three of them otherwise read as "that body is not here":

      * the registry COULD NOT BE READ -> error, and nothing is claimed about its columns
      * an entry carries NO SLUG        -> error; nothing can be attributed to that row
      * the registry holds NO ENTRIES   -> warning about the registry, not about a field
      * a declared name field reaches NO NAME in any entry -> warning naming the field

    REPORTED HERE, AND REPORTED RATHER THAN FATAL. `load()` checks the declaration's SHAPE
    and refuses a corpus that declares nothing usable; what it deliberately does not check
    is whether a field exists in the registry, because a mid-migration corpus legitimately
    declares the field its registry is about to grow — ERF declared `oar_name` between
    ERF#166 and ERF#168 — and failing the load would refuse a config that is correct and
    merely early. So a typo (`oar_nmae`) loads clean, serves clean, and every free-text
    query against that field matches nothing, which from the outside is indistinguishable
    from a body that is not in the corpus.

    THIS IS THE CHANNEL CORPUS-LEVEL CONFIG FINDINGS ALREADY USE: the same function that
    reports a missing `corpus.authoritative_source`, in the same command, run by every
    corpus on every PR through the validate-frontmatter reusable workflow, printed with the
    file it is about. A corpus maintainer reads it where they already read config findings.
    It is deliberately NOT `corpus_overview`'s `config_warning`: that reaches an AGENT
    holding an answer, and is spent on the one omission that changes how the answer should
    be read; a registry column an agent cannot fix would be noise on every conversation.

    A FIELD THE REGISTRY CARRIES ON SOME ROWS IS NOT REPORTED. Half a column is a partially
    populated registry, and it still matches the bodies that have it; a field carried by no
    row at all is the one that can never match anything.

    "COULD NOT CHECK" IS NOT "IS NOT THERE". A registry that could not be read says nothing
    about which columns it has, so the fields are not reported — the read failure is, as an
    error, because a configured registry that cannot be opened also silently skips every
    per-document attribution check. An EMPTY registry is read, but a column claim about a
    registry with no rows would accuse an author of a typo they did not make, so the empty
    registry is what gets reported.
    """
    if not config.issuing_body_registry:
        # Nothing to name columns of; declaring fields in that state is refused at load.
        return
    # The same spelling of the file the MCP tools' notes use — see
    # `CorpusConfig.issuing_body_registry_rel`.
    registry_rel = config.issuing_body_registry_rel
    fields = config.issuing_body_name_fields

    if not registry.readable:
        # THE SAME SENTENCE THE MCP TOOLS SERVE — see
        # `CorpusConfig.issuing_body_registry_fault`.
        r.error(rel, f"{config.issuing_body_registry_fault} — "
                     f"so the issuing-body slug of every document went unchecked, and "
                     f"plugins.issuing_body_name_fields ({', '.join(fields)}) could not be "
                     f"checked against it either. This is 'could not check', not 'nothing "
                     f"is wrong': fix the file, then re-run.")
        return

    if registry.without_slug:
        # A row nothing can be attributed to. This used to raise KeyError out of the read —
        # a traceback naming neither file nor row, but a failure nobody could miss. Reading
        # it tolerantly and saying nothing would be the trade this repo does not make.
        r.error(rel, f"plugins.issuing_body_registry {registry_rel}: "
                     f"{registry.without_slug} entr"
                     f"{'y has' if registry.without_slug == 1 else 'ies have'} no slug, so "
                     f"nothing can be attributed to "
                     f"{'it' if registry.without_slug == 1 else 'them'} and a document "
                     f"naming that body is reported as unregistered. Give every entry a "
                     f"slug, or remove the row.")

    if not registry.entries:
        # READ, AND EMPTY. Every free-text query fails, but the cause is the empty registry
        # and not the fields: a column claim about a registry holding no rows would accuse
        # the author of a typo they did not make. Say the one true thing.
        r.warn(rel, f"plugins.issuing_body_registry {registry_rel} holds no entries, so "
                    f"`issuing_body_profile` can match no body at all and every document's "
                    f"issuing-body slug is reported as unregistered.")
        return

    unmatched = [f for f in fields
                 if not any(name_values(e, f) for e in registry.mappings)]
    if not unmatched:
        return
    # A corpus that declared nothing must not be told off for a key it never wrote: the
    # finding is the same, the fix is to DECLARE the field its registry actually carries.
    declared = "issuing_body_name_fields" in (config.raw.get("plugins") or {})
    how = ("plugins.issuing_body_name_fields" if declared else
           "plugins.issuing_body_name_fields defaults to ['name'], and this corpus declares "
           "no override")
    r.warn(rel, (
        f"{how}: no entry in {registry_rel} carries a name in "
        f"{', '.join(repr(f) for f in unmatched)} — checked {len(registry.mappings)} entr"
        f"{'y' if len(registry.mappings) == 1 else 'ies'}, and a name is a string cell or a "
        f"list of strings. A free-text `issuing_body_profile` query can never match on "
        f"{'these fields' if len(unmatched) > 1 else 'that field'}, and matching nothing "
        f"looks exactly like a body this corpus does not hold. "
        + ("If this is a typo, fix the spelling; if the registry is about to grow the "
           "column, this line goes away when it does."
           if declared else
           "Declare the field(s) your registry carries under plugins."
           "issuing_body_name_fields.")))


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


def _check_corpus_config(config, r, registry):
    """EVERY corpus-level check, as one thing, because both entry points run all of them.

    Two call sites that each list the checks are two lists to keep in agreement, and this
    module has already paid for that once: `--check-relationships` returned before
    `_check_config` was reached, so the path `check-links.yml` runs gated no front door, no
    registry, no name fields and no declared extra schemas (corpus-toolkit#139).
    """
    _check_config(config, r, registry)
    _check_extra_schemas(config, r)


def _run_relationships_only(config, paths, r, registry):
    """The `--check-relationships` path: the relationship graph, the joins, AND the
    corpus-level configuration.

    WHY THE CONFIG CHECK RUNS HERE — see the flag's definition in `main` for the decision
    and its reasoning. In short: the flag narrows which DOCUMENTS are checked, not whether
    the corpus is CONFIGURED, and the config findings are not per-document.
    """
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
    _check_corpus_config(config, r, registry)
    # THE SUMMARY SAYS WHAT WAS CHECKED. #139's complaint was that a green
    # "relationship graph consistent" reads as a full pass to someone who reached for this
    # flag as the cheap validate; naming both halves is what makes the green run readable.
    r.finish(f"OK: relationship graph consistent across {len(paths)} content file(s), "
             f"and corpus configuration checked.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--schema", help="path to a frontmatter JSON schema. Defaults to the "
                    "one bundled with this package, which is what CI validates against — "
                    "pass this only to validate against a different schema.")
    # THE DECISION, RECORDED WHERE THE FLAG IS DEFINED (corpus-toolkit#139).
    #
    # This flag ran NO corpus-level config check at all: `_run_relationships_only`
    # returned before `_check_config` was reached, so the path `check-links.yml` runs
    # gated no front door, no registry readability, no slug-less registry rows, no
    # declared name fields and no extra schemas. It was harmless only because a SECOND
    # workflow runs the full command — a property of one workflow file, not of this tool.
    #
    # THE CHOICE: run the config check here too, rather than printing that it was skipped.
    #
    #   * The flag narrows WHICH DOCUMENTS are checked, not WHETHER THE CORPUS IS
    #     CONFIGURED. None of the config findings is per-document, and none of them gets
    #     cheaper by looking at fewer files.
    #   * The join gate went this way for exactly this reason (corpus-toolkit#3): "leaving
    #     it out of that path would mean the gate exists in a command no corpus's CI
    #     actually invokes."
    #   * It costs one YAML read that the config load has already done.
    #   * The config check stopped being advisory: #141 made a missing or placeholder
    #     front door a hard ERROR and #129 added the registry findings. Skipping all of it
    #     is a bigger claim now than when this path was written.
    #   * The alternative — print "no config was checked" — leaves a corpus whose CI is
    #     trimmed to the link check with no gate at all, and this repo does not ship a
    #     guard that cannot fire (AGENTS.md).
    #
    # WHAT IT COSTS: this command can now fail for a reason that is not a relationship.
    # That is the point, and the help text says so rather than leaving "only" to imply
    # otherwise.
    ap.add_argument("--check-relationships", action="store_true",
                    help="skip the per-document schema checks: run the relationship-graph "
                         "and join resolution checks plus the corpus-level config checks "
                         "(front door, issuing-body registry, extra schemas) — the "
                         "corpus-level facts are gated on every entry point")
    ap.add_argument("--changed", nargs="?", const="", metavar="REF",
                    help="validate only files changed vs REF (default: merge-base with origin/main)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="worker processes (default: all CPUs)")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    r = Reporter()
    # ONE READ, EVERY QUESTION. The per-file checks want the slug set; the config check
    # wants the entries themselves. Reading the file twice is how two answers about one
    # registry start to disagree (config.RegistryRead carries the same warning).
    registry = _read_registry(config)

    scoped = args.changed is not None
    if scoped:
        paths = changed_content_files(config, args.changed or None)
        if not paths:
            print("No changed content files to validate.")
            if args.check_relationships:
                # A CORPUS-LEVEL FACT DOES NOT DEPEND ON WHICH FILES A PR TOUCHED, and the
                # full command already checks the config on this same no-op run. A gate
                # that fires on one branch of one flag and not the other is the "guard
                # that cannot fire" AGENTS.md files as a defect.
                _check_corpus_config(config, r, registry)
                r.finish("OK: no changed content files; corpus configuration checked.")
                return
    else:
        paths = list(content_files(config))

    if args.check_relationships:
        _run_relationships_only(config, paths, r, registry)
        return

    doc_schema = json.loads(open(args.schema).read()) if args.schema else bundled_schema()
    doc_schema = schema_with_extensions(doc_schema, config)

    docs = {}
    # The fork-pool, the 50-file threshold and the chunk size live in repo.map_documents —
    # they were written out here AND in validate/provenance.py, identically, so tuning
    # either meant finding both (corpus-toolkit#76). The worker-global handoff below is
    # unchanged: _init_worker still populates this module's globals, which check_file reads.
    results = map_documents(paths, check_file, jobs=args.jobs, setup=_init_worker,
                            setup_args=(doc_schema, config, registry.slugs))
    for rel, findings, doc_id in results:
        for level, msg in findings:
            (r.error if level == "error" else r.warn)(rel, msg)
        if doc_id is not None:
            docs[doc_id] = rel

    universe = _resolution_universe(config, docs)
    for rel, level, msg in (_relationship_findings(paths, universe, config)
                            + _join_findings(paths, universe, config)):
        (r.error if level == "error" else r.warn)(rel, msg)

    _check_corpus_config(config, r, registry)

    scope = f"{len(paths)} changed" if scoped else f"{len(paths)}"
    r.finish(f"OK: {scope} content file(s) validated across {', '.join(config.content_dirs)}.")


if __name__ == "__main__":
    main()
