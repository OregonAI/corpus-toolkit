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
    (ORS/OAR/renumbering/etc.) lives here.
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
from corpus_toolkit.repo import (
    content_files, extract_fulltext, parse_frontmatter, repo_state,
)

BIG_DOC_BYTES = 50_000

# ---------- citation-scheme registry (populated by a corpus's citation_module) ----------

_SCHEMES: list[tuple[str, "re.Pattern", str | None, object | None]] = []


def register_scheme(name: str, pattern: str, id_template: str | None = None, *,
                    resolver=None) -> None:
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
    precedence if both are."""
    if id_template is None and resolver is None:
        raise ValueError("register_scheme requires id_template or resolver")
    _SCHEMES.append((name, re.compile(pattern), id_template, resolver))


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

    # ---------- FTS index ----------

    @property
    def _cache_dir(self) -> Path:
        return self.config.root / "_meta" / ".cache"

    @property
    def _db_path(self) -> Path:
        return self._cache_dir / "fts.db"

    def _extract_section(self, body: str, heading: str):
        m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
        return m.group(1).strip() if m else None

    def ensure_index(self) -> sqlite3.Connection:
        """Open the FTS cache, rebuilding it if the corpus has changed. Builds into a
        fresh temp file and atomically renames it into place, so a concurrent reader
        (e.g. a long-lived MCP server racing a CLI rebuild) never sees a half-written
        file."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        state = repo_state(self.config.root)
        if self._db_path.exists():
            con = sqlite3.connect(self._db_path)
            try:
                row = con.execute("SELECT v FROM meta WHERE k='state'").fetchone()
                if row and row[0] == state:
                    return con
            except sqlite3.OperationalError:
                pass
            con.close()

        tmp_path = self._db_path.with_suffix(".db.tmp")
        tmp_path.unlink(missing_ok=True)
        con = sqlite3.connect(tmp_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("""CREATE TABLE docs (
            id TEXT PRIMARY KEY, path TEXT, doc_type TEXT, issuing_body TEXT,
            citation TEXT, title TEXT, status TEXT, source_url TEXT, retrieved TEXT,
            effective_date TEXT, content_mode TEXT, content_exception TEXT, size INTEGER)""")
        con.execute("""CREATE VIRTUAL TABLE fts USING fts5(
            id, citation, title, tags, glance, body, tokenize='porter unicode61')""")
        con.execute("BEGIN")
        for p in content_files(self.config):
            fm, body = parse_frontmatter(p)
            glance = self._extract_section(body, "At a glance") or ""
            ft = extract_fulltext(body) or self._extract_section(body, "Key provisions") or ""
            con.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                fm["id"], str(p.relative_to(self.config.root)), fm["doc_type"],
                fm.get("issuing_body", ""), fm.get("citation", ""), fm["title"],
                fm.get("status", ""), fm.get("source_url", ""), str(fm.get("retrieved", "")),
                str(fm.get("effective_date") or ""), fm.get("content_mode", ""),
                fm.get("content_exception") or "", p.stat().st_size))
            con.execute("INSERT INTO fts VALUES (?,?,?,?,?,?)", (
                fm["id"], fm.get("citation", ""), fm["title"],
                " ".join(fm.get("tags") or []), glance, ft))
        con.execute("INSERT INTO meta VALUES ('state', ?)", (state,))
        con.commit()
        con.close()
        con = sqlite3.connect(tmp_path)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.close()
        tmp_path.replace(self._db_path)
        for stray in (self._db_path.with_name(self._db_path.name + "-wal"),
                      self._db_path.with_name(self._db_path.name + "-shm")):
            stray.unlink(missing_ok=True)
        return sqlite3.connect(self._db_path)

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

    def _fts_rows(self, con, query, doc_type, issuing_body, limit):
        terms = re.findall(r"[\w.\-]+", query)
        if not terms:
            return {}, []
        match = " ".join(f'"{t}"' for t in terms)
        sql = ("SELECT f.id, snippet(fts, 5, '[', ']', ' … ', 24), d.title, d.citation, "
               "d.doc_type, d.issuing_body, d.path, bm25(fts) FROM fts f "
               "JOIN docs d ON d.id = f.id WHERE fts MATCH ?")
        sql_args = [match]
        if doc_type:
            sql += " AND d.doc_type = ?"; sql_args.append(doc_type)
        if issuing_body:
            sql += " AND d.issuing_body = ?"; sql_args.append(issuing_body)
        sql += " ORDER BY bm25(fts) LIMIT ?"
        sql_args.append(max(1, min(int(limit), 40)))
        rows = {r[0]: r for r in con.execute(sql, sql_args).fetchall()}
        order = sorted(rows, key=lambda i: rows[i][7])
        return rows, order

    def _semantic_available(self) -> bool:
        if self._semantic is None:
            return False
        avail = getattr(self._semantic, "available", None)
        return bool(avail()) if avail else True

    def _doc_meta_row(self, con, doc_id):
        return con.execute(
            "SELECT id, title, citation, doc_type, issuing_body, path "
            "FROM docs WHERE id = ?", (doc_id,)).fetchone()

    @staticmethod
    def _rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
        """Reciprocal-rank fusion of several ranked id lists."""
        score: dict[str, float] = {}
        for ranking in rankings:
            for rank, did in enumerate(ranking):
                score[did] = score.get(did, 0.0) + 1.0 / (k + rank + 1)
        return sorted(score, key=lambda d: -score[d])

    def search_corpus(self, query: str, doc_type: str | None = None,
                      issuing_body: str | None = None, limit: int = 10,
                      mode: str = "hybrid") -> list[dict]:
        """mode: 'hybrid' (default) fuses BM25 keyword + a registered
        semantic_search_module via RRF; 'keyword' is FTS5/BM25 only;
        'semantic' is vector-only. Hybrid/semantic silently degrade to
        keyword when no semantic_search_module is configured/available."""
        con = self.ensure_index()
        n = max(1, min(int(limit), 40))
        use_sem = mode in ("hybrid", "semantic") and self._semantic_available()
        pool = max(n * 4, 40) if use_sem else n
        rows, kw_order = self._fts_rows(con, query, doc_type, issuing_body, pool)

        if not use_sem:
            final = kw_order[:n]
        else:
            sem_order = list(self._semantic.rank(query, pool) or [])
            if doc_type or issuing_body:
                keep = []
                for d in sem_order:
                    mr = self._doc_meta_row(con, d)
                    if mr and (not doc_type or mr[3] == doc_type) and \
                            (not issuing_body or mr[4] == issuing_body):
                        keep.append(d)
                sem_order = keep
            final = sem_order[:n] if mode == "semantic" else self._rrf([kw_order, sem_order])[:n]

        out = []
        for i in final:
            r = rows.get(i)
            if r:
                out.append({"id": r[0], "title": r[2], "citation": r[3], "doc_type": r[4],
                            "issuing_body": r[5], "path": r[6], "snippet": r[1][:400]})
            else:
                mr = self._doc_meta_row(con, i)
                if mr:
                    out.append({"id": mr[0], "title": mr[1], "citation": mr[2],
                                "doc_type": mr[3], "issuing_body": mr[4], "path": mr[5],
                                "snippet": "(semantic match — no keyword overlap)"})
        return out

    def _doc_row(self, doc_id: str):
        con = self.ensure_index()
        return con.execute(
            "SELECT id, path, doc_type, citation, title, status, source_url, "
            "retrieved, effective_date, content_mode, content_exception, size "
            "FROM docs WHERE id = ?", (doc_id,)).fetchone()

    def get_document(self, doc_id: str, part: str = "auto") -> dict:
        r = self._doc_row(doc_id)
        if not r:
            sug = self.search_corpus(doc_id.replace("-", " "), limit=3)
            return {"error": f"no document with id {doc_id!r}",
                    "did_you_mean": [{"id": s["id"], "title": s["title"]} for s in sug]}
        path = self.config.root / r[1]
        fm, body = parse_frontmatter(path)
        meta = {"id": r[0], "title": r[4], "citation": r[3], "doc_type": r[2],
                "status": r[5], "source_url": r[6], "retrieved": r[7],
                "effective_date": r[8] or None, "content_mode": r[9], "path": r[1],
                "relationships": {k: v for k, v in (fm.get("relationships") or {}).items() if v},
                "corpus": self.config.id, "archetype": self.config.archetype,
                "authoritative_source": r[6], "disclaimer": self.disclaimer}
        if r[10]:
            meta["content_exception"] = r[10]
        headings = re.findall(r"^## (.+)$", body, re.M)
        if part == "auto" and r[11] > BIG_DOC_BYTES:
            glance = self._extract_section(body, "At a glance")
            return {**meta, "note": (f"document body is {r[11] // 1024} KB — pass "
                                     "part='full text' (or another heading below) to page in "
                                     "the content you need"),
                    "at_a_glance": glance, "sections": headings}
        if part in ("auto", "full"):
            return {**meta, "body": body}
        sec = self._extract_section(body, part) or next(
            (self._extract_section(body, h) for h in headings if h.lower() == part.lower()), None)
        if sec is None:
            return {**meta, "error": f"no section {part!r}", "sections": headings}
        return {**meta, "section": part, "body": sec}

    def resolve_citation(self, citation: str) -> dict:
        nodes, _ = self.graph()
        c = citation.strip()
        matched_scheme = None
        cands: list[str] = []
        resolver_note = None
        for name, pattern, id_template, resolver in _SCHEMES:
            m = pattern.search(c)
            if not m:
                continue
            matched_scheme = name
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
        if hits and resolver_note:
            out["note"] = resolver_note
        if not hits:
            out["unresolved"] = True
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
        con = self.ensure_index()
        by_type = dict(con.execute(
            "SELECT doc_type, COUNT(*) FROM docs GROUP BY doc_type ORDER BY 2 DESC"))
        by_mode = dict(con.execute(
            "SELECT content_mode, COUNT(*) FROM docs GROUP BY content_mode ORDER BY 2 DESC"))
        head = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=self.config.root,
                              capture_output=True, text=True).stdout.strip()
        return {
            "corpus": self.config.id,
            "archetype": self.config.archetype,
            "jurisdiction": self.config.jurisdiction,
            "disclaimer": self.disclaimer,
            "commit": head,
            "documents_by_type": by_type,
            "content_mode": by_mode,
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
            "SELECT content_mode, COUNT(*) FROM docs WHERE issuing_body = ? "
            "GROUP BY content_mode", (slug,)).fetchall()
        return {
            "slug": slug,
            "registry": entries[slug],
            "curated": curated.get(slug, {}),
            "in_repo": {mode: n for mode, n in docs} or "no documents ingested for this issuing body yet",
            "disclaimer": self.disclaimer,
        }
