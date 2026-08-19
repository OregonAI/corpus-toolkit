"""Loads `_meta/corpus.yml` — the single source of truth every toolkit module
reads instead of hardcoding corpus-specific paths, directories, or enums."""
from __future__ import annotations

import dataclasses
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
    # Optional here rather than required because four corpora already ship
    # without one and a hard load failure would take their servers down on a pin bump;
    # `corpus-validate-frontmatter` reports the omission instead, and corpus_overview
    # says so on the response.
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
    issuing_body_registry: Path | None
    issuing_body_registry_key: str
    issuing_body_profiles: Path | None
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
    path = config.source_manifest_path
    if path is None:
        return []
    if path.is_dir():
        groups = []
        for p in sorted(path.glob("*.yml")):
            g = yaml.safe_load(p.read_text()) or {}
            g.setdefault("group", p.stem)
            groups.append(g)
        if not groups:
            raise MissingSourceManifest(
                f"source_manifest_path {path} is a directory containing no *.yml group "
                f"files. Refusing to report zero sources as success. (A directory of "
                f"*.yaml files is the usual cause — the loader only reads *.yml.)")
        return groups
    if path.is_file():
        return [yaml.safe_load(path.read_text()) or {}]
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


def load(config_path: str | Path) -> CorpusConfig:
    config_path = Path(config_path).resolve()
    root = config_path.parent.parent
    raw = yaml.safe_load(config_path.read_text()) or {}

    corpus = raw.get("corpus", {})
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
        id=corpus.get("id", ""),
        name=corpus.get("name", corpus.get("id", "")),
        jurisdiction=corpus.get("jurisdiction", ""),
        archetype=_validated_archetype(corpus.get("archetype")),
        authoritative_source=(corpus.get("authoritative_source") or "").strip() or None,
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
        issuing_body_registry=_resolve(root, plugins.get("issuing_body_registry")),
        issuing_body_registry_key=plugins.get("issuing_body_registry_key", "entries"),
        issuing_body_profiles=_resolve(root, plugins.get("issuing_body_profiles")),
        extra_schema_checks=plugins.get("extra_schema_checks", []) or [],
        mcp_server_name=mcp.get("server_name", corpus.get("id", "corpus")),
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
