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

_SCHEMES: list[tuple[str, "re.Pattern", str]] = []


def register_scheme(name: str, pattern: str, id_template: str) -> None:
    """Register a citation format: `pattern` is matched against the trimmed
    citation string; `id_template` is formatted with the match's named groups
    (falls back to positional groups) to produce a candidate document id.

    Example: register_scheme("retention-schedule", r"Schedule\\s+(?P<num>[\\d-]+)",
                             "schedule-{num}")"""
    _SCHEMES.append((name, re.compile(pattern), id_template))


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

    def search_corpus(self, query: str, doc_type: str | None = None,
                      issuing_body: str | None = None, limit: int = 10) -> list[dict]:
        con = self.ensure_index()
        n = max(1, min(int(limit), 40))
        rows, order = self._fts_rows(con, query, doc_type, issuing_body, n)
        out = []
        for i in order[:n]:
            r = rows[i]
            out.append({"id": r[0], "title": r[2], "citation": r[3], "doc_type": r[4],
                        "issuing_body": r[5], "path": r[6], "snippet": r[1][:400]})
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
        for name, pattern, id_template in _SCHEMES:
            m = pattern.search(c)
            if not m:
                continue
            try:
                cid = id_template.format(**m.groupdict()) if m.groupdict() \
                    else id_template.format(*m.groups())
            except (IndexError, KeyError):
                continue
            cands = [cid]
            matched_scheme = name
            break
        hits = [{"id": i, "title": nodes[i]["title"], "doc_type": nodes[i]["doc_type"]}
                for i in cands if i in nodes]
        out = {"citation": citation, "matches": hits,
               "corpus": self.config.id, "archetype": self.config.archetype}
        if not hits:
            out["unresolved"] = True
            out["note"] = (f"scheme '{matched_scheme}' matched but no such document exists"
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
        entries = {e["slug"]: e for e in registry.get("entries", [])}
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
