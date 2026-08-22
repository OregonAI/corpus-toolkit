"""Loads `_meta/corpus.yml` — the single source of truth every toolkit module
reads instead of hardcoding corpus-specific paths, directories, or enums."""
from __future__ import annotations

import dataclasses
import functools
import ipaddress
import re
import socket
import urllib.parse
from pathlib import Path
from typing import NamedTuple

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

    def _as_a_reader_names_it(self, path):
        """A declared file as a READER names it — repo-relative where it is inside the
        repo, absolute where a corpus points somewhere else.

        One spelling, because the validator's findings and the MCP tools' notes are read by
        the same people about the same file, and an absolute `/tmp/.../_meta/registry.yml`
        in one and `_meta/registry.yml` in the other reads as two files. Shared by every
        declared-file property rather than re-spelled per file: a second copy that forgot
        the `ValueError` arm would raise on the one corpus that points outside its repo.
        """
        if not path:
            return None
        try:
            return path.relative_to(self.root)
        except ValueError:
            return path

    @property
    def issuing_body_registry_rel(self):
        """The issuing-body registry file as a reader names it."""
        return self._as_a_reader_names_it(self.issuing_body_registry)

    @property
    def issuing_body_profiles_rel(self):
        """The curated issuing-body profiles file as a reader names it."""
        return self._as_a_reader_names_it(self.issuing_body_profiles)

    @property
    def front_door_fault(self) -> "FrontDoorFault | None":
        """What is wrong with this corpus's `corpus.authoritative_source`, or None.

        THE PROPERTY, NOT THE MODULE FUNCTION, IS WHAT CALLERS WANT: the validator and
        `corpus_overview` both hold a config, and both used to answer this question their
        own way — which is how the running server ended up carrying the template's
        placeholder in silence while CI refused it (corpus-toolkit#140). Same shape as
        `issuing_body_registry_fault` above, and for the same reason.

        NOT CHECKED AT LOAD, DELIBERATELY. `load()` keeps `str | None` and a server keeps
        starting: a pin bump must not take down a corpus that was legal when it deployed,
        and `authoritative_source: null` is a documented response value. Refusing is a
        repo gate; this property is how the runtime gets to SAY something without becoming
        one.
        """
        return front_door_fault(self.authoritative_source)

    @property
    def issuing_body_registry_fault(self) -> str | None:
        """One sentence naming the config key, the file and what went wrong — or None where
        this corpus declares no registry, or declares one that read fine.

        ONE WORDING, FOUR READERS. The validator's finding, `search_corpus`'s filter note,
        `documents_by_agency`'s attribution note, `issuing_body_profile`'s error and the
        server's startup warning are all about the same broken file and are read by the
        same people. Each assembling its own "file + reason" prose is how one of them ends
        up naming the file and not the key, which is half an answer to an operator who has
        to change the key.
        """
        if not self.issuing_body_registry or self.issuing_body_registry_read.readable:
            return None
        return (f"plugins.issuing_body_registry {self.issuing_body_registry_rel} "
                f"{self.issuing_body_registry_read.problem}")

    @property
    def issuing_body_profiles_fault(self) -> str | None:
        """One sentence naming the config key, the file and what went wrong — or None where
        this corpus declares no curated profiles, or declares some that read fine.

        A SECOND SENTENCE, NOT A SECOND USE OF THE REGISTRY'S. `plugins.issuing_body_profiles`
        is a different key naming a different file with a different fix, and the two are read
        back to back by one tool — so reporting a broken overlay in the registry's words sends
        an operator to edit the file that was never at fault (corpus-toolkit#143). Declared
        here rather than assembled at the call site for the reason the registry's is: the
        wording is the part that has to agree, and prose written per caller does not.
        """
        if not self.issuing_body_profiles or self.issuing_body_profiles_read.readable:
            return None
        return (f"plugins.issuing_body_profiles {self.issuing_body_profiles_rel} "
                f"{self.issuing_body_profiles_read.problem}")

    @functools.cached_property
    def issuing_body_profiles_read(self) -> ProfilesRead:
        """This corpus's curated issuing-body profiles, read once — the overlay, and why not.

        Read through this rather than parsing the file at a call site. `issuing_body_profile`
        did the latter and so raised whatever the file raised — `ParserError`, `PermissionError`,
        `UnicodeDecodeError`, or `AttributeError` from `.get` on a document that parsed to a
        list — after the registry beside it had already been hardened against exactly that
        (corpus-toolkit#136, #143). Cached to match the registry read: one declared file, one
        read, one answer.
        """
        return read_issuing_body_profiles(self.issuing_body_profiles)

    @functools.cached_property
    def issuing_body_registry_read(self) -> RegistryRead:
        """This corpus's issuing-body registry, read once — entries, slugs, and why not.

        Cached because the index build asks it once per document, and because a corpus with
        a broken registry would otherwise re-read and re-fail the same file per document.
        Read through this rather than re-parsing the registry anywhere: two readers
        disagreeing about which slugs exist is the kind of drift that shows up as a count
        instead of an error, and a second reader that RAISES where this one reports is
        corpus-toolkit#136 exactly.
        """
        return read_issuing_body_registry(self.issuing_body_registry,
                                          self.issuing_body_registry_key)

    @property
    def issuing_body_slugs(self) -> frozenset[str] | None:
        """Every slug in the issuing-body registry, or None when there is no registry this
        corpus can read — in which case "does this value name a body?" is a question with no
        answer here, and callers must report it as unknown rather than as no.

        NONE COVERS BOTH "DECLARES NONE" AND "DECLARES ONE IT CANNOT READ", deliberately:
        neither can answer the question. They are not the same finding, though — one is a
        choice and the other a fault — so a caller that reports the difference asks
        `issuing_body_registry_read.problem`, which names the file and what went wrong.
        A declared-but-unparseable registry used to RAISE here instead, out of whatever
        asked, including a live MCP tool call (corpus-toolkit#136).
        """
        return self.issuing_body_registry_read.slugs

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


class RegistryRead(NamedTuple):
    """ONE READ OF THE ISSUING-BODY REGISTRY, and every question anything asks of it.

    THE one registry reader, for the runtime and the validator alike. It lives in `config`
    because that is the floor both `mcp` and `validate` already import: the validator grew
    this shape first (corpus-toolkit#129) and the runtime raised where the validator
    reported, so the same registry answered two ways depending on who asked
    (corpus-toolkit#136). One fact declared twice with nothing gating agreement is the
    shape of five separate defects in this project; this is the one declaration.

    `entries` is None for COULD NOT READ, WHICH IS NEVER THE SAME ANSWER AS AN EMPTY
    REGISTRY. Every caller gates on that distinction: the per-file checks skip rather than
    report every document's slug as unregistered, `_check_config` declines to call a
    declared name field unmatched by a registry nobody could open, and the MCP tools report
    "could not check" rather than "not a slug".

    `problem` is why, in one line, or None when there was nothing to read (this corpus
    declares no registry) or nothing wrong. A CALLER MUST NOT READ `problem is None` AS
    "READABLE" — `readable` answers that, and the two differ for a corpus that declares no
    registry at all: nothing failed, and there is still nothing to check against.

    The derived answers hang off the read rather than being recomputed per caller. Two
    derivations of one registry drift into disagreeing about which slugs exist — a third
    one written inline at a call site is how that starts.
    """
    entries: list | None
    problem: str | None

    @property
    def readable(self) -> bool:
        return self.entries is not None

    @staticmethod
    def _slug_of(entry):
        """The slug this row can be attributed by, or None — for ANY row shape.

        A registry is hand-maintained, so a row may be a bare string or a number where an
        entry was meant. `entry["slug"]` and `entry.get(...)` both raise on those, which is
        why every caller asks through here.
        """
        return entry.get("slug") if isinstance(entry, dict) else None

    @property
    def slugs(self) -> frozenset[str] | None:
        """The registry's slugs, or None where there was nothing readable to check."""
        if self.entries is None:
            return None
        return frozenset(s for e in self.entries if (s := self._slug_of(e)))

    @property
    def without_slug(self) -> int:
        """Rows nothing can ever be attributed to: no `slug`, or not an entry at all.

        BOTH SHAPES, BECAUSE BOTH HAVE THE SAME CONSEQUENCE. A bare string under
        `entries:` used to be filtered out before anything counted it — the registry read
        clean, the validator reported nothing, and every document naming that body was
        reported as unregistered. A check that passes without checking anything is a defect
        in this repo (AGENTS.md), and this one was silent about exactly the row a human
        mistyped.
        """
        return 0 if self.entries is None else sum(
            1 for e in self.entries if not self._slug_of(e))

    @property
    def by_slug(self) -> dict:
        """Every row that can be attributed to, keyed by its slug. The lookup
        `issuing_body_profile` serves from, so a row of the wrong shape is skipped there
        the same way it is counted here rather than raising in one and not the other."""
        return {s: e for e in (self.entries or []) if (s := self._slug_of(e))}

    @property
    def mappings(self) -> list:
        """The rows that are entries — the ones a field can be read off at all. `entries`
        is what the file holds; `without_slug` counts the difference."""
        return [e for e in (self.entries or []) if isinstance(e, dict)]


class ProfilesRead(NamedTuple):
    """ONE READ OF THE CURATED ISSUING-BODY PROFILES — the optional overlay
    `issuing_body_profile` lays over the registry's identity for a body.

    THE SECOND FILE THAT TOOL READS, and for a release it was the only one that could still
    take the call down. `RegistryRead` beside it turned "declared file I cannot read" into a
    reported condition (corpus-toolkit#136); the overlay two lines later was parsed inline
    and raised whatever it raised (corpus-toolkit#143). Same shape, deliberately, so that
    the two files fail the same way — but a SEPARATE type, because the two hold different
    things and a caller that got a `RegistryRead` back from the overlay would ask it for
    `slugs`.

    `profiles` is None for COULD NOT READ, WHICH IS NEVER THE SAME ANSWER AS AN OVERLAY
    THAT CURATES NOTHING. "This corpus has no curated notes for this body" and "this
    corpus's curated notes could not be read" are two findings with two different fixes,
    and serving the second as the first is the collapse this platform files bugs about.

    `problem` is why, in one line, or None when there was nothing to read (this corpus
    declares no overlay) or nothing wrong. A CALLER MUST NOT READ `problem is None` AS
    "READABLE" — `readable` answers that, and the two differ for a corpus that declares no
    overlay at all: nothing failed, and there is still nothing to lay over the registry.
    """
    profiles: dict | None
    problem: str | None

    @property
    def readable(self) -> bool:
        """Whether there is an overlay to lay over the registry.

        FALSE COVERS BOTH "DECLARES NONE" AND "DECLARES ONE IT CANNOT READ", the same way
        `CorpusConfig.issuing_body_slugs` is None for both: neither has an overlay to
        serve. They are NOT the same finding — one is a choice and the other a fault — so
        a caller that reports the difference asks `CorpusConfig.issuing_body_profiles_fault`,
        which is None for the choice and names the file and the reason for the fault.
        `if not read.readable: warn(...)` on its own would warn every corpus that simply
        declares no overlay.
        """
        return self.profiles is not None

    def for_slug(self, slug: str):
        """The curated notes for one body — `{}` where there are none AND where there was
        nothing readable.

        THE VALUE CANNOT TELL THOSE TWO APART, and that is the whole hazard: `readable` is
        what distinguishes them, so a caller serving this must also serve
        `CorpusConfig.issuing_body_profiles_fault` or it has reported "could not check" as
        "is not there". Whatever a corpus curated is served as it wrote it — coercing an
        unexpected shape here would delete curated data on the way out.
        """
        return (self.profiles or {}).get(slug, {})


# ---------------------------------------------------------------- the front door
#
# ONE DECLARATION, TWO READERS, AND THAT IS THE WHOLE POINT (corpus-toolkit#140).
# `corpus-validate-frontmatter` gates the repo and `corpus_overview` tells a live agent
# what it is talking to; they are asking the SAME question about the SAME field, and the
# defect that brought this here was that they answered it differently — the validator
# refused `https://REPLACE-ME.invalid/...` while the running server carried it on every
# response without a word. Both now read the sentence below, so neither can drift from
# the other; this is the `issuing_body_registry_fault` arrangement (corpus-toolkit#136)
# applied to the field next door, and `mcp` importing a private helper out of `validate`
# would have been the arrangement #136 removed.

# RFC 2606 reserves these names so they can never belong to anyone: §2 sets aside the
# top-level domains, §3 the second-level `example.*` names, both for documentation and
# examples. A corpus's front door can never legitimately live under one, which is what
# makes "this is a placeholder, not an answer" checkable without guessing at intent —
# `corpus-template` ships `https://REPLACE-ME.invalid/where-the-official-text-lives`
# precisely because `.invalid` cannot resolve.
_RESERVED_TLDS = ("test", "example", "invalid", "localhost")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")

# The template's marker, kept as a NAMED BACKSTOP to the RFC 2606 rule rather than as the
# rule itself. The general rule is the reserved names — an author who edits the path and
# leaves the host still ships a dead pointer, which a `REPLACE-ME` match alone would miss
# — but the reverse edge is real too: swap `.invalid` for a live TLD, or drop it, and
# `https://REPLACE-ME.oregon.gov/...` is off the reserved list while still being the
# template's unfilled line.
_TEMPLATE_MARKER = "replace-me"

# The value's actionable half, shared by every finding about the field — validator and
# running server alike — so that a corpus meeting it for the first time is told the same
# thing however it got here.
#
# PRIVATE, LIKE EVERY HELPER IN THIS SECTION. AGENTS.md makes anything a corpus repo can
# reach public surface that cannot be renamed, and the only names this section needs to
# expose are `FrontDoorFault` and `front_door_fault` — every other name here has exactly
# one caller, inside this module.
_HOW_TO_SET_THE_FRONT_DOOR = (
    "Set it to this corpus's front door: the one page a reader opens to reach its "
    "official text — one line under `corpus:` in _meta/corpus.yml, e.g. "
    '`authoritative_source: "https://sos.oregon.gov/archives/records/"`. It need not '
    "cover every publisher — a corpus spanning several declares its best single entry "
    "point, and get_document answers per document from that document's source_url.")


class FrontDoorFault(NamedTuple):
    """What is wrong with a `corpus.authoritative_source`, and the sentence that says so.

    `exempt_while_uninstantiated` is the ONE thing a caller needs beyond the sentence, and
    it is carried here rather than re-derived from the message so that no caller has to
    match on prose to pick a severity. It answers: may an uninstantiated `corpus-template`
    — `corpus.id` still `{{CORPUS_ID}}` and no documents — report this as a warning rather
    than an error?

    TRUE FOR EXACTLY THE TWO STATES AN UNEDITED TEMPLATE IS LEGITIMATELY IN: no value at
    all, and the placeholder the template itself ships. Those it cannot avoid until someone
    fills the field in. Every other fault is a value somebody CHOSE, and the exemption
    covers the template's own starting state, not whatever a fork typed over it — widening
    it past those two states would turn a state-based exemption into a way to keep a bad
    front door.

    `corpus-validate-frontmatter` is the only reader of the flag; the MCP layer has one
    severity and reads only the message.
    """
    message: str
    exempt_while_uninstantiated: bool


def _front_door_host(url: str) -> str:
    """`url`'s host, normalised — lowercased and with a trailing dot stripped.

    Raises ValueError exactly where `urllib.parse.urlsplit` does: `https://[oops` is an
    "Invalid IPv6 URL". Callers decide what to say about that; this function will not
    swallow it, because a value convention 1 promises is a URL and is not one is a
    finding, never a shrug.
    """
    return (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()


def _reserved_name(host: str) -> str | None:
    """The RFC 2606 reserved name `host` sits under, or the template's marker, or None.

    Takes the HOST, never the URL text. A substring match on `example` or `invalid` would
    reject `https://sos.oregon.gov/archives/example-schedules` — a real front door whose
    path happens to say example — and would miss nothing a host check misses.

    An empty host (`https:///x`, or a value the caller could not parse) returns None: the
    callers own the message for a value that is not a usable URL.

    WHERE THIS RULE STOPS: an address literal is not a NAME, so `http://127.0.0.1:8000`
    is not reserved here. `_address_literal` below owns it, and says why it is a separate
    rule rather than a widening of this one.
    """
    if not host:
        return None
    labels = host.split(".")
    if labels[-1] in _RESERVED_TLDS:
        return "." + labels[-1]
    second_level = ".".join(labels[-2:])
    if second_level in _RESERVED_DOMAINS:
        return second_level
    # LAST, so the template's own `REPLACE-ME.invalid` is explained by the general rule
    # (the reserved name is the reason it can never resolve); the marker only has to speak
    # for the hosts that rule no longer covers.
    return "REPLACE-ME" if _TEMPLATE_MARKER in labels else None


def _address_literal(host: str) -> "ipaddress.IPv4Address | ipaddress.IPv6Address | None":
    """`host` as an IP address, or None where it is a NAME rather than an address.

    A SECOND RULE, NOT A WIDENING OF THE FIRST (corpus-toolkit#138) — the one place that
    argument is made. RFC 2606 reserves NAMES, so `http://localhost:8000` failed the gate
    and `http://127.0.0.1:8000`, the same dead pointer differently spelled, sailed through
    it. The two are refused for genuinely different reasons — a name that can never
    resolve, and an address that resolves only to the machine asking (RFC 5735/6890) —
    the messages say which, and folding the second under the first would make a cited RFC
    wrong about the value it is quoted at.

    `ipaddress.ip_address` ALONE IS NOT ENOUGH, MEASURED. Python's parser takes strict
    dotted quads only and has rejected leading zeros since CVE-2021-29921, so it raises on
    `127.1` and on `127.000.000.001` — the two shortenings corpus-toolkit#138 names, and
    the two every resolver in the path turns into 127.0.0.1. A guard built on it alone
    would have shipped with exactly the hole it was written to close. `socket.inet_aton`
    is the classic parser those forms are written for, and it is consulted second: it
    accepts one-, two-, three- and four-part forms and returns four packed bytes, which
    `ip_address` re-reads as the address a browser would reach.

    IPv4-MAPPED IPv6 IS UNWRAPPED, because `IPv6Address("::ffff:127.0.0.1").is_loopback`
    is False and the address is the loopback: the classification below would otherwise
    name the wrong reason for a host that reaches the agent's own machine.
    """
    if not host:
        return None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            return None
    return getattr(addr, "ipv4_mapped", None) or addr


def _why_not_a_front_door(
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Why this particular address cannot be a corpus's front door.

    EVERY LITERAL IS REFUSED, LOOPBACK OR NOT, AND THAT IS A DELIBERATE CHOICE.
    corpus-toolkit#138's own remedy asks for the class — "an IPv4/IPv6 literal host
    (`ipaddress.ip_address(host)` parses) is never a corpus's front door, loopback or not
    — a public official-text page has a name" — while the maintainer comment beneath it
    names only loopback, the case that prompted the issue. Going with the wider of the two
    because the line drawn at loopback is a line a one-character edit walks through, and
    each neighbouring class is at least as bad:

      * `0.0.0.0` and `::` are the WILDCARD A SERVER BINDS TO, never an address a client
        connects to. A corpus that pasted the address its dev server printed has pasted
        this one about as often as it has pasted 127.0.0.1.
      * `192.168.*`, `10.*`, `172.16-31.*`, `169.254.*` and `fc00::/7` resolve on the
        READER'S OWN NETWORK. That is worse than a dead pointer, not better: it can
        answer, from a host that has nothing to do with this corpus, differently for
        every reader.
      * a globally routable literal like `8.8.8.8` genuinely can answer — and still names
        no publisher, cannot be checked against any TLS certificate name, and stops being
        this corpus's front door the day the host is renumbered. Official text is
        published under a name; all nine live corpora and every publisher they draw on
        use one.

    The cost of the wide rule is a corpus that had a name available and typed an address
    instead, which the finding tells it to fix in one line. The cost of the narrow one is
    the class staying half-open.
    """
    if addr.is_loopback:
        return ("a loopback address, which resolves only to the machine asking — on an "
                "agent's own host, to whatever happens to be listening there")
    if addr.is_unspecified:
        return ("the wildcard address a server BINDS to, which is never an address a "
                "client can connect to")
    if addr.is_link_local:
        return ("a link-local address, which reaches only whatever shares the reader's "
                "network segment")
    if addr.is_private:
        return ("a private address, which resolves on the reader's own network rather "
                "than on the publisher's — differently for every reader, and for some of "
                "them it answers")
    return ("routable, and still not a front door: it names no publisher, matches no TLS "
            "certificate name, and stops pointing at this corpus the day the host is "
            "renumbered")


def front_door_fault(value: str | None) -> FrontDoorFault | None:
    """Why `value` cannot be a corpus's front door, or None where no rule here refuses it.

    None IS THE ABSENCE OF A FINDING, NOT A CLAIM THAT THE URL IS GOOD. Nothing here
    fetches anything; a host that is a name, spelled plausibly, and long dead reads as None
    and always will. Callers may say "nothing about this value says it cannot be a front
    door" and must not say "this front door works".

    EVERY WAY THIS FIELD CAN FAIL, IN ONE PLACE, because the caller that enumerates a
    SUBSET is the bug this function was extracted to end. `corpus_overview` used to ask
    only "is it missing?", so a corpus serving the template's placeholder was told
    nothing at all; a caller that re-lists the interesting kinds re-opens that gap at a
    new offset the next time a way is added. Callers ask "is anything wrong", and read
    `exempt_while_uninstantiated` only to choose how loudly to say it.

    ORDER IS NARROWING, AND THE MESSAGES DEPEND ON IT: is there a value, is it a URL, can
    it be parsed, does it name a host, and only then what that host is. Each branch may
    assume everything above it, which is why the reserved-name rule can talk about a host
    without hedging about whether there is one.
    """
    text = str(value or "")
    if not text:
        return FrontDoorFault((
            "corpus.authoritative_source is not set — MCP responses carry "
            "`authoritative_source: null`, so an agent is told to verify at source "
            "without being told where to start (response convention 1). "
            + _HOW_TO_SET_THE_FRONT_DOOR), True)
    if not text.startswith(("http://", "https://")):
        # A non-URL here is worse than nothing: convention 1 says the field IS a URL, so a
        # caller will try to follow it.
        return FrontDoorFault(
            f"corpus.authoritative_source must be a URL, got {value!r}", False)
    try:
        host = _front_door_host(text)
    except ValueError as e:
        return FrontDoorFault((
            f"corpus.authoritative_source is {text!r}, which starts like a URL but "
            f"cannot be parsed as one ({type(e).__name__}: {e}). Response convention 1 "
            f"says this field IS a URL and a caller will try to follow it. "
            + _HOW_TO_SET_THE_FRONT_DOOR), False)
    if not host:
        # `https:///schedules` clears the `https://` branch above and names nowhere to go.
        return FrontDoorFault(
            f"corpus.authoritative_source is {text!r}, which names no host, so it points "
            f"nowhere. " + _HOW_TO_SET_THE_FRONT_DOOR, False)
    if (name := _reserved_name(host)):
        why = ("the marker corpus-template leaves for you to replace"
               if name == "REPLACE-ME" else
               "a name RFC 2606 reserves so that it can never be a real host")
        return FrontDoorFault((
            f"corpus.authoritative_source is {text!r}, whose host carries {name} — {why}. "
            f"It parses as a URL, so nothing downstream refuses it: every MCP response "
            f"carries it and tells an agent to verify somewhere that cannot answer. This "
            f"is a placeholder, not a front door. " + _HOW_TO_SET_THE_FRONT_DOOR), True)
    if (addr := _address_literal(host)) is not None:
        # AFTER the name rule, so `localhost` keeps being refused as the RFC 2606 reserved
        # NAME it is — the order is what documents that the two rules are two.
        #
        # NOT EXEMPT FOR AN UNINSTANTIATED TEMPLATE, unlike the placeholder above. The
        # exemption covers the two states the template is legitimately in, and an address
        # is neither: corpus-template does not ship one, so a template carrying one is a
        # value somebody typed, and it is exactly the "ran it against my dev server and
        # pasted what it printed" case this rule exists for.
        return FrontDoorFault((
            f"corpus.authoritative_source is {text!r}, whose host {host!r} is an IP "
            f"address rather than a name — {_why_not_a_front_door(addr)}. It parses as a "
            f"URL, so nothing downstream refuses it: every MCP response carries it and "
            f"sends an agent to an address instead of to this corpus's publisher. An "
            f"address is not a front door. " + _HOW_TO_SET_THE_FRONT_DOOR), False)
    return None


def _read_declared_yaml(path: Path):
    """Parse one file a corpus DECLARED in its config: its data, or why not, never a raise.

    THE WHOLE OF "GONE, UNOPENABLE, UNPARSEABLE, OR NOT TEXT", in one place, because every
    declared config file fails those four ways and each reader that spelled them out again
    got a subset. The issuing-body registry's reader was hardened first
    (corpus-toolkit#136) and the curated profiles file beside it kept parsing inline and
    raising for another release (corpus-toolkit#143) — a second spelling of "unreadable"
    that disagreed with the first is precisely what this shares to prevent.

    A FILE IS READ AS TEXT AND NOT EVERY FILE IS ONE: a latin-1 or binary file raises
    `UnicodeDecodeError`, which is a `ValueError` and so slips an `OSError`/`YAMLError`
    catch. It is the same condition as a parse failure and is caught here with them.

    An EMPTY file parses to None, which is returned as `{}` — a declared file saying
    nothing, which is a thing a reader may legitimately act on. What shape the data has to
    be is each reader's question, not this one's.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        # ONE FINDING, ONE LINE, because a `Reporter` finding is a line and an MCP note is
        # a sentence. A yaml.YAMLError's message is several lines with a caret diagram;
        # pasted in raw, the rest of the sentence ended up under a `^`, reading as a
        # different message.
        detail = " ".join(str(e).split())[:200]
        return None, f"could not be read: {type(e).__name__}: {detail}"
    return ({} if data is None else data), None


def read_issuing_body_registry(registry_path, key: str) -> RegistryRead:
    """Read the issuing-body registry once, for every question that asks about it.

    Takes a path and a key rather than a `CorpusConfig` because `load()` asks this before
    there is a config to ask through (the sentinel/registry clash check), and the answer
    must be the same one every later caller gets.

    A file that parses to a mapping with a missing or null entries key is READ, and holds
    no entries — that is a registry saying nothing, and callers are entitled to act on it
    exactly as they always have. UNREADABLE IS: gone, unopenable, unparseable, or shaped
    like something other than a registry. All four are one condition from a caller's point
    of view — *this corpus declares a registry it cannot read* — and the reason says which.

    Entry-level shape is tolerant: a non-mapping entry, or one with no slug, is skipped
    rather than raised over. That used to raise `KeyError` out of the load, a traceback
    naming neither the file nor the row — which is a bad message but a loud one, so
    `RegistryRead.without_slug` carries the count and `_check_registry` reports it instead
    of letting a broken row disappear.

    A REGISTRY IS READ AS TEXT AND NOT EVERY FILE IS ONE: a latin-1 or binary file raises
    `UnicodeDecodeError`, which is a `ValueError` and so slips an `OSError`/`YAMLError`
    catch. It is the same condition as a parse failure and is caught here with them.
    """
    if not registry_path:
        return RegistryRead(None, None)
    data, problem = _read_declared_yaml(Path(registry_path))
    if problem:
        return RegistryRead(None, problem)
    if not isinstance(data, dict):
        return RegistryRead(None, (f"could not be read as a registry: expected a mapping "
                                   f"with a {key!r} list, got {type(data).__name__}"))
    entries = data.get(key) or []
    if not isinstance(entries, list):
        return RegistryRead(None, (f"could not be read as a registry: {key!r} must be a "
                                   f"list of entries, got {type(entries).__name__}"))
    # EVERY ROW, INCLUDING THE ONES THAT ARE NOT ENTRIES. Filtering them out here is how
    # they stopped being counted at all; `mappings` is for callers that need to read a
    # field off a row, and `without_slug` is the finding.
    return RegistryRead(list(entries), None)


_PROFILES_KEY = "profiles"
"""The one key a curated issuing-body profiles file holds its overlay under.

NOT A CONFIG KEY, deliberately, unlike `issuing_body_registry_key`. The registry's key is
declarable because corpora arrived with registries already keyed their own way; the profiles
file is a shape this toolkit defined, `corpus-template` documents as
`{profiles: {slug: {...}}}`, and every corpus that has one writes that way.

UNDERSCORED, so it is not surface a corpus repo can pin. AGENTS.md counts anything reachable
from a corpus repo as public whatever it is prefixed with, but this name has never been
reachable and does not become so by being extracted from the three f-strings and the lookup
in `read_issuing_body_profiles` that had to agree on it."""


def read_issuing_body_profiles(profiles_path) -> ProfilesRead:
    """Read the curated issuing-body profiles once, reporting rather than raising.

    THE SAME TREATMENT AS `read_issuing_body_registry`, ONE FILE OVER (corpus-toolkit#143).
    `issuing_body_profile` read the registry through that reader and then parsed this file
    inline on the next line, so the optional half of its answer could take the whole call
    down: registry identity, holdings and attribution lost to a file whose ABSENCE would
    have cost nothing.

    UNREADABLE IS: gone, unopenable, unparseable, or shaped like something other than a
    profiles file. All four are one condition from a caller's point of view — *this corpus
    declares curated profiles it cannot read* — and the reason says which. The old inline
    parse guarded exactly one of the four, `is_file()`, which is the only one that never
    raised.

    SHAPE IS CHECKED AT BOTH LEVELS because `.get` is called at both: a document that
    parses to a list or a string has no `.get`, and neither has a `profiles:` key holding
    a list, one level down where `curated.get(slug)` reaches. A file that holds a mapping
    with a missing or null `profiles` key IS read and curates nothing — that is an overlay
    saying nothing, which is a corpus's prerogative.

    Per-slug values are NOT shape-checked: what a corpus curates about a body is its own
    editorial content, and this reader has no standing to refuse it.
    """
    if not profiles_path:
        return ProfilesRead(None, None)
    data, problem = _read_declared_yaml(Path(profiles_path))
    if problem:
        return ProfilesRead(None, problem)
    if not isinstance(data, dict):
        return ProfilesRead(None, (f"could not be read as curated profiles: expected a "
                                   f"mapping with a {_PROFILES_KEY!r} key, got "
                                   f"{type(data).__name__}"))
    profiles = data.get(_PROFILES_KEY) or {}
    if not isinstance(profiles, dict):
        return ProfilesRead(None, (f"could not be read as curated profiles: "
                                   f"{_PROFILES_KEY!r} must be a mapping of registry slug "
                                   f"to notes, got {type(profiles).__name__}"))
    return ProfilesRead(dict(profiles), None)


def _parse_registry_slugs(path: Path, key: str) -> frozenset[str]:
    """KEPT AS A NAME, NOT AS A SECOND PARSER. Nothing in this toolkit calls it any more —
    `read_issuing_body_registry` is the one reader — but a name a corpus repo can import is
    public surface whatever it is prefixed with (AGENTS.md), and absence of a local caller
    is not evidence of no caller.

    It keeps its old shape: the slugs, or a raise for a registry that cannot be read. The
    exception is now a `ValueError` naming the file and the problem rather than whatever
    the file happened to raise — which is the whole point of corpus-toolkit#136 for every
    caller inside this toolkit, and a message rather than a traceback for any outside it.
    """
    read = read_issuing_body_registry(path, key)
    if not read.readable:
        raise ValueError(f"{path} {read.problem}")
    return read.slugs


def _registry_slugs_at_load(registry_path, key: str):
    """The registry's slugs during `load()`, or None when there is no registry to read.

    `CorpusConfig.issuing_body_slugs` answers the same question but is a cached_property on
    the config being constructed, so it is unavailable here. It reads through the same
    reader, so the two cannot disagree; the sentinel/registry clash check is the only
    caller, and a registry that cannot be read yields None, which that check treats as
    "unknown" and skips rather than as "no slugs" — an unreadable registry must not turn
    every sentinel into a spurious clash-free pass OR a spurious error.
    """
    return read_issuing_body_registry(registry_path, key).slugs


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
