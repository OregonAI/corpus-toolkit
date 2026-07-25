"""Loads `_meta/corpus.yml` — the single source of truth every toolkit module
reads instead of hardcoding corpus-specific paths, directories, or enums."""
from __future__ import annotations

import dataclasses
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
class CorpusConfig:
    root: Path                # repo root (parent of `_meta/`)
    config_path: Path
    id: str
    name: str
    jurisdiction: str
    archetype: str
    schema_version: int
    contract_version: int
    content_roots: list[ContentRoot]
    disclaimer_marker: str
    graph_path: Path
    source_manifest_path: Path
    snapshot_dir: Path
    snapshot_slice_module: str | None
    citation_module: str | None
    semantic_search_module: str | None
    issuing_body_registry: Path | None
    issuing_body_registry_key: str
    issuing_body_profiles: Path | None
    extra_schema_checks: list[dict]
    mcp_server_name: str
    mcp_transports: list[str]
    reverify_days: int
    coverage_fail_threshold: float
    coverage_warn_threshold: float
    raw: dict

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
        return groups
    if path.is_file():
        return [yaml.safe_load(path.read_text()) or {}]
    return []


def iter_manifest_sources(config: "CorpusConfig"):
    """Yield every source dict ({id, url, sha256, ...}) across all manifest
    groups, in group order."""
    for g in load_source_manifest_groups(config):
        yield from (g.get("sources", []) or [])


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

    return CorpusConfig(
        root=root,
        config_path=config_path,
        id=corpus.get("id", ""),
        name=corpus.get("name", corpus.get("id", "")),
        jurisdiction=corpus.get("jurisdiction", ""),
        archetype=corpus.get("archetype", "document"),
        schema_version=int(corpus.get("schema_version", 1)),
        contract_version=int(corpus.get("contract_version", 1)),
        content_roots=content_roots,
        disclaimer_marker=raw.get("disclaimer_marker", "NON-AUTHORITATIVE"),
        graph_path=_resolve(root, raw.get("graph_path", "_meta/graph.json")),
        source_manifest_path=_resolve(
            root, raw.get("source_manifest_path", "_meta/source-manifest.yml")),
        snapshot_dir=_resolve(root, raw.get("snapshot_dir", "_meta/snapshots")),
        snapshot_slice_module=plugins.get("snapshot_slice_module"),
        citation_module=plugins.get("citation_module"),
        semantic_search_module=plugins.get("semantic_search_module"),
        issuing_body_registry=_resolve(root, plugins.get("issuing_body_registry")),
        issuing_body_registry_key=plugins.get("issuing_body_registry_key", "entries"),
        issuing_body_profiles=_resolve(root, plugins.get("issuing_body_profiles")),
        extra_schema_checks=plugins.get("extra_schema_checks", []) or [],
        mcp_server_name=mcp.get("server_name", corpus.get("id", "corpus")),
        mcp_transports=mcp.get("transports", ["stdio", "http"]),
        reverify_days=int(status.get("reverify_days", 90)),
        coverage_fail_threshold=float(provenance.get("coverage_fail_threshold", 0.70)),
        coverage_warn_threshold=float(provenance.get("coverage_warn_threshold", 0.90)),
        raw=raw,
    )
