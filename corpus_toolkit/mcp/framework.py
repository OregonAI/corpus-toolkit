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
from corpus_toolkit.mcp.backends import (
    BIG_DOC_BYTES, REQUIRED_BACKEND_METHODS, FileBackend, RetrievalBackend,
)
from corpus_toolkit.remote import (
    document_url as sibling_document_url, load_sibling_index, lookup as sibling_lookup,
)
from corpus_toolkit.repo import (
    content_files, extract_fulltext, parse_frontmatter, repo_state,
)

# BIG_DOC_BYTES is imported from .backends above and deliberately NOT redefined here.
#
# It used to be, as `50_000`, on the line this comment replaces — three lines below an
# import of the same name from `backends`, where it is `50 * 1024` (51,200). The
# assignment shadowed the import, so this module's name carried 50,000 while the only
# code that actually branches on it (backends.py's `part == "auto"` big-doc check) used
# 51,200. A document between those two values was "big" to one module and not the other,
# and nothing errored — the divergence just decided a branch inconsistently depending on
# which module a caller had imported from. 51,200 is kept because it is the value that
# was always live; dropping the shadow changes no behaviour, it only stops the name
# meaning two things. See corpus-toolkit#52.

# ---------- citation-scheme registry (populated by a corpus's citation_module) ----------

_SCHEMES: list[tuple[str, "re.Pattern", str | None, object | None, str | None]] = []

# Kept identical to `properties.id.pattern` in schemas/document.frontmatter.v1.schema.json.
# A candidate id outside this charset can never match a validated document; see the check in
# _match_scheme, which is the only place it is used.
_LEGAL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]+$")
# Set while a CorpusFramework imports its citation module; see register_scheme.
# A citation scheme is a property of ONE corpus, and this platform's premise is many
# corpora sharing one toolkit — a process-wide registry made correctness depend on
# never constructing two frameworks at once, which nothing enforced and the
# cross-corpus feature actively invites.
_COLLECTING: list | None = None

# Collected schemes per (corpus root, citation module). THE IMPORT IS THE REGISTRATION,
# and an import happens once per process — which is what this cache exists for; see
# _collect_schemes.
_SCHEME_CACHE: dict[tuple[str, str], list] = {}


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
    entry = (name, re.compile(pattern), id_template, resolver, corpus)
    # A CorpusFramework installs a collector around its citation_module import, so the
    # schemes land on that instance instead of a process-wide list. The global remains
    # for direct callers and tests. Corpus code is unchanged: it still imports and calls
    # register_scheme at module scope exactly as before.
    if _COLLECTING is not None:
        _COLLECTING.append(entry)
    else:
        _SCHEMES.append(entry)


def clear_schemes() -> None:
    """Empty the module-level registry AND the per-corpus cache.

    Both, because they are two halves of one answer: a test that registers directly and
    then builds a framework over a corpus it has already built once would otherwise be
    served the schemes the earlier test collected. The cache is keyed by corpus, not by
    test, so nothing else would ever evict it."""
    _SCHEMES.clear()
    _SCHEME_CACHE.clear()


def _collect_schemes(config: CorpusConfig) -> list:
    """This corpus's citation schemes, importing its `citation_module` to collect them.

    A corpus registers its schemes with top-level `register_scheme` calls, so the IMPORT
    IS THE REGISTRATION — and a module imports once per process. Two mechanisms keep that
    from meaning "once per process is all you get":

      * the cache: a second framework over the same corpus is handed the same list.
        Keyed by (root, module) because that pair IS the corpus's scheme identity.
      * `force=True`: on a cache MISS the module is re-executed rather than served from
        sys.modules, so the registrations actually happen. Without it, evicting the cache
        (`clear_schemes`) would put us straight back into the bug.

    THE BUG (corpus-toolkit#73). A second CorpusFramework over one corpus collected
    nothing — the module was cached, its top-level calls did not re-run — and fell back to
    `_SCHEMES`, the process-wide list this collector had just deliberately bypassed and
    which was therefore empty. It ended up with NO schemes, and `resolve_citation` replied
    "no citation scheme recognized this format" about a corpus that recognizes it
    perfectly well. That is a false statement about the server's own capability, the shape
    response convention 5 exists to prevent; it also skipped sibling resolution entirely,
    so a sibling citation came back `unresolved` with no `sibling_unavailable` marker —
    "could not check" served as "not there".

    Nothing deployed hit it, because server.py builds one framework per process. The
    direction of travel is more corpora per process, not fewer.
    """
    key = (str(config.root), config.citation_module)
    cached = _SCHEME_CACHE.get(key)
    if cached is not None:
        return list(cached)

    global _COLLECTING
    collected: list = []
    _COLLECTING = collected
    try:
        load_module(config.citation_module, config.root, force=True)
    finally:
        _COLLECTING = None

    if not collected and _SCHEMES:
        # Re-execution registered nothing into this collector, but the process-wide list
        # is not empty — a module that guards its own re-import, or a corpus that
        # registered from somewhere other than the module named here. Adopt those rather
        # than serve this corpus with none; an over-broad scheme list degrades to a miss,
        # an empty one lies about the format being unrecognized.
        collected = list(_SCHEMES)

    _SCHEME_CACHE[key] = collected
    return list(collected)


class CorpusFramework:
    def __init__(self, config: CorpusConfig):
        self.config = config
        self.disclaimer = (
            "NON-AUTHORITATIVE curated copy for AI-agent reference. Not the "
            "official text — always cite and verify against source_url.")
        self._graph_cache = None
        self._schemes: list = (_collect_schemes(config) if config.citation_module
                               else [])
        self._semantic = self._load_semantic()
        # The retrieval seam. A corpus may supply its own via plugins.retrieval_module
        # (an API archetype does); everything else keeps the historical file backend.
        # Loaded exactly like citation_module, and validated against the protocol so a
        # broken adapter fails at startup rather than on the first query.
        self.backend = self._load_backend()

    def _load_semantic(self):
        """The semantic seam, HANDED THIS CORPUS (corpus-toolkit#74).

        A module exposing `make(config)` gets it and we use the returned object; anything
        else is duck-typed as the module itself, which is what a corpus-supplied semantic
        module written before `make` existed looks like. Either way the backend only ever
        calls `available()` / `rank()` / `rank_chunks()`.

        The distinction matters because the module-level form CANNOT be per-corpus: it is
        one installed module object shared by every framework in the process, so its loaded
        index is whichever corpus asked first, and it resolves its artifact path from the
        cwd rather than from the corpus root the builder wrote to.
        """
        if not self.config.semantic_search_module:
            return None
        mod = load_module(self.config.semantic_search_module, self.config.root)
        factory = getattr(mod, "make", None)
        return factory(self.config) if callable(factory) else mod

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
        missing = [m for m in REQUIRED_BACKEND_METHODS
                   if not callable(getattr(backend, m, None))]
        if missing:
            raise TypeError(f"retrieval_module {mod!r} produced {type(backend).__name__}, "
                            f"which does not satisfy RetrievalBackend: missing {missing}")
        return backend

    @property
    def schemes(self) -> list:
        """This corpus's citation schemes. Falls back to the module-level registry for
        callers that register directly (tests, scripts) rather than via a
        citation_module."""
        return self._schemes or _SCHEMES

    # ---------- retrieval (delegated to the backend) ----------

    @property
    def _cache_dir(self) -> Path:
        """Corpus-level cache root. Shared: FileBackend puts its FTS db here, and
        cross-corpus sibling indices live under _cache_dir/siblings regardless of which
        backend serves this corpus — a sibling lookup is a corpus concern, not a
        storage one."""
        return self.config.root / "_meta" / ".cache"

    def _extract_section(self, body: str, heading: str):
        return FileBackend._extract_section(self, body, heading)

    # ---------- graph ----------

    def _envelope(self) -> dict:
        """The three fields docs/mcp-interface-contract.md, response convention 1,
        requires on every response. Assembled in ONE place because it kept being
        assembled in several and they disagreed: `corpus_overview` carried
        corpus/archetype but never `authoritative_source`, so all four live corpora
        answered the "call this first" tool without ever telling the agent where the
        authoritative text is — while the same response's disclaimer instructed it to
        "verify at source".

        `authoritative_source` is None when a corpus has not declared one. Emitting the
        key as null is deliberate: an absent key reads as "the server did not look",
        a null one as "this corpus declared none", and only the second is true."""
        return {"corpus": self.config.id,
                "archetype": self.config.archetype,
                "authoritative_source": self.config.authoritative_source}

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

    def _graph_lookup(self, doc_id: str):
        """(node, error) for the graph-backed tools. `error` is None on success.

        Three conditions used to collapse into one message, and the one reported was the
        only one that was FALSE. graph() degrades to ({}, {}) when graph_path is absent
        — a legitimate state for a corpus that has not built a graph yet — so
        `doc_id not in nodes` was true for every id in the corpus and both tools answered
        "no document with id X" about documents they were actively serving. Measured on
        oregon-legislature: graph_neighbors("measure-2025r1-hb3592") denied a document
        that search_corpus returns and get_document serves with full provenance. An agent
        told a document does not exist stops looking; the corpus in fact has it and simply
        has no relationship graph. Response convention 5 requires errors to be explicit,
        and an explicit lie is worse than silence.

        The middle case matters just as much and is not in the issue: a graph that EXISTS
        but predates the document. That is the exact silent failure the corpus-side
        `generated` CI job exists to catch, and naming it points at the rebuild instead of
        at a nonexistent document."""
        nodes, _ = self.graph()
        node = nodes.get(doc_id)
        if node is not None:
            return node, None

        if not nodes:
            try:
                rel = self.config.graph_path.relative_to(self.config.root)
            except ValueError:
                rel = self.config.graph_path
            return None, {**self._envelope(), "no_graph": True,
                          "error": "this corpus has no relationship graph",
                          "note": (f"graph_path {str(rel)!r} does not exist, so no "
                                   f"document has neighbours here. This is NOT a "
                                   f"statement about whether {doc_id!r} exists — try "
                                   f"get_document or search_corpus.")}

        # The graph is real but does not know this id. Ask the backend whether the
        # DOCUMENT is real before reporting it missing. Guarded because a remote backend's
        # exists() is a network call, and a probe that raises would turn a precise error
        # into an opaque tool failure — the exact shape of the bug this method fixes.
        try:
            found = self.backend.exists(doc_id)
        except Exception:                                  # noqa: BLE001
            found = None
        if found:
            return None, {**self._envelope(), "not_in_graph": True,
                          "error": f"document {doc_id!r} exists but is not a node in the "
                                   f"relationship graph",
                          "note": ("the graph is stale relative to the corpus — rebuild "
                                   "it (the corpus's graph builder, e.g. "
                                   "`python3 src/build_graph.py`) and commit the result")}
        return None, {**self._envelope(),
                      "error": f"no document with id {doc_id!r}"}

    # ---------- citation schemes ----------

    def _match_schemes(self, c: str):
        """Run this corpus's citation schemes over `c`, returning
        (scheme_name, sibling_corpus_id, candidate_ids, resolver_note).

        Extracted from resolve_citation so the graph tools decide what an edge target
        MEANS the same way citation resolution does. They did not before, and the
        divergence was the whole of issue #4: `resolve_citation("OAR 166-300-0015")`
        resolved into the sibling corpus correctly while `graph_neighbors` treated the
        identical string as a missing local node and raised KeyError."""
        nodes, _ = self.graph()
        matched = []
        for name, pattern, id_template, resolver, scheme_corpus in self.schemes:
            m = pattern.search(c)
            if not m:
                continue
            note = None
            if resolver is not None:
                try:
                    nparams = len(inspect.signature(resolver).parameters)
                except (TypeError, ValueError):
                    nparams = 1
                result = resolver(m, nodes) if nparams >= 2 else resolver(m)
                if isinstance(result, tuple):
                    cands, note = list(result[0] or []), result[1]
                else:
                    cands = list(result or [])
            else:
                try:
                    cid = id_template.format(**m.groupdict()) if m.groupdict() \
                        else id_template.format(*m.groups())
                    cands = [cid]
                except (IndexError, KeyError):
                    cands = []
            # AN ID THAT NO DOCUMENT IS ALLOWED TO HAVE IS A SCHEME BUG, NOT A MISS.
            # `document.frontmatter.v1` pins ids to `^[a-z0-9][a-z0-9._-]+$`, so a candidate
            # outside that charset cannot match anything — not because the corpus lacks the
            # document, but because the scheme built an id the corpus could never contain.
            # Reported identically to a genuine absence, that is indistinguishable from a
            # coverage gap: two corpora templated `ors-{num}` straight from the citation
            # text, produced `ors-279A.010` against a corpus holding `ors-279a.010`, and
            # every lettered ORS chapter silently resolved to nothing. It was filed against
            # the OTHER corpus as missing chapters, twice, before anyone looked at the id.
            #
            # Dropped rather than raised: this runs inside a live MCP server answering a
            # user's question, and a malformed scheme should degrade to a clear diagnosis
            # rather than a stack trace. The note is what makes it loud.
            bad = [c for c in cands if not _LEGAL_ID.match(str(c))]
            if bad:
                cands = [c for c in cands if c not in bad]
                note = (f"citation-scheme bug, not a coverage gap: scheme {name!r} produced "
                        f"{', '.join(repr(b) for b in bad[:3])}, which cannot match any "
                        f"document — ids are lowercase, matching {_LEGAL_ID.pattern}. "
                        f"Fix the scheme's id_template/resolver.")
            matched.append((name, scheme_corpus, cands, note))
        if not matched:
            return None, None, [], None

        # ALL matching schemes are kept — the first-wins `return` that used to sit in the
        # loop meant `CJIS 5.9.4 and 2 CFR 200.303` resolved as CFR only and the CJIS
        # version-gap REFUSAL was silently dropped, while `schemes_attempted` read as
        # though every scheme had been consulted (federal-reference#12).
        #
        # Candidates merge only across schemes that target the SAME corpus, because the
        # caller routes one candidate list to one place (local backend or one sibling).
        # A match targeting a different corpus keeps its note and is named, not merged —
        # honest partial coverage beats mis-routed candidates.
        primary_corpus = next((c for _, c, cs, _ in matched if cs), matched[0][1])
        names, cands_merged, notes = [], [], []
        for name, corp, cs, note in matched:
            names.append(name)
            if corp == primary_corpus:
                cands_merged.extend(i for i in cs if i not in cands_merged)
            elif cs:
                notes.append(f"scheme {name!r} also matched, targeting corpus "
                             f"{corp or 'local'!r} — cite it separately to resolve there")
            if note:
                notes.append(note if len(matched) == 1 else f"[{name}] {note}")
        merged_note = "; ".join(notes) if notes else None
        return "+".join(names), primary_corpus, cands_merged, merged_note

    # ---------- graph edge targets ----------

    def _neighbour_records(self, targets, nodes) -> list[dict]:
        """Edge targets -> neighbour records, resolving anything that is not a local node
        as an EXTERNAL reference instead of raising.

        `nodes[t]` assumed every edge target was local. It is not, and for the corpora
        this platform exists to connect it is usually not: oregon-records-retention's
        graph reports n_edges 440 / n_edges_external 440 — every edge points at an OAR
        citation held by executive-regulatory-frameworks — so `graph_neighbors` raised
        KeyError for EVERY document in that corpus, surfacing to the caller as a tool
        error whose entire message was the citation string. The contract lists remote
        `corpus:id` edges as a specified feature of this tool, not an edge case.

        External targets are resolved through the same sibling index resolve_citation
        uses, so a neighbour comes back as {corpus, id, url} where the sibling can be
        consulted. Grouped per sibling: one index load per tool call, not one per edge.
        A sibling that cannot be consulted leaves the record marked `sibling_unavailable`
        rather than silently bare — "could not check" and "not there" are opposite
        answers and must never collapse (response convention 5)."""
        out: list[dict] = []
        external: list[dict] = []
        for t in targets:
            node = nodes.get(t)
            if node is not None:
                out.append({"id": t, "title": node["title"], "doc_type": node["doc_type"]})
            else:
                rec = {"citation": t, "external": True}
                out.append(rec)
                external.append(rec)
        if external:
            self._resolve_external_neighbours(external)
        return out

    def _resolve_external_neighbours(self, recs: list[dict]) -> None:
        """Annotate external neighbour records in place with a sibling-corpus hit."""
        by_sibling: dict[str, list[tuple[dict, list[str]]]] = {}
        for rec in recs:
            # Guarded HERE and not inside _match_schemes, so resolve_citation keeps its
            # exact behaviour. A scheme's `resolver` is corpus-supplied code; letting it
            # raise through a graph tool would turn a corpus's own citation bug into the
            # same opaque "tool failed" this method was written to eliminate — and for
            # neighbours, enrichment is a bonus. The edge is still reported as external.
            try:
                _, sib_id, cands, _ = self._match_schemes(rec["citation"])
            except Exception:                              # noqa: BLE001
                continue
            if sib_id and cands:
                by_sibling.setdefault(sib_id, []).append((rec, cands))
        for sib_id, items in by_sibling.items():
            all_cands = sorted({c for _, cands in items for c in cands})
            hits, status = self._resolve_in_sibling(sib_id, all_cands)
            by_id = {h["id"]: h for h in hits}
            for rec, cands in items:
                hit = next((by_id[c] for c in cands if c in by_id), None)
                if hit is not None:
                    rec.update(hit)
                    rec["resolved_via"] = f"sibling:{sib_id}"
                    if status.get("stale"):
                        rec["sibling_index_stale"] = True
                elif not status.get("available"):
                    rec["sibling_unavailable"] = sib_id
                    rec["note"] = (
                        f"belongs to sibling corpus {sib_id!r}, whose index could not be "
                        f"loaded ({status['reason']}) — NOT evidence it is absent")

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
            # The not-found branch used to return the bare backend error, so the one
            # response an agent gets when it guesses an id wrong was the only one with no
            # corpus/archetype on it — it could not even tell which corpus said no.
            return {**self._envelope(), **rec,
                    "did_you_mean": [{"id": s["id"], "title": s["title"]} for s in sug]}
        # Envelope FIRST so the record wins: a document's own `authoritative_source`
        # (its source_url) is the more precise answer to "where is the official text",
        # and the corpus-level URL is only the fallback — for a backend that emits none,
        # or a document whose source_url is empty.
        out = {**self._envelope(), **rec,
               "corpus": self.config.id, "archetype": self.config.archetype,
               "disclaimer": self.disclaimer}
        if not out.get("authoritative_source"):
            out["authoritative_source"] = self.config.authoritative_source
        return out

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
            # v1.19.0 rows carry status; surface anything not known-current LOUDLY.
            # "" is UNKNOWN (an older index), and unknown is stated, never upgraded to
            # current — the failure this field exists to close was a sibling serving
            # superseded federal text as current law (corpus-toolkit#25).
            status = row.get("status", "")
            if status and status != "current":
                hit["status"] = status
                hit["note"] = (f"resolves, but the sibling records this document as "
                               f"{status.upper()} — not current text")
            elif not status:
                hit["status"] = "unknown"
            url = sibling_document_url(sib, row["path"])
            if url:
                hit["url"] = url
            hits.append(hit)
        return hits, {"available": True, "stale": bool(index.get("_stale")),
                      "reason": ""}

    def resolve_citation(self, citation: str) -> dict:
        nodes, _ = self.graph()
        c = citation.strip()
        matched_scheme, matched_corpus, cands, resolver_note = self._match_schemes(c)
        # Existence is decided by the BACKEND, with the graph as a fast path.
        #
        # This used to consult `nodes` alone. graph() degrades to {} when graph_path is
        # absent (a corpus that has not built one, or an ingest that outran the rebuild),
        # so every candidate "did not exist" and the server reported documents it was
        # actively serving as nonexistent — a false statement about its own contents,
        # not merely a missing answer. backend.exists() is exactly the cheap probe this
        # needs, and was added for it.
        hits = []
        for i in cands:
            node = nodes.get(i)
            if node is not None:
                hits.append({"id": i, "title": node["title"],
                             "doc_type": node["doc_type"]})
                continue
            found = self.backend.exists(i)
            if found:
                hits.append({"id": found["id"], "title": found.get("title", ""),
                             "doc_type": found.get("doc_type", "")})
        out = {"citation": citation, "matches": hits, **self._envelope()}

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
            out["schemes_attempted"] = [s[0] for s in self.schemes]
            out["search_fallback"] = [{"id": s["id"], "title": s["title"]}
                                      for s in self.search_corpus(c, limit=3)]
        return out

    def authority_chain(self, doc_id: str, direction: str = "both", depth: int = 3) -> dict:
        nodes, edges = self.graph()
        node, err = self._graph_lookup(doc_id)
        if err is not None:
            return err
        depth = max(1, min(int(depth), 6))

        def walk(start, keys):
            # `keys` may name several relations. They are merged per node rather than
            # walked separately and concatenated, because `walk` seeds `seen` with the
            # start node per call — two relations naming the same target would otherwise
            # emit it twice.
            keys = (keys,) if isinstance(keys, str) else tuple(keys)
            seen, levels, frontier = {start}, [], [start]
            for _ in range(depth):
                nxt = []
                for i in frontier:
                    rels = edges.get(i, {})
                    targets = [t for k in keys for t in rels.get(k, [])]
                    # dict.fromkeys, not a set: order is the graph's edge order, and a
                    # target listed twice on one node must still only appear once (the
                    # old loop got that from marking `seen` inside the loop body).
                    fresh = list(dict.fromkeys(t for t in targets if t not in seen))
                    seen.update(fresh)
                    # Same external-target rule as graph_neighbors — this walk had the
                    # identical unguarded nodes[t] and raised KeyError the moment an
                    # implements/implemented_by edge pointed at a sibling corpus, which
                    # is precisely what a cross-corpus authority chain IS.
                    for rec in self._neighbour_records(fresh, nodes):
                        nxt.append({**rec, "via": i})
                if not nxt:
                    break
                levels.append(nxt)
                # An external neighbour has no local edges to continue from, so only
                # local ids extend the frontier. Following a `citation` key here would
                # walk nothing and re-add it at every level.
                frontier = [n["id"] for n in nxt if "id" in n and not n.get("external")]
            return levels

        out = {**self._envelope(), "id": doc_id, "title": node["title"],
               "doc_type": node["doc_type"]}
        # implements/implemented_by are ALWAYS walked and are not configurable. They are the
        # asserted authority relation; everything else a corpus declares is returned beside
        # them, never merged into them — see config._validated_authority_relations for why.
        configured = self.config.mcp_authority_relations
        if direction in ("up", "both"):
            out["up_implements"] = walk(doc_id, "implements")
            for name, keys in (configured.get("up") or {}).items():
                out[f"up_{name}"] = walk(doc_id, keys)
        if direction in ("down", "both"):
            out["down_implemented_by"] = walk(doc_id, "implemented_by")
            for name, keys in (configured.get("down") or {}).items():
                out[f"down_{name}"] = walk(doc_id, keys)
        return out

    def graph_neighbors(self, doc_id: str) -> dict:
        nodes, edges = self.graph()
        node, err = self._graph_lookup(doc_id)
        if err is not None:
            return err
        out = {**self._envelope(), "id": doc_id, "title": node["title"]}
        for k, targets in edges.get(doc_id, {}).items():
            out[k] = self._neighbour_records(targets, nodes)
        return out

    def corpus_overview(self) -> dict:
        out = {
            **self._envelope(),
            "jurisdiction": self.config.jurisdiction,
            "disclaimer": self.disclaimer,
            **self.backend.overview(),
            "graph_edges": sum(len(v) for d in self.graph()[1].values() for v in d.values()),
            "contract_version": self.config.contract_version,
        }
        if not self.config.authoritative_source:
            # Do not let the omission pass as a fact about the world. The disclaimer on
            # this very response tells the agent to "verify at source" and then declines
            # to say where the source is; naming the missing config key is the only way
            # the gap reaches anyone who can close it.
            out["config_warning"] = (
                "this corpus declares no `corpus.authoritative_source` in "
                "_meta/corpus.yml, which docs/mcp-interface-contract.md response "
                "convention 1 requires on every response — set it to the URL where the "
                "official text lives")
        return out

    # ---------- document-corpus extension: issuing-body profile ----------

    def issuing_body_profile(self, slug_or_query: str) -> dict:
        # Every return path carries the envelope. This tool was the one convention-1
        # violation on the surface — both its error shapes and its success shape omitted
        # corpus/archetype/authoritative_source, undocumented (corpus-toolkit#38).
        if not self.config.issuing_body_registry:
            return {**self._envelope(),
                    "error": "this corpus has no issuing-body registry configured"}
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
                return {**self._envelope(),
                        "error": f"no unique issuing body match for {slug_or_query!r}",
                        "candidates": [{"slug": s, "name": entries[s].get("name")} for s in hits[:8]]}
            slug = hits[0]

        # Through the seam, not around it. This ran raw SQL against FileBackend's `docs`
        # table via ensure_index(), which is why the tool could not exist for any other
        # backend and why three separate guards were needed to keep that from surfacing as
        # a crash (corpus-toolkit#75).
        docs = self.backend.holdings_for(slug)
        return {
            **self._envelope(),
            "slug": slug,
            "registry": entries[slug],
            "curated": curated.get(slug, {}),
            "in_repo": docs or "no documents ingested for this issuing body yet",
            "disclaimer": self.disclaimer,
        }
