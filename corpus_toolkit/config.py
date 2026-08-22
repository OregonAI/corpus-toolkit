"""Loads `_meta/corpus.yml` — the single source of truth every toolkit module
reads instead of hardcoding corpus-specific paths, directories, or enums."""
from __future__ import annotations

import dataclasses
import functools
import re
from pathlib import Path

import yaml


@dataclasses.dataclass
class ContentRoot:
    path: str
    doc_type: str | None = None          # jurisdiction-wide: one doc_type for the whole dir
    scoped: bool = False                  # issuing-body-scoped: <path>/<slug>/<subdir>
    subdirs: dict[str, str] = dataclasses.field(default_factory=dict)  # subdir name -> doc_type

    def doc_type_for(self, rel_parts: tuple[str, ...]) -> str | None:
        """rel_parts is the path relative to the repo root. Returns the one doc_type this
        location is allowed to hold, or None if rel_parts isn't under this root at all."""
        if not rel_parts or rel_parts[0] != self.path:
            return None
        if not self.scoped:
            return self.doc_type
        # scoped: <path>/<issuing-body-slug>/<subdir>/...
        if len(rel_parts) < 3:
            return None
        return self.subdirs.get(rel_parts[2])


@dataclasses.dataclass
class Sibling:
    """A corpus in the same org that this one cites. Resolution against a
    sibling reads its COMPACT `_meta/corpus-index.json` (see
    corpus_toolkit/index.py) — never its `_meta/graph.json`, which is tens of
    megabytes for a real corpus.

    `index_path` is a local filesystem path (absolute, or relative to this
    corpus's root) and takes precedence over `index_url` — for offline/dev
    work, monorepo checkouts, and tests."""
    id: str
    index_url: str | None = None
    web_base: str = ""
    index_path: Path | None = None


@dataclasses.dataclass
class CorpusConfig:
    root: Path                # repo root (parent of `_meta/`)
    config_path: Path
    id: str
    name: str
    jurisdiction: str
    archetype: str
    # URL of the CORPUS-LEVEL authoritative source — the ORS/OAR landing page, the SoS
    # Archives schedules page, the legislature's bill site. Required by
    # docs/mcp-interface-contract.md response convention 1 and emitted on every MCP
    # response.
    #
    # One URL, and single by design (corpus-toolkit#70): it is the corpus's FRONT DOOR,
    # where a reader starts for this corpus's official text, not a per-answer citation —
    # `get_document` answers per document from that document's own `source_url` and falls
    # back here only when it has none. So a corpus spanning publishers declares its best
    # single entry point rather than a list, and `str | None` is the settled type.
    #
    # `str | None` HERE, AND A HARD ERROR IN THE VALIDATOR (corpus-toolkit#11). The two
    # are not in tension, they are different jobs: `corpus-validate-frontmatter` gates the
    # REPO — a corpus cannot merge without a front door, and cannot merge one whose host
    # is an RFC 2606 reserved name (the template's unfilled placeholder) — while `load()`
    # keeps serving whatever is on disk. A loader that refused would take a running
    # server DOWN on a pin bump, over a config that was legal when it was deployed, and
    # `authoritative_source: null` is a documented response value that convention 1
    # requires every response model to admit. Refuse it in CI, never at runtime.
    authoritative_source: str | None
    schema_version: int
    contract_version: int
    content_roots: list[ContentRoot]
    disclaimer_marker: str
    graph_path: Path
    source_manifest_path: Path
    snapshot_dir: Path
    snapshot_slice_module: str | None
    extraction_module: str | None
    citation_module: str | None
    semantic_search_module: str | None
    # Attr path to a RetrievalBackend factory, e.g. "src.odata_backend:ODataBackend".
    # None = the built-in FileBackend (markdown + FTS). An API-archetype corpus
    # supplies its own; see corpus_toolkit/mcp/backends.py.
    retrieval_module: str | None
    # Attr path to a callable registering CORPUS-SPECIFIC MCP tools, e.g.
    # "src.budget_tools:register". Called as register(mcp, framework) after every
    # built-in tool, so a corpus can add tools the shared contract cannot know about
    # (dataset queries, join lookups) without forking the server. None = built-ins only.
    #
    # The seven built-in tools were previously the complete, closed set, which forced
    # corpora to smuggle extra behaviour through get_document enrichment — workable for
    # attaching data to a document, useless for a tool that takes a dataset key rather
    # than a document id.
    tools_module: str | None
    # doc_type -> ordered list of '## ' headings whose text feeds the searchable body
    # column. A doc_type ABSENT from this map keeps the historical behaviour exactly
    # ('## Full text', else '## Key provisions'), so an existing corpus that does not
    # set this key indexes byte-identically to before. Listed headings are CONCATENATED,
    # not first-match: a mirrored record often has several sections worth searching.
    index_headings: dict
    # Byte-regexes stripped from HTML/XML before hashing, COMPILED at load — see
    # _validated_volatile_patterns and corpus_toolkit.repo.normalize_volatile. Empty for a
    # corpus that declares none, which hashes byte-identically to every release before
    # v1.26.0. ADDING OR CHANGING A PATTERN CHANGES THE HASH of every page it matches, so
    # the recorded baselines for those pages go stale in one wave; re-seed them with
    # `corpus-detect-changes --record-baseline=refresh` in the same PR that adds the
    # pattern, or the next scheduled run reports the whole group as drift
    # (corpus-toolkit#66, #68).
    volatile_patterns: list
    issuing_body_registry: Path | None
    issuing_body_registry_key: str
    issuing_body_profiles: Path | None
    # Registry fields that carry a NAME, in the order `issuing_body_profile`'s free-text
    # fallback tries them. ("name",) for a corpus that declares nothing, which then matches
    # exactly what it matched before.
    #
    # WHY THIS IS CONFIG AND NOT A SECOND HARDCODED KEY. `name` is the name a reader knows
    # only for as long as a corpus keeps it that way, and a corpus may legitimately promote
    # it to something else — `executive-regulatory-frameworks` is doing exactly that under
    # its ADR 0003, which is what left 189 of 189 of its bodies unfindable by the name
    # printed on their OAR citations. The replacement field is ERF's own name for it and
    # this toolkit serves many corpora, so the corpus declares its own list (AGENTS.md: all
    # corpus specifics come from config). The measurement and the migration are told once,
    # in docs/mcp-interface-contract.md under `issuing_body_profile` (corpus-toolkit#128).
    #
    # A LIST-VALUED FIELD NEEDS NO SECOND KEY. An entry's value may be a string or a list of
    # strings, and a list is matched element-wise, so ERF's curated `aliases` is declared in
    # the same list as `oar_name`. A separate `issuing_body_alias_fields` would double the
    # public surface to encode a shape the value already states.
    issuing_body_name_fields: tuple[str, ...]
    # The frontmatter key carrying a document's REGISTRY SLUG, where a corpus has one.
    # None = this corpus attributes documents by directory only, and the path-derived
    # scope slug is the whole answer. See `registry_slug_for` for which wins and why.
    issuing_body_slug_field: str | None
    # Values of `issuing_body_slug_field` that mean "attributed to NO body, deliberately" —
    # ERF's `agency: statewide`, 37,991 documents that carry no agency by design. Empty
    # frozenset for a corpus that declares none, which then behaves exactly as before.
    #
    # A sentinel is a positive assertion, not a gap. Without this the toolkit could not tell
    # a deliberate `statewide` from a misspelling, so a corpus that had done everything right
    # was told its per-agency counts were lower bounds — permanently, for a reason that was
    # 99.997% legitimate (corpus-toolkit#94). They get their OWN coverage bucket rather than
    # being folded into `in_registry`: "counted for a registry body" and "deliberately
    # counted for no body" are different answers, and CONTEXT.md forbids collapsing two
    # distinct answers into one.
    issuing_body_slug_sentinels: frozenset[str]
    extra_schema_checks: list[dict]
    mcp_server_name: str
    mcp_transports: list[str]
    # Frontmatter keys a corpus promises to serve on get_document, beyond the fixed set
    # every corpus shares. Opt-in and explicit: the response shape is an interface
    # contract, so a corpus declares what it adds rather than leaking whatever happens to
    # be in its frontmatter.
    mcp_extra_document_fields: list[str]
    # Relations `authority_chain` should walk IN ADDITION to implements/implemented_by,
    # each returned under its own response key. Shape: {"up"|"down": {name: [rel, ...]}}.
    #
    # WHY THIS IS CONFIG AND NOT A SECOND HARDCODED KEY. There is no single citation-style
    # relation across the org: oregon-counties, oregon-budget, oregon-legislature and
    # oregon-audits record external citations as `references_external`, while
    # oregon-records-retention puts its external OAR citations under `related`. Hardcoding
    # one fixes four corpora and leaves another empty.
    #
    # WHY THEY GET THEIR OWN KEY. `references_external` is not `implements`. A county
    # ordinance citing ORS 215.203 is usually implementing it, and usually is not a fact —
    # which is why oregon-counties records citations rather than asserting implementation.
    # Returning those under a key named `up_implements` would assert the relationship the
    # graph deliberately declined to claim.
    #
    # Empty by default, so a corpus that declares nothing gets byte-identical responses.
    mcp_authority_relations: dict[str, dict[str, list[str]]]
    # Corpus-declared doc_types beyond the shared schema's enum: {name: verbatim_required}.
    # Before this existed (corpus-toolkit#40), every new corpus vertical cost a toolkit
    # release plus an org-wide pin bump — three of releases v1.9–v1.13 were exactly one
    # doc_type each. Link-graph participation stays a separate, explicit opt-in: deriving
    # edges from a new type automatically is how 851 bogus `implements` edges appeared
    # when `schedule` was added to LINK_DOC_TYPES (PLAN.md Phase 9).
    extra_doc_types: dict[str, bool]
    reverify_days: int
    coverage_fail_threshold: float
    coverage_warn_threshold: float
    raw: dict
    siblings: list[Sibling] = dataclasses.field(default_factory=list)

    def sibling(self, sibling_id: str) -> Sibling | None:
        return next((s for s in self.siblings if s.id == sibling_id), None)

    @property
    def content_dirs(self) -> list[str]:
        return [cr.path for cr in self.content_roots]

    def doc_type_for(self, rel_parts: tuple[str, ...]) -> str | None:
        for cr in self.content_roots:
            dt = cr.doc_type_for(rel_parts)
            if dt is not None:
                return dt
        return None

    def scope_slug_for(self, rel_parts: tuple[str, ...]) -> str | None:
        """The issuing-body slug this path is scoped under (e.g. 'agencies/<slug>/...'),
        or None for a jurisdiction-wide root or a path outside any content root. This
        is PATH-derived, deliberately independent of whatever a document's own
        `issuing_body` frontmatter field says (that's a free-text descriptor — e.g.
        Oregon's `issuing_body: "DAS Enterprise Information Strategy and Policy
        Division"` is a sub-unit name, not the registry slug `agency:
        department-of-administrative-services` the directory is scoped under).
        Used to join documents to the issuing-body registry correctly, whatever a
        corpus's frontmatter field naming happens to be."""
        for cr in self.content_roots:
            if cr.scoped and rel_parts and rel_parts[0] == cr.path and len(rel_parts) > 1:
                return rel_parts[1]
        return None

    @functools.cached_property
    def issuing_body_slugs(self) -> frozenset[str] | None:
        """Every slug in the issuing-body registry, or None when there is no registry to
        read — in which case "does this value name a body?" is a question with no answer
        here, and callers must report it as unknown rather than as no.

        Cached because the index build asks it once per document. Read through this rather
        than re-parsing the registry: `issuing_body_profile` needs the whole entry and
        parses it separately, and two parsers disagreeing about which slugs exist is the
        kind of drift that shows up as a count instead of an error.
        """
        if not self.issuing_body_registry or not self.issuing_body_registry.is_file():
            return None
        return _parse_registry_slugs(self.issuing_body_registry,
                                     self.issuing_body_registry_key)

    def registry_slug_for(self, frontmatter: dict, rel_parts: tuple[str, ...]) -> str | None:
        """The issuing-body slug this document is attributed to, or None if it carries no
        attribution at all. The returned value is NOT guaranteed to name a registry entry;
        `holdings_for` classifies it, so a value the registry does not contain is counted
        and reported rather than dropped.

        TWO MECHANISMS, AND THE VALIDATED ONE IS NEVER OVERRIDDEN BY AN UNVALIDATED ONE.

        `scope_slug_for` reads the slug off the PATH and covers only what lives under a
        `scoped: true` content root. That is correct where a corpus is organised by issuing
        body and unreachable everywhere else — a rule belongs to its chapter, not to an
        agency folder — so on `executive-regulatory-frameworks` it answers for 960 of
        75,905 documents, 1.3% (measured 2026-08-18). Counts built on it alone under-report
        by 97% for the corpus's largest agencies (corpus-toolkit#71). It is also CHECKED:
        `validate/frontmatter.py` fails CI when a scoped path's slug is not in the registry.

        `plugins.issuing_body_slug_field` names the frontmatter key carrying the registry
        slug for EVERY document, whatever directory it sits in (`agency:` on ERF, present
        on 100%). Nothing validates its values (corpus-toolkit#94), so it wins only where
        the value it carries actually names a registry entry. Order:

          1. the declared field, when its value is a registry slug — the corpus's explicit
             assertion, and the only mechanism that reaches a chapter-organised root;
          2. the path-derived slug, which CI has already checked;
          3. the declared field's value even though the registry does not contain it — kept
             so it lands in the "names no registry entry" bucket with a number beside it,
             instead of disappearing;
          4. None.

        RULE 2 BEFORE RULE 3 IS THE WHOLE POINT. Letting an unchecked value override a
        checked one means one typo in `agency:` silently REMOVES a document from a count
        that was previously right — a count going down, reported by a response that still
        calls itself complete. An earlier draft of this (corpus-toolkit#71) did exactly
        that. Where no registry is loadable, rule 1 cannot be evaluated and the declared
        field wins outright; the coverage buckets are then reported as unknown.

        NOT the free-text `issuing_body` frontmatter field, which is a sub-unit name
        ("DAS Enterprise Information Strategy and Policy Division") and not a registry
        slug — the distinction `scope_slug_for`'s docstring exists to preserve. A corpus
        declares which key carries slugs precisely so the toolkit never has to guess.
        """
        declared = ""
        if self.issuing_body_slug_field:
            declared = str(frontmatter.get(self.issuing_body_slug_field) or "").strip()
        # RULE 0: A DECLARED SENTINEL WINS OUTRIGHT AND DOES NOT FALL THROUGH.
        #
        # It sits above the registry check because it is not a slug at all — it is the
        # corpus positively asserting "this document is attributed to NO body"
        # (corpus-toolkit#94). The rules below exist to stop an UNCHECKED value displacing
        # a CHECKED one, which is right for a typo and wrong here: falling through would
        # re-attribute a sentinel document by directory, contradicting the corpus about its
        # own document. A sentinel is declared in config and validated at load, so it is not
        # the unchecked input those rules guard against.
        #
        # This is what changes indexed values for an existing corpus that adopts sentinels,
        # and why SCHEMA_VERSION moved to 4: a sentinel document under a scoped root used to
        # resolve to its directory's slug and now resolves to the sentinel.
        if declared and declared in self.issuing_body_slug_sentinels:
            return declared
        known = self.issuing_body_slugs
        if declared and (known is None or declared in known):
            return declared
        return self.scope_slug_for(rel_parts) or declared or None

    def expected_root_for(self, doc_type: str) -> ContentRoot | None:
        """The one content root a given doc_type is allowed to live under (used to report
        'this belongs under X/, not Y/' when a document is misplaced)."""
        for cr in self.content_roots:
            if not cr.scoped and cr.doc_type == doc_type:
                return cr
            if cr.scoped and doc_type in cr.subdirs.values():
                return cr
        return None


def _resolve(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    return (root / value).resolve()


def load_source_manifest_groups(config: "CorpusConfig") -> list[dict]:
    """Source-manifest groups: `source_manifest_path` may be a single flat
    file (one implicit group) or a directory of per-group `*.yml` files
    (each with its own `sources:` list, and optionally `group`/`last_checked`/
    `recheck`/etc. — carried through as-is, not interpreted by the toolkit).
    Large corpora with many distinct source groups (different recheck
    cadences, different upstream owners) use directory mode; a small corpus
    just ships one `_meta/source-manifest.yml`."""
    return [g for _, g in load_source_manifest_group_files(config)]


def load_source_manifest_group_files(config: "CorpusConfig") -> list[tuple[Path, dict]]:
    """(file path, parsed group) for every source-manifest group, in load order.

    Same rules as `load_source_manifest_groups`, which is a thin wrapper on this. The path
    is exposed because a writer needs it: `corpus-detect-changes --record-baseline` edits
    the group file a source came from, and re-deriving "which file was that?" in the caller
    is how the file/directory rules end up implemented twice and diverging
    (corpus-toolkit#68)."""
    path = config.source_manifest_path
    if path is None:
        return []
    if path.is_dir():
        groups = []
        for p in sorted(path.glob("*.yml")):
            g = yaml.safe_load(p.read_text()) or {}
            g.setdefault("group", p.stem)
            groups.append((p, g))
        if not groups:
            raise MissingSourceManifest(
                f"source_manifest_path {path} is a directory containing no *.yml group "
                f"files. Refusing to report zero sources as success. (A directory of "
                f"*.yaml files is the usual cause — the loader only reads *.yml.)")
        return groups
    if path.is_file():
        return [(path, yaml.safe_load(path.read_text()) or {})]
    # A configured path that is neither file nor directory returned [] — so
    # corpus-detect-changes reported "0 changed, 0 fetch failure(s)" and exited 0,
    # forever, while checking nothing. A renamed file, a typo'd path, or a directory
    # holding *.yaml instead of *.yml all silenced upstream-change detection for a
    # corpus declaring 1,874 sources. Absence of a CONFIGURED path is a fault.
    raise MissingSourceManifest(
        f"source_manifest_path {path} does not exist. Refusing to report zero sources "
        f"as success — upstream-change detection would be permanently green while "
        f"checking nothing. (If this corpus has no manifest, unset the key.)")


class MissingSourceManifest(RuntimeError):
    """A configured source_manifest_path that is not on disk."""


def _validated_watch(raw, sid):
    """Check a source's `watch` list (corpus-toolkit#72).

    NOT called from `load()`, unlike `_validated_volatile_patterns` — a manifest is not the
    corpus config, and validating every group on load made one group's typo abort every
    other group's cron. `changes.main()` calls `validate_watch_declarations` on the sources
    this run will fetch, after the `--group` filter and before the first request. Callers
    that reach a manifest another way (`status.py`, `--check-robots`, a corpus's own script)
    do NOT validate — they also never read `watch`, and the hash validates again regardless.

    The grammar itself lives in `corpus_toolkit.repo.validate_watch`, which the hash also
    calls — one parser, because while these were two implementations they disagreed and a
    path the door accepted was reported mid-crawl as an upstream schema change. This adds
    the source id, and the one rule the hash cannot express: at the manifest, a `watch:` key
    with no value is an authoring accident rather than a caller passing None.

    Checked before the crawl rather than only at the hash because the answer does not depend
    on the response: after a 3,447-source crawl is the wrong moment to learn a key was
    mistyped.
    """
    from corpus_toolkit.repo import validate_watch

    if raw is None:
        # A PRESENT KEY WITH NO VALUE, since this is only called when `watch` is in the
        # source. `watch:` with nothing under it -- a mis-indented list, or one deleted a
        # line at a time -- parses to None, and the source silently reverted to hashing the
        # whole document: the exact `viewCount` false-positive stream #72 removes, emitted
        # from a manifest that visibly declares `watch`, with nothing said anywhere. One
        # character away, `watch: []` is a hard error; the same accident must not get
        # opposite treatment, and the silent branch is the wrong one to keep.
        raise ValueError(
            f"source {sid!r}: `watch` is declared with no value. A `watch:` key with "
            f"nothing under it hashes the whole document, so the source would go on "
            f"reporting the vendor counters `watch` exists to ignore. Remove the key to "
            f"hash raw bytes deliberately, or give it a list of paths.")
    return validate_watch(raw, where=f"source {sid!r}")


def validate_watch_declarations(sources):
    """Validate `watch` on every source in `sources`, in place. Returns the same list.

    SEPARATE FROM `iter_manifest_sources` and applied AFTER the caller's `--group` filter.
    Validating on yield made one group's typo abort every OTHER group's cron with an
    uncaught traceback -- `--group` is the per-cadence knob, and `--check-robots`, which is
    documented as reporting and never blocking, blocked. The fail-before-the-first-request
    property is what matters, and filtering first keeps it: every source this run will
    actually fetch is checked before any of them is.
    """
    for s in sources:
        if isinstance(s, dict) and "watch" in s:
            s["watch"] = _validated_watch(s["watch"], s.get("id", "<no id>"))
    return sources


def iter_manifest_sources(config: "CorpusConfig"):
    """Yield every source dict ({id, url, sha256, ...}) across all manifest
    groups, in group order. Each dict is annotated with `_group` (the group
    name, or "manifest" for the single-file form) so callers can filter —
    the per-cadence cron's knob in sources/changes.py."""
    for g in load_source_manifest_groups(config):
        gname = g.get("group") or "manifest"
        for s in (g.get("sources", []) or []):
            if isinstance(s, dict):
                s = {**s, "_group": gname}
            yield s


# The five keys `relationships` may contain — document.frontmatter.v1.schema.json pins this
# with additionalProperties: false, so a typo here can never match a real edge.
_RELATION_KEYS = ("implements", "implemented_by", "references_external", "related",
                  "supersedes")
_ALWAYS_WALKED = {"up": "implements", "down": "implemented_by"}

# The three archetypes docs/mcp-interface-contract.md defines. Validated LOUDLY because the
# string was documentation, not a contract: `archetpye: hybrd` silently yielded a document
# corpus, and the value flows verbatim into every response envelope and the server's own
# instructions text (corpus-toolkit#38).
_ARCHETYPES = ("document", "api", "hybrid")

# What PyYAML's safe_load — the parser this loader actually uses — resolves to booleans,
# MEASURED rather than recalled from the YAML 1.1 spec. Bare `y`/`n` are NOT among them
# (they come back as the strings "y"/"n"), and the match is case-insensitive, so `Off`,
# `NO` and `True` are all booleans too.
#
# Every spelling for a given value is listed because the parsed value cannot tell us which
# one the author typed. The advice must therefore be "quote whichever you wrote" — naming a
# single spelling would tell someone who wrote `id: off` to write `"no"`, which silently
# RENAMES their corpus: `corpus.id` is the envelope's `corpus` field, the MCP server name,
# and how siblings cross-reference this corpus. The fix is quoting, never a new value.
_YAML_BOOLEANS = {True: ("yes", "on", "true"), False: ("no", "off", "false")}


def _validated_corpus_string(raw, field: str, *, default=None):
    """Parse and CHECK one `corpus.*` string field, loudly.

    Same policy as _validated_archetype: a bad value must fail at LOAD. These four fields
    were the ones that had no check at all, and they failed in two different directions.

    `authoritative_source` was `(raw or "").strip()`, so a non-string raised AttributeError
    from inside a string method — naming neither the file nor the key, and pre-empting the
    URL validator downstream whose whole purpose is to say something useful here.

    `id`, `name` and `jurisdiction` were passed through untouched, so a non-string was
    accepted silently. Since corpus-toolkit#103 that is the more serious half:
    `ResponseEnvelope` types `corpus` as `str` and `config.id` fills that slot on all six
    object-shaped tools, so `id: 90210` or an unquoted `id: no` turns every tool call into a
    ValidationError at runtime — on a config this function had called good.
    """
    value = raw if raw is not None else default
    if value is None or isinstance(value, str):
        return value
    hint = ""
    for parsed, spellings in _YAML_BOOLEANS.items():
        if value is parsed:
            hint = (f" YAML reads {', '.join(spellings)} (any capitalisation) as boolean "
                    f"{str(parsed).lower()} — QUOTE WHICHEVER YOU WROTE, keeping the same "
                    f"word.")
            break
    raise ValueError(f"corpus.{field}: {value!r} is a {type(value).__name__}, "
                     f"not a string.{hint}")


# Distinguishes "key absent" from "key present with a null value" — see
# _validated_slug_sentinels, and the `corpus:` block check for the same distinction.
_ABSENT = object()


def name_values(entry, field: str) -> list[str]:
    """The strings in registry `entry`'s `field` that a name query can match.

    ONE DEFINITION OF "A CELL THAT CAN CARRY A NAME", because two callers assert about it
    and they must agree: `framework._name_match` decides whether a query hits a body, and
    `validate/frontmatter._check_config` reports a declared name field that reaches no
    matchable cell in the whole registry. A validator that checked only for the KEY would
    pass a registry whose every `oar_name` is null while every query against it still
    matched nothing — the check reporting a different condition than the one that bites.

    A value may be a STRING or a LIST of strings (ERF's curated `aliases`). Anything else
    in a registry cell — a number, a null, a mapping — is skipped rather than coerced: a
    registry is hand-maintained, and `str(None)` matching "none" is a match nobody wrote.
    """
    if not isinstance(entry, dict):
        return []
    value = entry.get(field)
    return [v for v in (value if isinstance(value, (list, tuple)) else [value])
            if isinstance(v, str)]


def _registry_slugs_at_load(registry_path, key: str):
    """The registry's slugs during `load()`, or None when there is no registry to read.

    `CorpusConfig.issuing_body_slugs` answers the same question but is a cached_property on
    the config being constructed, so it is unavailable here. Reads through the same key and
    the same shape; the sentinel/registry clash check is the only caller, and a registry
    that cannot be read yields None, which that check treats as "unknown" and skips rather
    than as "no slugs" — a missing registry file must not turn every sentinel into a
    spurious clash-free pass OR a spurious error.
    """
    if not registry_path or not Path(registry_path).is_file():
        return None
    try:
        return _parse_registry_slugs(Path(registry_path), key)
    except Exception:                                            # noqa: BLE001
        return None


def _parse_registry_slugs(path: Path, key: str) -> frozenset[str]:
    """THE one registry parser. `CorpusConfig.issuing_body_slugs` and the load-time clash
    check both go through it.

    Its docstring warns that "two parsers disagreeing about which slugs exist is the kind of
    drift that shows up as a count instead of an error", and the load-time check was briefly
    a second one that differed in two ways — admitting `slug: null` into the set, and
    treating a null entry list differently. Sharing the function is the only way that
    warning stays true.
    """
    data = yaml.safe_load(path.read_text()) or {}
    return frozenset(e["slug"] for e in (data.get(key) or [])
                     if isinstance(e, dict) and e.get("slug"))


def _validated_slug_sentinels(raw, *, field: str | None, registry_slugs) -> frozenset[str]:
    """Parse and CHECK `plugins.issuing_body_slug_sentinels`, loudly.

    Every failure here is silent if unchecked, and each produces a corpus that misreports
    its own coverage while looking healthy — which is the defect this feature exists to fix,
    so shipping a way to *silence* the warning rather than answer it would be worse than
    shipping nothing (corpus-toolkit#94).

      * declared without `issuing_body_slug_field`: there is no field for them to apply to,
        so the declaration cannot mean anything. A corpus author who wrote it believes those
        documents are attributed, and would read the resulting coverage as a measurement
        rather than as their declaration being dropped.
      * a bare string instead of a list: `sentinels: statewide` would otherwise iterate as
        the characters of the word.
      * an EMPTY list: declares nothing while looking like it declares something.
      * a value the REGISTRY also contains: that value would mean both "this body" and "no
        body" at once, and the two mechanisms would disagree about the same document
        silently — exactly the class of contradiction `registry_slug_for`'s precedence
        exists to prevent.
    """
    if raw is _ABSENT:
        return frozenset()
    # `issuing_body_slug_sentinels:` with nothing under it is PRESENT-BUT-EMPTY, not absent —
    # the same distinction the `corpus:` block check draws. Returning early on None let a
    # corpus author who commented out their only entry hit exactly the failure the
    # empty-list branch below calls out, silently: the key looks declared, declares nothing,
    # and its documents fall back into `no_registry_entry`.
    if raw is None:
        raise ValueError(
            "plugins.issuing_body_slug_sentinels is declared with no value, which declares "
            "nothing. Remove the key, or list the values that mean 'attributed to no body'.")
    if not field:
        raise ValueError(
            "plugins.issuing_body_slug_sentinels is declared but "
            "plugins.issuing_body_slug_field is not, so there is no frontmatter key for "
            "the sentinels to apply to. Declare the field, or remove the sentinels.")
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"plugins.issuing_body_slug_sentinels must be a list of strings, got "
            f"{type(raw).__name__}: {raw!r}")
    if not raw:
        raise ValueError(
            "plugins.issuing_body_slug_sentinels is an empty list, which declares nothing. "
            "Remove the key, or list the values that mean 'attributed to no body'.")
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"plugins.issuing_body_slug_sentinels: every entry must be a non-empty "
                f"string, got {entry!r}")
    sentinels = frozenset(e.strip() for e in raw)
    if registry_slugs is not None:
        clash = sorted(sentinels & registry_slugs)
        if clash:
            raise ValueError(
                f"plugins.issuing_body_slug_sentinels: {', '.join(clash)} also name(s) an "
                f"entry in the issuing-body registry, so the value would mean both 'this "
                f"body' and 'no body'. A sentinel must be a value the registry does not "
                f"contain.")
    return sentinels


def _validated_name_fields(raw, *, registry) -> tuple[str, ...]:
    """Parse and CHECK `plugins.issuing_body_name_fields`, loudly.

    ABSENT IS ("name",) AND THAT DEFAULT IS THE COMPATIBILITY PROMISE: a corpus that
    declares nothing matches exactly the one field it matched before, so a toolkit upgrade
    never widens a corpus's matcher on its behalf. Widening is the corpus's decision.

    The mistakes below are all SILENT without this check, and all fail the same way: a
    field name that reaches no registry cell matches nothing, and "matches nothing" is
    indistinguishable from a body that genuinely is not there — the exact symptom
    corpus-toolkit#128 is about, arriving from the fix for it.

      * PRESENT WITH NO VALUE: declares nothing while looking like it declares something —
        the state a corpus author reaches by commenting out their only entry.
      * declared without `plugins.issuing_body_registry`: the fields name columns OF THAT
        REGISTRY, so there is nothing for them to name and the fallback they widen never
        runs (`issuing_body_profile` errors out before reaching it).
      * a bare string: `issuing_body_name_fields: oar_name` would iterate as the characters
        of the word, so the corpus would match on fields named "o", "a", "r"...
      * an EMPTY list: read as "no name fields" it would make every free-text query
        unmatchable.
      * a non-string or blank entry: names no column, and quietly drops out of the list.

    NOT CHECKED HERE, DELIBERATELY: whether a declared field appears in any registry entry.
    A corpus mid-migration legitimately declares the field its registry is about to grow (ERF
    declared `oar_name` while ADR 0003 was still copying titles into it), so failing the load
    there would refuse to start a corpus whose config is correct and merely early.

    It IS checked, and reported rather than fatal, by `corpus-validate-frontmatter`:
    `validate/frontmatter._check_name_fields` warns when a declared field reaches no name in
    the registry, next to the missing-`authoritative_source` warning that is the other
    corpus-level config finding (corpus-toolkit#129). So a typo — `oar_nmae` — is no longer
    silent, without this loader refusing a config that is correct and merely early.
    """
    if raw is _ABSENT:
        return ("name",)
    # ORDER MIRRORS `_validated_slug_sentinels` DELIBERATELY: no-value before the
    # companion-key check, so the same mistake in either declaration is reported the same
    # way. Two sibling validators reporting a bare `key:` differently is the kind of
    # inconsistency a corpus author reads as two different problems.
    if raw is None:
        raise ValueError(
            "plugins.issuing_body_name_fields is declared with no value, which declares "
            "nothing. Remove the key to match on `name` alone, or list the registry fields "
            "that carry a name.")
    if not registry:
        raise ValueError(
            "plugins.issuing_body_name_fields is declared but plugins.issuing_body_registry "
            "is not, so the fields name columns of a registry this corpus does not have. "
            "Declare the registry, or remove the name fields.")
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"plugins.issuing_body_name_fields must be a list of registry field names, got "
            f"{type(raw).__name__}: {raw!r}")
    if not raw:
        raise ValueError(
            "plugins.issuing_body_name_fields is an empty list, which declares nothing and "
            "would leave every free-text query unmatchable. Remove the key to match on "
            "`name` alone, or list the registry fields that carry a name.")
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"plugins.issuing_body_name_fields: every entry must be a non-empty "
                f"registry field name, got {entry!r}")
    # Order is preserved and is load-bearing: it decides which field a candidate reports as
    # the one that matched. Duplicates are dropped — a field scanned twice cannot match
    # anything the first scan missed.
    return tuple(dict.fromkeys(e.strip() for e in raw))


def _validated_archetype(raw) -> str:
    """Parse and CHECK `corpus.archetype`, loudly.

    Same policy as _validated_index_headings: a bad value must fail at LOAD, not surface
    as a server that starts clean while quietly missing its extension tools — which is how
    a declared hybrid served six tools for a week (oregon-legislature#11).
    """
    value = raw if raw is not None else "document"
    if not isinstance(value, str) or value not in _ARCHETYPES:
        raise ValueError(f"corpus.archetype: {value!r} is not an archetype. "
                         f"Legal values: {', '.join(_ARCHETYPES)}")
    return value


def _validated_extra_doc_types(raw) -> dict[str, bool]:
    """Parse and CHECK the top-level `schema:` section, loudly.

        schema:
          doc_types:
            - name: transmittal
              verbatim: false

    `verbatim` is REQUIRED per type: whether a paraphrase of this material is a changed
    document is the load-bearing property (see validate/provenance.py's VERBATIM_REQUIRED
    rationale), and defaulting it either way would decide that silently."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - {"doc_types"}:
        raise ValueError("schema: must be a mapping with the single key doc_types")
    out = {}
    for i, entry in enumerate(raw.get("doc_types") or []):
        if not isinstance(entry, dict) or "name" not in entry or "verbatim" not in entry:
            raise ValueError(f"schema.doc_types[{i}]: each entry needs name and verbatim")
        name, verbatim = entry["name"], entry["verbatim"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"schema.doc_types[{i}].name: {name!r} must be lower_snake")
        if not isinstance(verbatim, bool):
            raise ValueError(f"schema.doc_types[{i}].verbatim: {verbatim!r} must be a bool")
        out[name] = verbatim
    return out


def _validated_authority_relations(raw) -> dict[str, dict[str, list[str]]]:
    """Parse and CHECK `mcp.authority_relations`, loudly.

    Every failure mode here is silent if unchecked, and each produces a tool that answers
    confidently with nothing — which is the defect this whole feature exists to fix:

      * a direction other than up/down is simply never walked
      * a relation key that is not one of the five legal ones matches no edge, ever
      * a name colliding with `implements`/`implemented_by` would overwrite the
        unconditional result and silently redefine what the corpus claims
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("mcp.authority_relations must be a mapping of "
                         "{'up'|'down': {name: [relation, ...]}}")
    out: dict[str, dict[str, list[str]]] = {}
    for direction, group in raw.items():
        if direction not in ("up", "down"):
            raise ValueError(f"mcp.authority_relations: unknown direction {direction!r} "
                             f"(expected 'up' or 'down')")
        if not group:
            continue
        if not isinstance(group, dict):
            raise ValueError(f"mcp.authority_relations.{direction} must be a mapping of "
                             f"{{name: [relation, ...]}}")
        named: dict[str, list[str]] = {}
        for name, rels in group.items():
            if name in _ALWAYS_WALKED.values():
                raise ValueError(
                    f"mcp.authority_relations.{direction}: {name!r} is walked "
                    f"unconditionally and cannot be redefined — pick another name so the "
                    f"asserted relation and the configured one stay distinguishable")
            rels = [rels] if isinstance(rels, str) else list(rels or [])
            bad = [r for r in rels if r not in _RELATION_KEYS]
            if bad:
                raise ValueError(
                    f"mcp.authority_relations.{direction}.{name}: "
                    f"{', '.join(map(repr, bad))} is not a relationship key. "
                    f"Legal keys: {', '.join(_RELATION_KEYS)}")
            if not rels:
                raise ValueError(f"mcp.authority_relations.{direction}.{name} is empty — "
                                 f"remove it, or name the relations it should walk")
            named[name] = rels
        if named:
            out[direction] = named
    return out


def _validated_index_headings(raw) -> dict:
    """Validate `index_headings` shape LOUDLY.

    Getting this wrong empties a doc_type's searchable body with no error anywhere:
    _searchable_body() finds no matching section, writes "", and health() still reports
    the corpus as reachable because it counts ROWS, not indexed text. The worst case is
    a YAML scalar --

        index_headings:
          statute: "Full text"      # a string, not a list

    -- because a non-empty string is truthy AND iterable, so the loop walks CHARACTERS,
    every one fails to match a heading, and the doc_type silently vanishes from keyword
    search. That is the single most likely authoring mistake here, so it must fail at
    load rather than at query time."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"index_headings must be a mapping of doc_type -> list of "
                         f"heading names, got {type(raw).__name__}")
    for doc_type, headings in raw.items():
        if isinstance(headings, str):
            raise ValueError(
                f"index_headings[{doc_type!r}] is a string, not a list. Write "
                f"[{headings!r}] — a bare string is iterated CHARACTER BY CHARACTER, "
                f"which silently empties this doc_type's search index.")
        if not isinstance(headings, (list, tuple)) or not headings:
            raise ValueError(f"index_headings[{doc_type!r}] must be a non-empty list of "
                             f"heading names, got {headings!r}")
        bad = [h for h in headings if not isinstance(h, str) or not h.strip()]
        if bad:
            raise ValueError(f"index_headings[{doc_type!r}] contains non-string or empty "
                             f"heading(s): {bad!r}")
    return dict(raw)


def _validated_volatile_patterns(raw) -> list:
    """Parse, CHECK and COMPILE `volatile_patterns`, loudly (corpus-toolkit#66).

    Per-fetch or per-release tokens embedded in a shared template — session ids, CDN
    tokens, an application footer version — are hashed as content unless they are stripped
    first. ERF's `oar` group is the measured case: one OARD footer bump (`v2.1.7` ->
    `v2.1.8`) turned all 484 sources into drift with zero rule text changed.

    Every failure mode here is a SILENT NO-OP if unchecked, which is indistinguishable from
    the empty-list bug this key exists to fix, so each one fails at LOAD instead:

      * a bare string is truthy AND iterable, so it would be walked CHARACTER BY CHARACTER
        — each character compiled as its own regex, '(' and '[' raising mid-crawl and the
        rest matching arbitrary bytes in every source. Same trap as _validated_index_headings.
      * an invalid regex would otherwise raise inside the fetch loop, per source, after the
        crawl has already been running for however long it takes to reach the first HTML
        source — or, worse, be caught and swallowed as a fetch failure.
      * an EMPTY pattern matches at every position and substitutes nothing: a pattern that
        is configured and does nothing, which is exactly the reported bug.

    Compiled here, once, rather than per source: this runs against 3,447-source manifests.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raise ValueError(
            f"volatile_patterns is a string, not a list. Write [{raw!r}] — a bare string is "
            f"iterated CHARACTER BY CHARACTER, so each character becomes its own regex.")
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"volatile_patterns must be a list of regex strings, got "
                         f"{type(raw).__name__}")
    out = []
    for i, pattern in enumerate(raw):
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"volatile_patterns[{i}]: {pattern!r} must be a non-empty "
                             f"string. An empty pattern matches everywhere and strips "
                             f"nothing.")
        try:
            # Bytes, not text: normalization runs on the RAW response, before any text
            # extraction, because that is where a session id or a footer version lives.
            out.append(re.compile(pattern.encode("utf-8")))
        except re.error as e:
            raise ValueError(f"volatile_patterns[{i}]: {pattern!r} is not a valid regex "
                             f"({e}). Refusing to load — a pattern that cannot compile "
                             f"would otherwise be a pattern that never strips anything.")
    return out


def load(config_path: str | Path) -> CorpusConfig:
    config_path = Path(config_path).resolve()
    root = config_path.parent.parent
    raw = yaml.safe_load(config_path.read_text()) or {}

    # The SAME defect class as the field checks below, one level up (corpus-toolkit#89,
    # found in review). An empty or mis-indented `corpus:` block made every
    # `corpus.get(...)` below raise `AttributeError: 'NoneType' object has no attribute
    # 'get'` — the exact shape this issue was filed about, and a far more common authoring
    # mistake than a non-string `id`. Checking the fields while leaving the block they live
    # in unchecked would have left the reported symptom reachable by an easier route.
    #
    # PRESENT-BUT-WRONG is an error; ABSENT keeps its existing `{}` default. Those are
    # different states and only the first is evidence of a mistake. Note that `corpus:` with
    # nothing under it is present-but-wrong, not absent: coercing it to `{}` would load a
    # corpus with an empty id, which serves and answers under no name — a silent version of
    # the failure this check exists to make loud.
    if "corpus" in raw and not isinstance(raw["corpus"], dict):
        raise ValueError(
            f"corpus: must be a mapping of fields (id, name, jurisdiction, ...), got a "
            f"{type(raw['corpus']).__name__}: {raw['corpus']!r}")
    corpus = raw.get("corpus", {})
    # Validated ONCE and reused, rather than re-read from the raw dict at each use site.
    # `mcp_server_name` used to fall back to `corpus.get("id", "corpus")` directly, so it
    # could diverge from `config.id` — for `id: ~` the dataclass field declared `str` would
    # have held None while `config.id` held "". One read, one check, one value.
    corpus_id = _validated_corpus_string(corpus.get("id"), "id", default="")
    content_roots = [
        ContentRoot(
            path=cr["path"],
            doc_type=cr.get("doc_type"),
            scoped=bool(cr.get("scoped", False)),
            subdirs=cr.get("subdirs", {}) or {},
        )
        for cr in raw.get("content_roots", []) or []
    ]
    plugins = raw.get("plugins", {}) or {}
    # Routed through the same validated-string path as the `corpus.*` fields (the preceding
    # commit in this stack): `issuing_body_slug_field: [agency]` used to raise a bare
    # `AttributeError: 'list' object has no attribute 'strip'` naming no config key, which
    # is the defect class that commit eliminated one level up.
    _slug_field = (_validated_corpus_string(
        plugins.get("issuing_body_slug_field"), "plugins.issuing_body_slug_field",
        default="") or "").strip() or None
    mcp = raw.get("mcp", {}) or {}
    status = raw.get("status", {}) or {}
    provenance = raw.get("provenance", {}) or {}
    siblings = [
        Sibling(
            id=s["id"],
            index_url=s.get("index_url"),
            web_base=s.get("web_base", "") or "",
            index_path=_resolve(root, s.get("index_path")),
        )
        for s in raw.get("siblings", []) or []
    ]

    return CorpusConfig(
        root=root,
        config_path=config_path,
        id=corpus_id,
        name=_validated_corpus_string(corpus.get("name"), "name", default=corpus_id),
        jurisdiction=_validated_corpus_string(
            corpus.get("jurisdiction"), "jurisdiction", default=""),
        archetype=_validated_archetype(corpus.get("archetype")),
        authoritative_source=(
            _validated_corpus_string(corpus.get("authoritative_source"),
                                     "authoritative_source", default="") or "").strip()
        or None,
        schema_version=int(corpus.get("schema_version", 1)),
        contract_version=int(corpus.get("contract_version", 1)),
        content_roots=content_roots,
        disclaimer_marker=raw.get("disclaimer_marker", "NON-AUTHORITATIVE"),
        graph_path=_resolve(root, raw.get("graph_path", "_meta/graph.json")),
        source_manifest_path=_resolve(
            root, raw.get("source_manifest_path", "_meta/source-manifest.yml")),
        snapshot_dir=_resolve(root, raw.get("snapshot_dir", "_meta/snapshots")),
        snapshot_slice_module=plugins.get("snapshot_slice_module"),
        extraction_module=plugins.get("extraction_module"),
        citation_module=plugins.get("citation_module"),
        semantic_search_module=plugins.get("semantic_search_module"),
        retrieval_module=plugins.get("retrieval_module"),
        tools_module=plugins.get("tools_module"),
        index_headings=_validated_index_headings(raw.get("index_headings")),
        volatile_patterns=_validated_volatile_patterns(raw.get("volatile_patterns")),
        issuing_body_registry=_resolve(root, plugins.get("issuing_body_registry")),
        issuing_body_registry_key=plugins.get("issuing_body_registry_key", "entries"),
        issuing_body_profiles=_resolve(root, plugins.get("issuing_body_profiles")),
        issuing_body_name_fields=_validated_name_fields(
            plugins.get("issuing_body_name_fields", _ABSENT),
            registry=plugins.get("issuing_body_registry")),
        issuing_body_slug_field=_slug_field,
        issuing_body_slug_sentinels=_validated_slug_sentinels(
            plugins.get("issuing_body_slug_sentinels", _ABSENT),
            field=_slug_field,
            registry_slugs=_registry_slugs_at_load(
                _resolve(root, plugins.get("issuing_body_registry")),
                plugins.get("issuing_body_registry_key", "entries"))),
        extra_schema_checks=plugins.get("extra_schema_checks", []) or [],
        mcp_server_name=mcp.get("server_name") or corpus_id or "corpus",
        mcp_transports=mcp.get("transports", ["stdio", "http"]),
        mcp_extra_document_fields=list(mcp.get("extra_document_fields", []) or []),
        extra_doc_types=_validated_extra_doc_types(raw.get("schema")),
        mcp_authority_relations=_validated_authority_relations(
            mcp.get("authority_relations")),
        reverify_days=int(status.get("reverify_days", 90)),
        coverage_fail_threshold=float(provenance.get("coverage_fail_threshold", 0.70)),
        coverage_warn_threshold=float(provenance.get("coverage_warn_threshold", 0.90)),
        raw=raw,
        siblings=siblings,
    )
