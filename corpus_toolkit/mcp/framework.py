#!/usr/bin/env python3
"""Query engine behind the MCP server — pure stdlib + PyYAML, no `mcp` SDK
dependency, so it can be smoke-tested without installing it. Ported from
oregon-policy-repo/src/mcp_lib.py, generalized per
docs/mcp-interface-contract.md:

  - FTS5 full-text index over content files, cached at _meta/.cache/fts.db,
    keyed on repo_state() (git HEAD + working-tree changes).
  - Graph queries over _meta/graph.json (authority chains, neighbors) — the
    toolkit only CONSUMES this file; building it (citation-mining regex) is
    corpus-specific and stays in the corpus repo.
  - Citation resolution via a pluggable scheme registry: a corpus's
    `plugins.citation_module` (see corpus_toolkit/config.py) registers its
    citation formats with `register_scheme()`; nothing Oregon-specific
    (ORS/OAR/renumbering/etc.) lives here. A scheme may declare
    `corpus="<sibling id>"`, in which case its candidates resolve against that
    sibling's compact index (corpus_toolkit/index.py + remote.py) rather than
    the local graph — an unreachable sibling degrades to `unresolved` with an
    explicit "unavailable, not absent" note, never a fabricated hit.
  - Optional issuing-body profile extension, active only when corpus.yml
    declares an `issuing_body_registry` (+ optional `issuing_body_profiles`).

Every document payload carries the non-authoritative notice + source_url +
retrieved from frontmatter — this server must never present content as the
official text."""
import inspect
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import yaml

from corpus_toolkit.config import CorpusConfig
from corpus_toolkit.plugins import load_module
from corpus_toolkit.mcp.backends import BIG_DOC_BYTES, FileBackend, RetrievalBackend
from corpus_toolkit.remote import (
    document_url as sibling_document_url, load_sibling_index, lookup as sibling_lookup,
)
from corpus_toolkit.repo import (
    content_files, extract_fulltext, parse_frontmatter, repo_state,
)

BIG_DOC_BYTES = 50_000

# ---------- citation-scheme registry (populated by a corpus's citation_module) ----------

_SCHEMES: list[tuple[str, "re.Pattern", str | None, object | None, str | None]] = []


def register_scheme(name: str, pattern: str, id_template: str | None = None, *,
                    resolver=None, corpus: str | None = None) -> None:
    """Register a citation format: `pattern` is matched against the trimmed
    citation string. Two ways to turn a match into candidate document id(s):

    - `id_template`: formatted with the match's named groups (falls back to
      positional groups) to produce ONE candidate id.
      Example: register_scheme("retention-schedule", r"Schedule\\s+(?P<num>[\\d-]+)",
                               "schedule-{num}")
    - `resolver`: a callable for cases a flat template can't express — a
      renumbering-map lookup, or a division citation that expands to several
      rule ids. Two signatures are accepted: `resolver(match)` or
      `resolver(match, nodes)` — `nodes` is the corpus's full {id: node} graph
      map, for resolvers that need corpus-wide context (e.g. "every rule
      whose id starts with this division"). Return either `candidates` (a
      list of ids) or `(candidates, note)` — `note` is surfaced on the
      response whether resolution succeeds (e.g. "was renumbered to X") or
      not (e.g. a repealed-citation explanation), overriding the generic
      unresolved message.
      Example: register_scheme("oar-rule", r"OAR\\s+(?P<num>\\d+-\\d+-\\d+)",
                               resolver=lambda m: [renumber(m["num"])])

    Exactly one of `id_template`/`resolver` should be given; `resolver` takes
    precedence if both are.

    `corpus` names the SIBLING corpus this scheme's candidate ids belong to —
    the citation formats a corpus recognizes but does not itself hold (a
    records-retention corpus recognizing `OAR 166-300-0040`, which lives in
    the rules corpus). The sibling must be declared in corpus.yml's
    `siblings:` block; resolution then goes through that sibling's compact
    index (corpus_toolkit/index.py) and hits come back carrying `corpus` and
    `url`. Default None = resolve locally, exactly as before."""
    if id_template is None and resolver is None:
        raise ValueError("register_scheme requires id_template or resolver")
    _SCHEMES.append((name, re.compile(pattern), id_template, resolver, corpus))


def clear_schemes() -> None:
    _SCHEMES.clear()


class CorpusFramework:
    def __init__(self, config: CorpusConfig):
        self.config = config
        self.disclaimer = (
            "NON-AUTHORITATIVE curated copy for AI-agent reference. Not the "
            "official text — always cite and verify against source_url.")
        self._graph_cache = None
        if config.citation_module:
            load_module(config.citation_module, config.root)
        self._semantic = (load_module(config.semantic_search_module, config.root)
                          if config.semantic_search_module else None)
        # The retrieval seam. A corpus may supply its own via plugins.retrieval_module
        # (an API archetype does); everything else keeps the historical file backend.
        # Loaded exactly like citation_module, and validated against the protocol so a
        # broken adapter fails at startup rather than on the first query.
        self.backend = self._load_backend()

    def _load_backend(self):
        """Pick the retrieval backend. A corpus opts into a different one by setting
        plugins.retrieval_module to an attr path ("src.odata_backend:ODataBackend").

        Validated against the protocol at STARTUP, deliberately: a backend missing a
        method would otherwise surface as an AttributeError on some later query, which
        for a search tool is indistinguishable from an empty corpus."""
        mod = getattr(self.config, "retrieval_module", None)
        if not mod:
            return FileBackend(self.config, self._semantic)
        from ..plugins import load_attr
        factory = load_attr(mod, self.config.root)
        backend = factory(self.config, self._semantic)
        missing = [m for m in ("search", "get", "exists", "overview", "health")
                   if not callable(getattr(backend, m, None))]
        if missing:
            raise TypeError(f"retrieval_module {mod!r} produced {type(backend).__name__}, "
                            f"which does not satisfy RetrievalBackend: missing {missing}")
        return backend

    # ---------- retrieval (delegated to the backend) ----------

    @property
    def _cache_dir(self) -> Path:
        """Corpus-level cache root. Shared: FileBackend puts its FTS db here, and
        cross-corpus sibling indices live under _cache_dir/siblings regardless of which
        backend serves this corpus — a sibling lookup is a corpus concern, not a
        storage one."""
        return self.config.root / "_meta" / ".cache"

    def ensure_index(self):
        """Back-compat shim. The FTS index is a FileBackend implementation detail now;
        callers outside the MCP tools (CLI, tests) still reach for it."""
        idx = getattr(self.backend, "ensure_index", None)
        if idx is None:
            raise AttributeError(
                f"{type(self.backend).__name__} has no FTS index — this corpus is not "
                "file-backed, so ensure_index() is not meaningful for it")
        return idx()

    def _extract_section(self, body: str, heading: str):
        return FileBackend._extract_section(self, body, heading)

    # ---------- graph ----------

    def graph(self):
        if self._graph_cache is None:
            if not self.config.graph_path.is_file():
                self._graph_cache = ({}, {})
            else:
                g = json.loads(self.config.graph_path.read_text())
                nodes = {n["id"]: n for n in g["nodes"]}
                edges = {}
                for e in g["edges"]:
                    edges.setdefault(e["from"], {}).setdefault(e["type"], []).append(e["to"])
                self._graph_cache = (nodes, edges)
        return self._graph_cache

    # ---------- tools ----------

    def search_corpus(self, query: str, doc_type: str | None = None,
                      issuing_body: str | None = None, limit: int = 10,
                      mode: str = "hybrid") -> list[dict]:
        """mode: 'hybrid' (default) fuses BM25 keyword + a registered
        semantic_search_module via RRF; 'keyword' is FTS5/BM25 only;
        'semantic' is vector-only. Hybrid/semantic silently degrade to
        keyword when no semantic_search_module is configured/available.

        Ranking and storage belong to the backend; this stays a passthrough so every
        archetype returns the same hit shape."""
        return self.backend.search(query, doc_type=doc_type, issuing_body=issuing_body,
                                   limit=limit, mode=mode)

    def get_document(self, doc_id: str, part: str = "auto") -> dict:
        """Backend supplies the record; the corpus-level envelope is added here so it is
        identical across archetypes (and so a new backend cannot forget the
        disclaimer)."""
        rec = self.backend.get(doc_id, part=part)
        if rec.get("error") and "id" not in rec:
            sug = self.search_corpus(doc_id.replace("-", " "), limit=3)
            return {**rec, "did_you_mean": [{"id": s["id"], "title": s["title"]} for s in sug]}
        return {**rec, "corpus": self.config.id, "archetype": self.config.archetype,
                "disclaimer": self.disclaimer}

    # ---------- cross-corpus resolution ----------

    def _resolve_in_sibling(self, sibling_id: str, cands: list[str]):
        """Look candidate ids up in a sibling corpus's compact index. Returns
        (hits, status); status distinguishes "the sibling says it has no such
        document" (available=True, no hits) from "we could not consult the
        sibling at all" (available=False) — opposite answers for a caller."""
        sib = self.config.sibling(sibling_id)
        if sib is None:
            return [], {"available": False, "stale": False,
                        "reason": f"no sibling {sibling_id!r} declared in corpus.yml's "
                                  f"`siblings:` block"}
        index = load_sibling_index(sib, self._cache_dir / "siblings")
        if index is None:
            return [], {"available": False, "stale": False,
                        "reason": "index could not be fetched and no cached copy exists"}
        hits = []
        for i in cands:
            row = sibling_lookup(index, i)
            if row is None:
                continue
            hit = {"id": i, "title": row["title"], "doc_type": row["doc_type"],
                   "corpus": sibling_id}
            url = sibling_document_url(sib, row["path"])
            if url:
                hit["url"] = url
            hits.append(hit)
        return hits, {"available": True, "stale": bool(index.get("_stale")),
                      "reason": ""}

    def resolve_citation(self, citation: str) -> dict:
        nodes, _ = self.graph()
        c = citation.strip()
        matched_scheme = None
        matched_corpus = None
        cands: list[str] = []
        resolver_note = None
        for name, pattern, id_template, resolver, scheme_corpus in _SCHEMES:
            m = pattern.search(c)
            if not m:
                continue
            matched_scheme = name
            matched_corpus = scheme_corpus
            if resolver is not None:
                try:
                    nparams = len(inspect.signature(resolver).parameters)
                except (TypeError, ValueError):
                    nparams = 1
                result = resolver(m, nodes) if nparams >= 2 else resolver(m)
                if isinstance(result, tuple):
                    cands, resolver_note = list(result[0] or []), result[1]
                else:
                    cands = list(result or [])
            else:
                try:
                    cid = id_template.format(**m.groupdict()) if m.groupdict() \
                        else id_template.format(*m.groups())
                    cands = [cid]
                except (IndexError, KeyError):
                    cands = []
            break
        hits = [{"id": i, "title": nodes[i]["title"], "doc_type": nodes[i]["doc_type"]}
                for i in cands if i in nodes]
        out = {"citation": citation, "matches": hits,
               "corpus": self.config.id, "archetype": self.config.archetype}

        sibling_status = None
        if matched_corpus and cands:
            sibling_hits, sibling_status = self._resolve_in_sibling(matched_corpus, cands)
            if sibling_hits:
                hits = hits + sibling_hits
                out["matches"] = hits
                out["resolved_via"] = f"sibling:{matched_corpus}"
            if sibling_status.get("stale"):
                out["sibling_index_stale"] = True

        if hits and resolver_note:
            out["note"] = resolver_note
        if hits and sibling_status and sibling_status.get("stale"):
            out.setdefault("note", (
                f"resolved from a STALE cached copy of sibling corpus "
                f"'{matched_corpus}'s index — it could not be refreshed"))
        if not hits:
            out["unresolved"] = True
            if sibling_status and not sibling_status.get("available"):
                # "we couldn't look" is a completely different answer from "it
                # isn't there" — a caller must never conflate them.
                out["sibling_unavailable"] = matched_corpus
                out["note"] = (
                    f"citation belongs to sibling corpus '{matched_corpus}', but its "
                    f"index could not be loaded ({sibling_status['reason']}) — this is "
                    f"NOT evidence the document is absent; retry, or check the sibling "
                    f"corpus directly")
            elif sibling_status:
                out["note"] = resolver_note or (
                    f"scheme '{matched_scheme}' matched and sibling corpus "
                    f"'{matched_corpus}'s index loaded, but it holds no document with "
                    f"id(s) {', '.join(cands)}")
            else:
                out["note"] = resolver_note or (
                    f"scheme '{matched_scheme}' matched but no such document exists"
                    if matched_scheme else
                    "no citation scheme recognized this format — try search_corpus")
            out["schemes_attempted"] = [s[0] for s in _SCHEMES]
            out["search_fallback"] = [{"id": s["id"], "title": s["title"]}
                                      for s in self.search_corpus(c, limit=3)]
        return out

    def authority_chain(self, doc_id: str, direction: str = "both", depth: int = 3) -> dict:
        nodes, edges = self.graph()
        if doc_id not in nodes:
            return {"error": f"no document with id {doc_id!r}"}
        depth = max(1, min(int(depth), 6))

        def walk(start, key):
            seen, levels, frontier = {start}, [], [start]
            for _ in range(depth):
                nxt = []
                for i in frontier:
                    for t in edges.get(i, {}).get(key, []):
                        if t not in seen:
                            seen.add(t)
                            nxt.append({"id": t, "title": nodes[t]["title"],
                                        "doc_type": nodes[t]["doc_type"], "via": i})
                if not nxt:
                    break
                levels.append(nxt)
                frontier = [n["id"] for n in nxt]
            return levels

        out = {"id": doc_id, "title": nodes[doc_id]["title"],
               "doc_type": nodes[doc_id]["doc_type"]}
        if direction in ("up", "both"):
            out["up_implements"] = walk(doc_id, "implements")
        if direction in ("down", "both"):
            out["down_implemented_by"] = walk(doc_id, "implemented_by")
        return out

    def graph_neighbors(self, doc_id: str) -> dict:
        nodes, edges = self.graph()
        if doc_id not in nodes:
            return {"error": f"no document with id {doc_id!r}"}
        out = {"id": doc_id, "title": nodes[doc_id]["title"]}
        for k, targets in edges.get(doc_id, {}).items():
            out[k] = [{"id": t, "title": nodes[t]["title"], "doc_type": nodes[t]["doc_type"]}
                      for t in targets]
        return out

    def corpus_overview(self) -> dict:
        return {
            "corpus": self.config.id,
            "archetype": self.config.archetype,
            "jurisdiction": self.config.jurisdiction,
            "disclaimer": self.disclaimer,
            **self.backend.overview(),
            "graph_edges": sum(len(v) for d in self.graph()[1].values() for v in d.values()),
            "contract_version": self.config.contract_version,
        }

    # ---------- document-corpus extension: issuing-body profile ----------

    def issuing_body_profile(self, slug_or_query: str) -> dict:
        if not self.config.issuing_body_registry:
            return {"error": "this corpus has no issuing-body registry configured"}
        registry = yaml.safe_load(self.config.issuing_body_registry.read_text()) or {}
        entries = {e["slug"]: e for e in registry.get(self.config.issuing_body_registry_key, [])}
        curated = {}
        if self.config.issuing_body_profiles and self.config.issuing_body_profiles.is_file():
            curated = (yaml.safe_load(self.config.issuing_body_profiles.read_text()) or {}).get(
                "profiles", {})

        slug = slug_or_query
        if slug not in entries:
            q = slug_or_query.lower()
            hits = [s for s, o in entries.items() if q in o.get("name", "").lower()]
            if len(hits) != 1:
                return {"error": f"no unique issuing body match for {slug_or_query!r}",
                        "candidates": [{"slug": s, "name": entries[s].get("name")} for s in hits[:8]]}
            slug = hits[0]

        con = self.ensure_index()
        docs = con.execute(
            "SELECT content_mode, COUNT(*) FROM docs WHERE issuing_body_slug = ? "
            "GROUP BY content_mode", (slug,)).fetchall()
        return {
            "slug": slug,
            "registry": entries[slug],
            "curated": curated.get(slug, {}),
            "in_repo": {mode: n for mode, n in docs} or "no documents ingested for this issuing body yet",
            "disclaimer": self.disclaimer,
        }
