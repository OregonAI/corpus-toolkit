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

CONTRACT NOTE. `FileBackend` began as a PURE REFACTOR: its method bodies were moved
verbatim from `CorpusFramework`, not rewritten. That is still the rule for retrieval
semantics — ranking in particular. The FTS schema is load-bearing in ways that are easy
to miss, so it changes only deliberately and with the reason written down (see
CONTENTLESS FTS below).

A backend owns RETRIEVAL only. Citation schemes, the authority graph, the disclaimer and
response assembly stay in `CorpusFramework`: those are corpus-shaped concerns, not
storage-shaped ones, and duplicating them per backend is how the shapes drift.

CONTENTLESS FTS (corpus-toolkit#17). The `fts` table is declared `content=''`, so FTS5
tokenizes each document but does NOT keep a copy of the text. Measured on
executive-regulatory-frameworks: that copy was `fts_content` = 304 MiB of a 437 MiB
index, duplicating text that already exists in the working tree.

Three consequences, all of them silent if you do not know to look for them — the reason
this note is long:

  * READING AN FTS COLUMN RETURNS NULL. `SELECT f.id FROM fts f` yields None, so the old
    `JOIN docs d ON d.id = f.id` matched NOTHING rather than failing. Every join is now on
    `rowid`, which is why the build inserts into `fts` with the rowid `docs` just assigned
    instead of letting FTS5 pick its own.
  * `snippet()` RETURNS NULL, not an error. Excerpts are therefore built in Python from
    the document on disk — see `make_excerpt` — and only for the rows actually returned,
    which is strictly less work than the old query did for the whole candidate pool.
  * `columnsize=0` WOULD save a further 1.1 MiB and is NOT used. It requires an explicit
    rowid (fine, we now have one) but it also drops the per-column token counts that
    `bm25()` normalizes by, which CHANGES RANKING. Verified on sqlite 3.45.1: identical
    corpus, bm25 went from -1.633e-06 to -2.34e-06. 1.1 MiB is not worth moving results.

`bm25()` itself is unaffected by `content=''` — verified byte-identical scores — so
ranking is preserved. Only excerpt TEXT changes.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..repo import content_files, extract_fulltext, parse_frontmatter, repo_state

# THE one definition. `framework` re-exports it by importing this name; nothing else may
# redefine it (corpus-toolkit#52 — it was defined twice, 1,200 bytes apart, for months).
#
# Corpora that need to agree with the server about what counts as "big" -- to decide which
# documents get `### ` anchors, say -- should import THIS rather than re-declare a literal:
#     from corpus_toolkit.mcp.backends import BIG_DOC_BYTES
BIG_DOC_BYTES = 50 * 1024

# Bumped whenever the shape of the cache db changes. Checked ALONGSIDE the content state
# key, because the two answer different questions: the state key says "is this index built
# from the current corpus", and it is completely blind to "was this index built by code that
# agrees with me about the schema". Without this, upgrading the toolkit under an existing
# cache leaves a valid-looking index whose `docs` table lacks a column the new queries
# select -- an OperationalError on the first request, on every deployed server at once.
SCHEMA_VERSION = 2

# What FTS5's own tokenizer would treat as a word, near enough for locating matches. Kept
# identical to the pattern the query side uses so a term that can MATCH can also be found
# in the text for highlighting.
_WORD = re.compile(r"[\w.\-]+")

# Column offsets into the tuples _fts_rows() returns. Named because the previous positional
# reads (r[1] for the snippet, r[7] for bm25) had to be found and re-counted by hand every
# time the SELECT changed -- and one of them, `snippet(fts, 5, ...)`, addressed a column by
# index too, which the module docstring used to carry a standing warning about.
#
# _doc_meta_row() selects the SAME six columns in the same order, so KW_ID..KW_PATH address
# it as well. It used to be read as mr[0], mr[1], mr[3] ... in the very branch below where
# the fts row beside it was already named -- the hazard these constants exist to remove,
# applied to one of the two readers.
KW_ID, KW_TITLE, KW_CITATION, KW_DOCTYPE, KW_ISSUER, KW_PATH, KW_BM25 = range(7)

# Column offsets into the tuple _doc_row() returns -- a DIFFERENT and wider projection than
# the one above, in a different order, and the only one `get()` reads. Twelve positional
# indices spread over sixty lines, with r[6] appearing twice under two different meanings
# (source_url, and authoritative_source derived from it) and r[11] deciding the big-document
# branch. Nothing that reads like `r[11] > BIG_DOC_BYTES` should have to be counted out on
# fingers to be reviewed.
(DOC_ID, DOC_PATH, DOC_DOCTYPE, DOC_CITATION, DOC_TITLE, DOC_STATUS, DOC_SOURCE_URL,
 DOC_RETRIEVED, DOC_EFFECTIVE, DOC_CONTENT_MODE, DOC_EXCEPTION, DOC_SIZE) = range(12)


def _stem_match(token: str, term: str) -> bool:
    """Would FTS5's porter tokenizer plausibly have matched these?

    APPROXIMATE, and only ever used to decide what to put brackets around -- never to
    decide what is a hit. FTS5 already decided that; this function's worst failure is an
    excerpt that fails to bracket a word, or brackets one word too many.

    Reimplementing porter here to be exact was the alternative. It is ~200 lines to make
    the highlighting in an excerpt marginally better, and it would then have to track
    whatever tokenizer a corpus configured. Not worth it: accept an approximation and say
    so.

    Known and accepted: it is LOOSE on long words. "information"/"informant" share seven
    of nine characters and are treated as a match though porter keeps them distinct. The
    result is one extra pair of brackets in an excerpt. Tightening the tolerance would
    start missing real matches like "governed"/"governing", which is the worse trade.
    """
    a, b = token.lower(), term.lower()
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 4:                       # short words: exact or nothing, else "cat" hits "car"
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    # Agree on everything but a suffix or two -- "governing"/"governs" share 6 of 7.
    return common >= 4 and common >= n - 2


def extract_section(body: str, heading: str):
    """The text under a `## <heading>`, up to the next `## `, or None.

    A MODULE FUNCTION, not a method, because it reads nothing off an instance. It was a
    FileBackend method, and `CorpusFramework._extract_section` called it as
    `FileBackend._extract_section(self, body, heading)` — passing a CorpusFramework as
    `self` to an unbound method of an unrelated class. That works only for exactly as long
    as the body ignores `self`, so the first line added here that touches `self.config`
    would break a caller in another module with no static or runtime signal beforehand.
    `make_excerpt` beside it was already a module function for the same reason.
    """
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else None


def make_excerpt(text: str, terms: list[str], *, width: int = 24,
                 open_mark: str = "[", close_mark: str = "]",
                 ellipsis: str = " … ") -> str:
    """A search excerpt: the `width`-token window of `text` densest in `terms`.

    Replaces `snippet(fts, 5, '[', ']', ' … ', 24)`, which returns NULL on a contentless
    table. Deliberately NOT byte-identical to it -- SQLite's exact windowing was never the
    contract, and asserting it would freeze an implementation detail of one tokenizer.

    Falls back to the head of the text when nothing matches, which happens legitimately:
    a document can be a hit on its title, citation or tags columns and contain no query
    term in its body at all.
    """
    if not text:
        return ""
    toks = list(_WORD.finditer(text))
    if not toks:
        return text[:200].strip()
    # `_WORD` accepts '.' and '-' so citations tokenize whole ("192.355", "137-090-0000").
    # The cost is that sentence-final punctuation rides along, which both inflates the
    # length comparison in _stem_match ("governed." vs "governing" fails where "governed"
    # passes) and would end up inside the brackets. Strip it once, use the core for both.
    cores = [t.group(0).rstrip(".-") or t.group(0) for t in toks]
    hit = [any(_stem_match(c, q) for q in terms) for c in cores]

    start = 0
    if any(hit):
        # Densest window, earliest on a tie. A rolling count rather than re-summing each
        # window: bodies here run to thousands of tokens.
        run = sum(hit[:width])
        best = run
        for i in range(1, max(1, len(toks) - width + 1)):
            run += hit[i + width - 1] if i + width - 1 < len(toks) else 0
            run -= hit[i - 1]
            if run > best:
                best, start = run, i
    end = min(len(toks), start + width)

    # Slice from the ORIGINAL text between token spans, so punctuation and spacing survive
    # instead of being rebuilt from a word list.
    out, cur = [], toks[start].start()
    for j in range(start, end):
        t = toks[j]
        word = t.group(0)
        out.append(text[cur:t.start()])
        if hit[j]:
            trail = word[len(cores[j]):]
            out.append(f"{open_mark}{cores[j]}{close_mark}{trail}")
        else:
            out.append(word)
        cur = t.end()

    body = " ".join("".join(out).split())
    return (("" if start == 0 else ellipsis) + body
            + ("" if end >= len(toks) else ellipsis))


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

    # ---------- optional ----------

    def holdings_for(self, slug: str) -> dict:
        """{content_mode: count} for one issuing-body slug — what this corpus holds for it.

        OPTIONAL, and the only optional member of this protocol; see
        REQUIRED_BACKEND_METHODS. A backend that cannot answer it simply omits it, and
        `issuing_body_profile` is not registered for that corpus.

        It exists so the question stops being asked BEHIND the seam. `issuing_body_profile`
        used to reach through `ensure_index()` and run raw SQL against `docs`, which is
        FileBackend's private storage — so the tool was unavailable to any other backend at
        any price, and three separate pieces of code existed to compensate: a shim on
        CorpusFramework, a `hasattr(backend, "ensure_index")` gate in server.py, and a
        stderr warning for the tool that gate silently dropped (corpus-toolkit#75).
        Registration is still gated, but now on a capability a backend can CHOOSE to have.
        """
        ...


# What `CorpusFramework._load_backend` requires of a backend at STARTUP. Restated here
# rather than read off the Protocol because Python exposes no portable way to enumerate a
# Protocol's members across 3.10-3.13 (`__protocol_attrs__` is 3.12+, `_get_protocol_attrs`
# is private). KEEP IT BESIDE THE PROTOCOL so the two are edited together — it used to be a
# literal tuple inside framework.py, one module away from the definition it restates.
#
# `holdings_for` is deliberately NOT here: adding a required method would break every
# corpus-supplied backend already in service on the next pin bump, for a tool most of them
# do not serve. It is a capability, checked where it is used.
REQUIRED_BACKEND_METHODS = ("search", "get", "exists", "overview", "health")


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

    # Kept as a name on the class because corpora and tests reach for it; the
    # implementation is the module function, which is what everything should call.
    _extract_section = staticmethod(extract_section)

    @staticmethod
    def _subheadings(body: str) -> list[str]:
        """`### ` headings — the sub-document units big instruments carry (2 CFR 200's
        199 sections; the anchored federal statutes). Serving them is what makes a
        900 KB document navigable instead of a glance-or-everything binary."""
        return re.findall(r"^### (.+)$", body, re.M)

    def _extract_subsection(self, body: str, part: str) -> tuple[str | None, str | None]:
        """(section_text, matched_heading) for a `###` heading; prefix-matched
        case-insensitively so part='SEC. 188.' finds '### SEC. 188. NONDISCRIMINATION.'.
        Ambiguity is an answer, not a guess: multiple prefix matches return None and
        the caller lists them. Span runs to the next heading of the same or higher
        level — the slicing-plugin rule."""
        subs = self._subheadings(body)
        exact = [h for h in subs if h.lower() == part.lower()]
        pref = exact or [h for h in subs if h.lower().startswith(part.lower())]
        if len(pref) != 1:
            return None, None
        h = pref[0]
        m = re.search(rf"^### {re.escape(h)}\s*$(.*?)(?=^### |^## |\Z)",
                      body, re.M | re.S)
        return (m.group(1).strip() if m else None), h

    def _chunk_part(self, path, fm_id: str, part: str):
        """part='chunk:N' — the Nth embeddable chunk of this document, recomputed by
        the SAME deterministic chunker the semantic index was built with (no offsets
        stored anywhere, so nothing can drift; meta.json's params are the build's).
        This is the retrieval half of search's chunk hits: search names the ordinal,
        this fetches it."""
        m = re.match(r"chunk:(\d+)$", part)
        if not m:
            return None
        want = int(m.group(1))
        from corpus_toolkit.semantic.build import iter_chunks
        bh = (self.config.raw.get("plugins") or {}).get("semantic_body_headings")
        for doc_id, heading, ordinal, _title, text in iter_chunks([path], bh):
            if ordinal == want:
                return {"section": f"chunk:{want}", "chunk_heading": heading,
                        "body": text}
        return {"error": f"no chunk {want} for {fm_id!r} — ordinals are 0-based and "
                         f"per-document; search hits carry the right one"}

    def index_status(self, state: str | None = None) -> tuple[bool, str]:
        """Would ensure_index() reuse the cache as-is? -> (current, reason).

        `state` lets a caller that has already computed the content key pass it in.
        repo_state() shells out to git twice and costs ~114 ms on a 75k-file corpus, and
        this runs on EVERY tool call via ensure_index -- computing it a second time here
        would double that for no new information.

        Exists so a deploy script can ask this question WITHOUT reimplementing it. The
        answer depends on two independent keys (see ensure_index), and platform-deploy was
        comparing only one of them -- which meant a toolkit upgrade looked "current" to the
        deployer while the server would rebuild under live traffic on the first request.
        That rebuild is unlocked and uses a fixed temp filename, so a concurrent warm and
        a live rebuild collide on it (`disk I/O error`). One implementation, one answer.

        Never raises: every failure is a reason to rebuild, and a checker that throws is a
        checker whose caller learns nothing.
        """
        if not self._db_path.exists():
            return False, "no index on disk"
        try:
            live = state if state is not None else repo_state(self.config.root)
            if live.startswith(":"):
                # repo_state() swallows git failures and returns sha256(b"")[:16] with an
                # empty sha. Indistinguishable from a real key to everything downstream,
                # so name it here rather than let it be compared as if it were valid.
                return False, f"git is not working: key would be the constant {live}"
            con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            try:
                got = dict(con.execute(
                    "SELECT k, v FROM meta WHERE k IN ('state','schema')"))
            finally:
                con.close()
        except Exception as e:                       # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        if got.get("schema") != str(SCHEMA_VERSION):
            return False, (f"built by schema {got.get('schema') or 'unknown'}, "
                           f"this toolkit is {SCHEMA_VERSION}")
        if got.get("state") != live:
            return False, f"content changed (stored {got.get('state')!r} != live {live!r})"
        return True, "current"

    def ensure_index(self) -> sqlite3.Connection:
        """Open the FTS cache, rebuilding it if the corpus has changed. Builds into a
        fresh temp file and atomically renames it into place, so a concurrent reader
        (e.g. a long-lived MCP server racing a CLI rebuild) never sees a half-written
        file."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        state = repo_state(self.config.root)
        # BOTH the content key and the schema version must match; index_status() owns that
        # rule so the deploy tooling can ask the same question and get the same answer.
        if self.index_status(state)[0]:
            return sqlite3.connect(self._db_path)

        tmp_path = self._db_path.with_suffix(".db.tmp")
        tmp_path.unlink(missing_ok=True)
        con = sqlite3.connect(tmp_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        # body_chars: length of the text that went into fts.body. Exists because the
        # health() check for "indexed but unsearchable" documents used to read `f.body = ''`
        # off the FTS table, and on a contentless table that column reads NULL -- so the
        # comparison is never true and the check silently stops finding anything. Storing
        # the length here keeps the check working and makes it a plain integer scan.
        con.execute("""CREATE TABLE docs (
            id TEXT PRIMARY KEY, path TEXT, doc_type TEXT, issuing_body TEXT,
            issuing_body_slug TEXT, citation TEXT, title TEXT, status TEXT,
            source_url TEXT, retrieved TEXT, effective_date TEXT, content_mode TEXT,
            content_exception TEXT, size INTEGER, body_chars INTEGER)""")
        # content='': tokenize but do not store the text. See CONTENTLESS FTS in the module
        # docstring -- in particular, columnsize=0 is omitted deliberately.
        con.execute("""CREATE VIRTUAL TABLE fts USING fts5(
            id, citation, title, tags, glance, body,
            tokenize='porter unicode61', content='')""")
        con.execute("BEGIN")
        for p in content_files(self.config):
            fm, body = parse_frontmatter(p)
            glance = self._extract_section(body, "At a glance") or ""
            ft = self._searchable_body(body, fm.get("doc_type", ""))
            rel = p.relative_to(self.config.root)
            cur = con.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                fm["id"], str(rel), fm["doc_type"],
                fm.get("issuing_body", ""), self.config.scope_slug_for(rel.parts) or "",
                fm.get("citation", ""), fm["title"],
                fm.get("status", ""), fm.get("source_url", ""), str(fm.get("retrieved", "")),
                str(fm.get("effective_date") or ""), fm.get("content_mode", ""),
                fm.get("content_exception") or "", p.stat().st_size, len(ft)))
            # EXPLICIT rowid, matching the row just written to `docs`. Every query joins
            # these two tables on rowid, because a contentless fts cannot be read back to
            # join on `id`. Letting FTS5 assign its own would happen to line up today and
            # break the first time an insert is skipped or reordered.
            con.execute("INSERT INTO fts(rowid, id, citation, title, tags, glance, body) "
                        "VALUES (?,?,?,?,?,?,?)", (
                            cur.lastrowid, fm["id"], fm.get("citation", ""), fm["title"],
                            " ".join(fm.get("tags") or []), glance, ft))
        con.execute("INSERT INTO meta VALUES ('state', ?)", (state,))
        con.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
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

    def _searchable_body(self, body: str, doc_type: str) -> str:
        """Text that feeds the FTS `body` column.

        A doc_type listed in config.index_headings gets exactly the sections named there,
        concatenated in order. Anything NOT listed keeps the historical rule verbatim --
        '## Full text', else '## Key provisions', first match wins -- so a corpus that
        does not set the key indexes byte-identically to before this existed.

        Why per-doc_type rather than 'index every section': mirrored records and entity
        docs need different sections indexed than statutes do, and widening the rule for
        everyone would silently change what an existing corpus matches on.
        """
        headings = (getattr(self.config, "index_headings", None) or {}).get(doc_type)
        if not headings:
            return extract_fulltext(body) or self._extract_section(body, "Key provisions") or ""
        parts = []
        for h in headings:
            sec = extract_fulltext(body) if h == "Full text" else self._extract_section(body, h)
            if sec:
                parts.append(sec)
        return "\n\n".join(parts)

    # ---------- retrieval ----------

    @staticmethod
    def _terms(query: str) -> list[str]:
        return _WORD.findall(query)

    def _fts_rows(self, con, query, doc_type, issuing_body, limit):
        terms = self._terms(query)
        if not terms:
            return {}, []
        match = " ".join(f'"{t}"' for t in terms)
        # d.* not f.*: reading a column off a contentless fts table returns NULL. Joining
        # on rowid for the same reason -- `f.id` is NULL too, so the old `d.id = f.id`
        # join would match zero rows and report an empty corpus.
        sql = ("SELECT d.id, d.title, d.citation, d.doc_type, d.issuing_body, d.path, "
               "bm25(fts) FROM fts f "
               "JOIN docs d ON d.rowid = f.rowid WHERE fts MATCH ?")
        sql_args = [match]
        if doc_type:
            sql += " AND d.doc_type = ?"; sql_args.append(doc_type)
        if issuing_body:
            sql += " AND d.issuing_body = ?"; sql_args.append(issuing_body)
        sql += " ORDER BY bm25(fts) LIMIT ?"
        sql_args.append(max(1, min(int(limit), 40)))
        rows = {r[KW_ID]: r for r in con.execute(sql, sql_args).fetchall()}
        order = sorted(rows, key=lambda i: rows[i][KW_BM25])
        return rows, order

    def _excerpt(self, rel_path: str, doc_type: str, terms: list[str]) -> str:
        """Build a search excerpt from the document on disk.

        Excerpts come from `_searchable_body`, NOT the raw file, so what is shown is the
        text that was actually indexed. Showing a passage the query could never have
        matched is worse than showing none.

        Called only for rows being returned -- at most `limit` of them, where the candidate
        pool can be 40 -- so this does less work than the old SQL did, not more.
        """
        p = self.config.root / rel_path
        try:
            _fm, body = parse_frontmatter(p)
        except (OSError, ValueError) as e:
            # A contentless index depends on the tree still being there at query time.
            # Say so instead of returning "" and looking like a document with no body.
            return f"(excerpt unavailable: {type(e).__name__})"
        return make_excerpt(self._searchable_body(body, doc_type), terms)

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
        sem_chunks: dict = {}

        if not use_sem:
            final = kw_order[:n]
        else:
            # Prefer the chunk-aware ranking when the module provides it: the hit then
            # carries WHERE in the document the match lives (heading/ordinal/preview),
            # and the ordinal plugs straight into get_document(part="chunk:N"). Falls
            # back to bare ids for custom semantic modules that predate rank_chunks.
            rank_chunks = getattr(self._semantic, "rank_chunks", None)
            sem_chunks = {h["doc_id"]: h for h in (rank_chunks(query, pool) or [])} \
                if callable(rank_chunks) else {}
            sem_order = (list(sem_chunks) if sem_chunks
                         else list(self._semantic.rank(query, pool) or []))
            if doc_type or issuing_body:
                keep = []
                for d in sem_order:
                    mr = self._doc_meta_row(con, d)
                    if mr and (not doc_type or mr[KW_DOCTYPE] == doc_type) and \
                            (not issuing_body or mr[KW_ISSUER] == issuing_body):
                        keep.append(d)
                sem_order = keep
            final = sem_order[:n] if mode == "semantic" else self._rrf([kw_order, sem_order])[:n]

        terms = self._terms(query)
        out = []
        for i in final:
            r = rows.get(i)
            if r:
                hit = {"id": r[KW_ID], "title": r[KW_TITLE], "citation": r[KW_CITATION],
                       "doc_type": r[KW_DOCTYPE], "issuing_body": r[KW_ISSUER],
                       "path": r[KW_PATH],
                       "snippet": self._excerpt(r[KW_PATH], r[KW_DOCTYPE], terms)[:400]}
                if use_sem and i in sem_chunks:
                    c = sem_chunks[i]
                    hit["chunk"] = {"ordinal": c["ordinal"], "heading": c["heading"],
                                    "preview": (c["preview"] or "")[:200],
                                    "fetch": f"get_document(part='chunk:{c['ordinal']}')"}
                out.append(hit)
            else:
                mr = self._doc_meta_row(con, i)
                if mr:
                    hit = {"id": mr[KW_ID], "title": mr[KW_TITLE],
                           "citation": mr[KW_CITATION], "doc_type": mr[KW_DOCTYPE],
                           "issuing_body": mr[KW_ISSUER], "path": mr[KW_PATH],
                           "snippet": "(semantic match — no keyword overlap)"}
                    if i in sem_chunks:
                        c = sem_chunks[i]
                        hit["chunk"] = {"ordinal": c["ordinal"], "heading": c["heading"],
                                        "preview": (c["preview"] or "")[:200],
                                        "fetch": f"get_document(part='chunk:{c['ordinal']}')"}
                    out.append(hit)
        return out

    def exists(self, doc_id: str) -> dict | None:
        con = self.ensure_index()
        r = self._doc_meta_row(con, doc_id)
        return ({"id": r[KW_ID], "title": r[KW_TITLE], "doc_type": r[KW_DOCTYPE]}
                if r else None)

    def get(self, doc_id: str, *, part: str = "auto") -> dict:
        """Metadata + body for one document.

        Returns backend-level fields only. CorpusFramework adds `corpus`, `archetype`
        and `disclaimer`, and builds the not-found `did_you_mean` suggestions, so those
        stay identical across every backend instead of being re-derived per storage
        type."""
        r = self._doc_row(doc_id)
        if not r:
            return {"error": f"no document with id {doc_id!r}"}
        path = self.config.root / r[DOC_PATH]
        fm, body = parse_frontmatter(path)
        meta = {"id": r[DOC_ID], "title": r[DOC_TITLE], "citation": r[DOC_CITATION],
                "doc_type": r[DOC_DOCTYPE], "status": r[DOC_STATUS],
                "source_url": r[DOC_SOURCE_URL], "retrieved": r[DOC_RETRIEVED],
                "effective_date": r[DOC_EFFECTIVE] or None,
                "content_mode": r[DOC_CONTENT_MODE], "path": r[DOC_PATH],
                "relationships": {k: v for k, v in (fm.get("relationships") or {}).items() if v},
                # The document's OWN source_url is the authoritative source for it —
                # more precise than the corpus-level URL, which CorpusFramework only
                # falls back to when this is empty.
                "authoritative_source": r[DOC_SOURCE_URL]}
        if r[DOC_EXCEPTION]:
            meta["content_exception"] = r[DOC_EXCEPTION]
        # Corpus-specific frontmatter this corpus has declared it serves.
        #
        # Without this the fixed key set above is all an agent ever sees, and a field can
        # be REQUIRED by a corpus's own schema checks and still be unreachable — which is
        # not a gap so much as a trap, because the field validates and then vanishes.
        # oregon-audits hit it with `audited_period_start`: the value that stops a 2019
        # finding being read as current was invisible to every caller.
        #
        # Allow-listed rather than "return everything": the response shape is an interface
        # contract, and a corpus adding a frontmatter key should not silently change what
        # its server emits. Missing keys are skipped, so declaring a field a document does
        # not carry is harmless.
        for key in self.config.mcp_extra_document_fields:
            if key in fm:
                meta[key] = fm[key]
        headings = re.findall(r"^## (.+)$", body, re.M)
        subs = self._subheadings(body)
        if part == "auto" and r[DOC_SIZE] > BIG_DOC_BYTES:
            glance = self._extract_section(body, "At a glance")
            hint = (f" — or one of its {len(subs)} subsections" if subs else "")
            resp = {**meta, "note": (f"document body is {r[DOC_SIZE] // 1024} KB — pass "
                                     f"part='full text' (or another heading below{hint}) "
                                     "to page in the content you need"),
                    "at_a_glance": glance, "sections": headings}
            if subs:
                resp["subsections"] = subs
            return resp
        if part in ("auto", "full"):
            return {**meta, "body": body}
        chunk = self._chunk_part(path, r[DOC_ID], part)
        if chunk is not None:
            return {**meta, **chunk}
        sec = self._extract_section(body, part) or next(
            (self._extract_section(body, h) for h in headings if h.lower() == part.lower()), None)
        if sec is not None:
            return {**meta, "section": part, "body": sec}
        sub, matched = self._extract_subsection(body, part)
        if sub is not None:
            return {**meta, "section": matched, "body": sub}
        err = {**meta, "error": f"no section {part!r}", "sections": headings}
        if subs:
            near = [h for h in subs if part.lower() in h.lower()][:8]
            err["subsections_matching"] = near or f"{len(subs)} subsections; none contain {part!r}"
        return err

    def overview(self) -> dict:
        con = self.ensure_index()
        by_type = dict(con.execute(
            "SELECT doc_type, COUNT(*) FROM docs GROUP BY doc_type ORDER BY 2 DESC"))
        by_mode = dict(con.execute(
            "SELECT content_mode, COUNT(*) FROM docs GROUP BY content_mode ORDER BY 2 DESC"))
        head = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=self.config.root,
                              capture_output=True, text=True).stdout.strip()
        return {"documents_by_type": by_type, "content_mode": by_mode, "commit": head}

    def holdings_for(self, slug: str) -> dict:
        """{content_mode: count} for one issuing-body slug.

        `issuing_body_slug` is PATH-derived (see config.scope_slug_for), not read from the
        document's own free-text `issuing_body` field — which is why this query belongs to
        the backend that owns the path-to-row mapping rather than to the tool that asks the
        question. Known to under-report where a corpus is not issuing-body-scoped; that is
        a separate defect in WHAT is counted (#71), not in who counts it.
        """
        con = self.ensure_index()
        return dict(con.execute(
            "SELECT content_mode, COUNT(*) FROM docs WHERE issuing_body_slug = ? "
            "GROUP BY content_mode", (slug,)).fetchall())

    def health(self) -> dict:
        """A file corpus is reachable iff its index holds anything.

        Reporting an EMPTY index as unreachable is the point of this method: the old
        behaviour answered 'nothing found' for a misconfigured corpus, which a caller
        cannot distinguish from a genuine no-match."""
        try:
            con = self.ensure_index()
            n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            # Documents present in the index but with NO searchable body. Config
            # validation catches a malformed index_headings; it cannot catch a
            # well-formed one naming a heading no document actually uses
            # (["Full Text"] for "## Full text"). That mistake leaves rows in `docs`
            # while `fts.body` is empty, so a row count alone reports a healthy corpus
            # that returns nothing for every keyword query.
            #
            # Reads docs.body_chars, NOT fts.body. On a contentless fts table `f.body`
            # reads NULL, so `f.body = ''` is never true and this check would report a
            # clean bill of health for every corpus, forever. body_chars is written from
            # the same string that goes into the index.
            empty = dict(con.execute(
                "SELECT doc_type, COUNT(*) FROM docs WHERE body_chars = 0 "
                "GROUP BY doc_type"))
        except Exception as e:                       # noqa: BLE001
            return {"reachable": False, "checked_at": None,
                    "detail": f"{type(e).__name__}: {e}"}
        if not n:
            return {"reachable": False, "checked_at": None,
                    "detail": "index is EMPTY — content_roots may be misconfigured"}
        detail = f"{n} document(s) indexed"
        problems = {}
        for dt, cnt in empty.items():
            total = con.execute("SELECT COUNT(*) FROM docs WHERE doc_type = ?",
                                (dt,)).fetchone()[0]
            # Some documents legitimately have no body (a stub, a metadata-only record).
            # A doc_type where ALL of them are empty is a configuration fault.
            if total and cnt == total:
                problems[dt] = cnt
        # Only warn for doc_types someone CONFIGURED index_headings for. An
        # unconfigured doc_type with no body is the corpus's own authoring choice --
        # `external_reference` is summary-by-policy and deliberately has no
        # "## Full text", and it stays findable through the title/citation/tags
        # columns. Warning on that trains an operator to ignore the message, which
        # costs more than the case it catches.
        configured = set(getattr(self.config, "index_headings", None) or {})
        problems = {dt: c for dt, c in problems.items() if dt in configured}
        if problems:
            detail += (f" — WARNING: index_headings is configured for doc_type "
                       f"{sorted(problems)}, but EVERY one of those "
                       f"{sum(problems.values())} document(s) indexed with empty "
                       f"searchable text. The configured headings almost certainly do "
                       f"not match the '## ' headings these documents actually use.")
        return {"reachable": True, "checked_at": None, "detail": detail,
                "empty_body_by_doc_type": problems}
