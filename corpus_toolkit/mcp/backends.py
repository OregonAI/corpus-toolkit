"""Retrieval backends — the seam between the MCP tools and where content lives.

WHY THIS EXISTS. Until now `CorpusFramework` called `sqlite3` and `Path.read_text()`
inline, so every tool assumed markdown files on disk. That is correct for the document
archetype and fatal for anything else: an API-backed corpus (Phase 5,
`oregon-legislature`) has no content files at all, and six of the seven tools would
return an EMPTY corpus rather than an error — a silent wrong answer, not a loud one.

The alternative considered and rejected was a parallel `ApiFramework` implementing the
same method names. Two implementations of one surface drift within a release, and then
two corpora disagree about response shape — destroying the single property the whole
franchise depends on: that one client config works against every server.

CONTRACT NOTE. `FileBackend` is a PURE REFACTOR. Its method bodies are moved verbatim
from `CorpusFramework`, not rewritten — the FTS schema in particular is load-bearing in
ways that are easy to miss (`snippet(fts, 5, ...)` addresses the `body` column BY INDEX,
so reordering the virtual table silently returns the wrong text). Any behaviour change
here is a bug.

A backend owns RETRIEVAL only. Citation schemes, the authority graph, the disclaimer and
response assembly stay in `CorpusFramework`: those are corpus-shaped concerns, not
storage-shaped ones, and duplicating them per backend is how the shapes drift.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..repo import content_files, extract_fulltext, parse_frontmatter, repo_state

BIG_DOC_BYTES = 50 * 1024


@runtime_checkable
class RetrievalBackend(Protocol):
    """What a corpus must do for the MCP tools to work.

    Deliberately small: everything here is something an OData proxy can also implement.
    Anything file-specific (an FTS connection, a filesystem path) stays private to
    FileBackend and never reaches this protocol.
    """

    name: str

    def search(self, query: str, *, doc_type: str | None = None,
               issuing_body: str | None = None, limit: int = 10,
               mode: str = "hybrid") -> list[dict]:
        """Ranked hits: {id, title, citation, doc_type, issuing_body, path, snippet}."""
        ...

    def get(self, doc_id: str, *, part: str = "auto") -> dict:
        """Record metadata + body, or {"error": ...}."""
        ...

    def exists(self, doc_id: str) -> dict | None:
        """Cheap existence probe: {id, title, doc_type} or None.

        Separate from get() because resolve_citation only needs to know a document is
        real. For a file corpus that is one indexed SELECT; for a live API it is the
        difference between a key lookup and fetching a whole record."""
        ...

    def overview(self) -> dict:
        """Backend-shaped facts for corpus_overview: counts, commit/source stamp."""
        ...

    def health(self) -> dict:
        """{reachable: bool, checked_at: str|None, detail: str}.

        In the protocol because a file corpus is trivially reachable and a remote one is
        not — and 'could not check' must never be served as 'not there'."""
        ...


class FileBackend:
    """Markdown files on disk + an FTS5 cache. Historical behaviour, unchanged."""

    name = "file"

    def __init__(self, config, semantic=None):
        self.config = config
        self._semantic = semantic

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
            issuing_body_slug TEXT, citation TEXT, title TEXT, status TEXT,
            source_url TEXT, retrieved TEXT, effective_date TEXT, content_mode TEXT,
            content_exception TEXT, size INTEGER)""")
        con.execute("""CREATE VIRTUAL TABLE fts USING fts5(
            id, citation, title, tags, glance, body, tokenize='porter unicode61')""")
        con.execute("BEGIN")
        for p in content_files(self.config):
            fm, body = parse_frontmatter(p)
            glance = self._extract_section(body, "At a glance") or ""
            ft = extract_fulltext(body) or self._extract_section(body, "Key provisions") or ""
            rel = p.relative_to(self.config.root)
            con.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                fm["id"], str(rel), fm["doc_type"],
                fm.get("issuing_body", ""), self.config.scope_slug_for(rel.parts) or "",
                fm.get("citation", ""), fm["title"],
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

    # ---------- retrieval ----------

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

    def _doc_row(self, doc_id: str):
        con = self.ensure_index()
        return con.execute(
            "SELECT id, path, doc_type, citation, title, status, source_url, "
            "retrieved, effective_date, content_mode, content_exception, size "
            "FROM docs WHERE id = ?", (doc_id,)).fetchone()

    @staticmethod
    def _rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
        """Reciprocal-rank fusion of several ranked id lists."""
        score: dict[str, float] = {}
        for ranking in rankings:
            for rank, did in enumerate(ranking):
                score[did] = score.get(did, 0.0) + 1.0 / (k + rank + 1)
        return sorted(score, key=lambda d: -score[d])

    def search(self, query: str, *, doc_type: str | None = None,
               issuing_body: str | None = None, limit: int = 10,
               mode: str = "hybrid") -> list[dict]:
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

    def exists(self, doc_id: str) -> dict | None:
        con = self.ensure_index()
        r = self._doc_meta_row(con, doc_id)
        return {"id": r[0], "title": r[1], "doc_type": r[3]} if r else None

    def get(self, doc_id: str, *, part: str = "auto") -> dict:
        """Metadata + body for one document.

        Returns backend-level fields only. CorpusFramework adds `corpus`, `archetype`
        and `disclaimer`, and builds the not-found `did_you_mean` suggestions, so those
        stay identical across every backend instead of being re-derived per storage
        type."""
        r = self._doc_row(doc_id)
        if not r:
            return {"error": f"no document with id {doc_id!r}"}
        path = self.config.root / r[1]
        fm, body = parse_frontmatter(path)
        meta = {"id": r[0], "title": r[4], "citation": r[3], "doc_type": r[2],
                "status": r[5], "source_url": r[6], "retrieved": r[7],
                "effective_date": r[8] or None, "content_mode": r[9], "path": r[1],
                "relationships": {k: v for k, v in (fm.get("relationships") or {}).items() if v},
                "authoritative_source": r[6]}
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

    def overview(self) -> dict:
        con = self.ensure_index()
        by_type = dict(con.execute(
            "SELECT doc_type, COUNT(*) FROM docs GROUP BY doc_type ORDER BY 2 DESC"))
        by_mode = dict(con.execute(
            "SELECT content_mode, COUNT(*) FROM docs GROUP BY content_mode ORDER BY 2 DESC"))
        head = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=self.config.root,
                              capture_output=True, text=True).stdout.strip()
        return {"documents_by_type": by_type, "content_mode": by_mode, "commit": head}

    def health(self) -> dict:
        """A file corpus is reachable iff its index holds anything.

        Reporting an EMPTY index as unreachable is the point of this method: the old
        behaviour answered 'nothing found' for a misconfigured corpus, which a caller
        cannot distinguish from a genuine no-match."""
        try:
            con = self.ensure_index()
            n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        except Exception as e:                       # noqa: BLE001
            return {"reachable": False, "checked_at": None,
                    "detail": f"{type(e).__name__}: {e}"}
        return {"reachable": n > 0, "checked_at": None,
                "detail": f"{n} document(s) indexed" if n else
                          "index is EMPTY — content_roots may be misconfigured"}
