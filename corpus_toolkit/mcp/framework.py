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
    extract_section,
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

# One page of `documents_by_agency`. A slug can match tens of thousands of documents on ERF,
# and an unclamped LIMIT put all of them in one MCP response; SQLite also reads a NEGATIVE
# limit as unbounded, so `limit=-1` was the whole corpus. Higher than `search`'s 40 because
# this is an exhaustive per-agency listing rather than a relevance ranking.
_MAX_DOCUMENTS_PER_PAGE = 200

# What `issuing_body_profile` puts in `in_repo` when it counted nothing, keyed by whether
# attribution coverage is complete (True), known to be partial (False) or unmeasured (None).
#
# THREE STRINGS BECAUSE THERE ARE THREE ANSWERS. "Nothing ingested for this body" is a
# claim about the corpus, and it is only true when every document in it is counted for some
# registry body. Where some are counted for none, or where nothing measured — an old-shape
# backend, a half-reported coverage, an empty index — the honest answer is "nothing I could
# attribute": the same distinction as `no_graph` vs `not_in_graph` (CONTEXT.md).
_NO_HOLDINGS = {
    True: "no documents ingested for this issuing body yet",
    False: ("no documents attributed to this issuing body — but this corpus holds "
            "documents that are counted for no body at all, so this is not the same as "
            "none ingested; see `attribution`"),
    None: ("no documents attributed to this issuing body — attribution coverage was not "
           "measured here, so none-ingested cannot be told apart from none-attributable; "
           "see `attribution`"),
}

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


def _name_match(entry: dict, fields, query: str) -> tuple[str, str] | None:
    """The first declared name field of `entry` holding a name that contains `query`, as
    (field, name) — or None where the query reaches none of them. Case-insensitive.

    THE LOWERCASING HAPPENS HERE, not in the caller. Taking a pre-lowered query would be an
    unstated precondition, and the way it breaks is the failure this whole function exists
    to fix: a later caller passing raw text matches nothing and looks like a body that is
    not there.

    A field's value may be a STRING or a LIST of strings (ERF's curated `aliases`), and a
    list is matched element-wise. Anything else in a registry cell is skipped rather than
    coerced: a registry is hand-maintained, and `str(None)` matching "none" is a match
    nobody wrote.
    """
    q = query.lower()
    for field in fields:
        value = entry.get(field)
        for name in (value if isinstance(value, (list, tuple)) else [value]):
            if isinstance(name, str) and q in name.lower():
                return field, name
    return None


class CorpusFramework:
    def __init__(self, config: CorpusConfig):
        self.config = config
        self.disclaimer = (
            "NON-AUTHORITATIVE curated copy for AI-agent reference. Not the "
            "official text — always cite and verify against source_url.")
        self._graph_cache = None
        # Relation names in this corpus's graph that collide with response keys
        # `graph_neighbors` writes (corpus-toolkit#105). Populated when the graph is parsed
        # and read only by that tool; empty for every corpus on the platform today.
        self._reserved_clashes: list[str] = []
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

    def ensure_index(self):
        """Build or open this corpus's FTS index. Delegates to the backend.

        RESTORED AFTER BEING DELETED IN #75, WHICH BROKE EVERY ERF DEPLOY. The FTS index is
        a FileBackend implementation detail, `issuing_body_profile` no longer reaches
        through it, and nothing inside this package called this — so it looked like dead
        code. It was not. `executive-regulatory-frameworks`'s Dockerfile bakes its index at
        image build:

            RUN python3 -c "... CorpusFramework(config_mod.load('_meta/corpus.yml')).ensure_index()"

        Deleting it turned that into `AttributeError: 'CorpusFramework' object has no
        attribute 'ensure_index'`, so the image build failed, `deployed.txt` never advanced,
        and the reconcile loop re-detected the same drift and rebuilt a 1 GB context every
        ten minutes for hours — starving every other corpus behind it (platform-deploy#28).

        THE LESSON, WORTH MORE THAN THE METHOD. A search of `corpus_toolkit/` and `tests/`
        found no caller, and that was the whole of the evidence. The callers are in EIGHT
        OTHER REPOSITORIES that pin this one, and nothing on this platform checks a release
        against them — `release-gate.yml` instantiates a corpus from `corpus-template`,
        which does not call this. A method reachable from a corpus repo is public surface
        whether or not it looks like it.

        Kept as a real method rather than restored-then-deprecated: it is one line of
        delegation, a corpus is entitled to ask its framework to build the index, and a
        deprecation would only move the same breakage to a later release.
        """
        idx = getattr(self.backend, "ensure_index", None)
        if idx is None:
            # An API-archetype corpus has no FTS index. Say which backend and why, rather
            # than an AttributeError that reads as a toolkit bug.
            raise AttributeError(
                f"{type(self.backend).__name__} has no FTS index — this corpus is not "
                "file-backed, so ensure_index() is not meaningful for it")
        return idx()

    def _extract_section(self, body: str, heading: str):
        """Kept as a method because corpora reach for it. It used to be implemented as
        `FileBackend._extract_section(self, body, heading)` — an unbound method of an
        unrelated class, handed a CorpusFramework as `self`, which worked only because the
        body never touched it."""
        return extract_section(body, heading)

    # ---------- graph ----------

    def with_envelope(self, payload: dict) -> dict:
        """`payload` plus response convention 1's three fields, WITH THE ENVELOPE WINNING.

        The supported way an extension tool registered through `plugins.tools_module`
        satisfies the convention (corpus-toolkit#96):

            @mcp.tool()
            def list_datasets() -> ResponseEnvelope:
                return framework.with_envelope({"datasets": [...]})

        A MERGE RATHER THAN A GETTER, because precedence is not each corpus's to remember.
        The first version of this shipped a getter and documented
        `{**framework.response_envelope(), **payload}` — envelope FIRST, payload last —
        which is precisely the class corpus-toolkit#102/#104 eliminated, reinstated across
        a repo boundary where it is harder to see. Every built-in puts the assembled front
        LAST for that reason: `get_document`'s not-found branch, `corpus_overview`,
        `resolve_citation`. Blessing the opposite for corpora would have made the toolkit
        enforce on itself a rule it told everyone else to invert.

        It is not hypothetical. A `join_lookup` relating this corpus to a sibling has
        `corpus` as its natural key, and oregon-budget ships one: measured, such a payload
        served `corpus: "oregon-legislature"` from a corpus whose id was something else —
        #38 reopened from the extension side — and a list-valued `corpus` stopped the tool
        answering at all.

        A corpus that genuinely needs to name another corpus in its payload should use a
        key that is not one of the three; `corpus` means "who is answering", and that is
        never the extension tool's to decide.

        Annotating a tool `-> ResponseEnvelope` WITHOUT going through this is a hard tool
        error, not a weaker response: the three fields are required with no defaults. The
        annotation and the payload change together.
        """
        return {**payload, **self._envelope()}

    def response_envelope(self) -> dict:
        """Response convention 1's three fields, as a copy, for READING.

        Use `with_envelope` to build a response — it merges in the correct direction.
        Spreading this into a payload yourself risks the payload displacing the envelope,
        which is the failure described there.
        """
        return dict(self._envelope())

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
        a null one as "this corpus declared none", and only the second is true.

        What the value means here is the corpus's FRONT DOOR — where a reader starts for
        this corpus's official text — and not a citation for whatever this particular
        response happens to be about (corpus-toolkit#70). `get_document` is the one tool
        with something more precise to say, and overrides it below."""
        return {"corpus": self.config.id,
                "archetype": self.config.archetype,
                "authoritative_source": self.config.authoritative_source}

    def _reserved_response_keys(self) -> frozenset:
        """Response keys a graph relation name may not take (corpus-toolkit#105).

        DERIVED FROM WHAT `graph_neighbors` WRITES, not duplicated as a literal. That tool
        assembles the envelope plus its own `id` and `title` before looping over relation
        names, and each relation becomes a response key verbatim — so exactly those keys
        are unavailable. A hardcoded list would drift the moment the tool gained a field,
        and the new field would silently become displaceable again.

        The envelope half comes from `_envelope()` rather than from `ResponseEnvelope`:
        this module is deliberately stdlib + PyYAML only, so that a corpus image can smoke
        test it without the `mcp` extra installed, and importing the pydantic model here
        would break that.
        """
        return frozenset(self._envelope()) | {"id", "title"}

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
                # A RELATION NAME BECOMES A RESPONSE KEY VERBATIM (corpus-toolkit#105).
                #
                # `graph_neighbors` writes one key per relation, after the envelope, so a
                # relation named `corpus` overwrote that field with a list of neighbour
                # records — a hard ValidationError since #103 types it `str`, meaning the
                # tool stopped answering for that document. `id` and `title` were worse:
                # overwritten with no error at all, so a caller received a list where it
                # expected a document id.
                #
                # Same class as #102/#104, DIFFERENT REMEDY, and the difference is whose
                # data it is. Those merged a BACKEND's mapping over a response and were
                # fixed by re-asserting the framework's keys last: the backend had no
                # business setting them, so ignoring it costs nothing. A graph relation is
                # the corpus's OWN declared edge, and silently dropping it would be data
                # loss rather than enforcement — the author would never learn their
                # relationship had stopped being served. So this fails, and names what to
                # rename.
                #
                # DETECTED HERE, REPORTED BY `graph_neighbors`, and the split is the
                # point. This is the one place the graph is parsed, so the scan costs once
                # per corpus — but raising from here took down every tool that shares the
                # loader: `corpus_overview` (which the server's own instructions say to
                # call first), `resolve_citation` and `authority_chain`, none of which can
                # have a key displaced by a relation name. A caller of `corpus_overview`
                # got a crash about a file it never asked about, which is both response
                # convention 5's rule (an error names the condition that occurred) and the
                # shape of corpus-toolkit#4, where a corpus data problem surfaced as an
                # opaque tool error.
                #
                # So only the tool that would be WRONG declines to answer, and it declines
                # the way the other graph conditions already do: an explicit error response
                # carrying the envelope, not a raise. Note the graph still loads LAZILY, so
                # this is first observed on a graph-tool call rather than at boot; warming
                # it in `build_server` would cost ERF a ~26 MB parse at every start, which
                # is its own question and deliberately not taken here.
                self._reserved_clashes = sorted(
                    {rel for rels in edges.values() for rel in rels}
                    & self._reserved_response_keys())
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
            # ENVELOPE LAST, so the record cannot displace it (corpus-toolkit#102). This
            # branch used to merge `**rec` over the envelope with no re-assertion, so a
            # backend's error record carrying `corpus` renamed the corpus on the one
            # response an agent gets when it guesses an id wrong — the response it is most
            # likely to misread, and the failure #38 fixed, re-openable from the backend
            # side. A non-string was worse still: a hard ValidationError at serialization
            # since #103 declared these three `str`.
            #
            # Envelope-last rather than the success branch's re-assert-after because all
            # THREE fields are fixed here. Unlike the success branch there is no document,
            # so there is no `source_url` that could be a more precise
            # `authoritative_source` than the corpus's own — the record has nothing better
            # to offer and must not overwrite a declared `null`.
            return {**rec, **self._envelope(),
                    "did_you_mean": [{"id": s["id"], "title": s["title"]} for s in sug]}
        out = {**self._envelope(), **rec,
               "corpus": self.config.id, "archetype": self.config.archetype,
               "disclaimer": self.disclaimer}
        # PRECEDENCE, MOST PRECISE FIRST (corpus-toolkit#90): the record's own
        # `authoritative_source`, then the record's `source_url`, then the corpus front
        # door. Empty and absent are both "not supplied" at every step.
        #
        # RESOLVED FROM `rec`, NOT FROM `out`. That is the whole bug. The old code tested
        # `out.get("authoritative_source")`, but `**self._envelope()` has ALREADY put the
        # front door in that slot, so for any corpus declaring one the test could never be
        # true and the fallback could never fire — it fired only for a corpus that declared
        # no front door at all. Reading the assembled response to decide what the record
        # offered is the mistake; the record is the only thing that knows.
        #
        # FileBackend fills both keys from one column, so it could never expose this: the
        # built-in path was correct by accident of one backend's implementation rather than
        # because the framework enforced it. A corpus supplying `plugins.retrieval_module`
        # and honouring the documented `get()` contract ("Record metadata + body", which
        # nowhere requires `authoritative_source`) got the front door stamped over a
        # per-document URL sitting in the same payload — a wrong answer, not a missing one,
        # with nothing erroring.
        #
        # Enforced HERE rather than by widening `RetrievalBackend.get()`'s contract: pushing
        # a shared response-floor rule out to every corpus that writes a backend is the
        # arrangement this fallback exists to avoid.
        # STEP 2 IS TYPE-CHECKED AND STEP 1 IS NOT, deliberately.
        #
        # `authoritative_source` is declared `str | None`, and the protocol never types
        # `source_url` — a proxy backend may reasonably hold a list of mirrors there. If
        # this promoted such a value unchecked, a key that used to ride along harmlessly as
        # an undeclared extra (while the slot took the front door) would become a hard
        # ValidationError on every `get_document` for that corpus. That is the class the
        # preceding commit closed for the not-found branch and `corpus_overview`, and it
        # would have been reopened here by the fix for it.
        #
        # Step 1 keeps failing loudly on a non-string because the two are different acts. A
        # backend setting `authoritative_source` is ASSERTING "this is where the official
        # text lives", and a malformed assertion should surface at the point of the mistake
        # — MIGRATION.md has told backend authors so since #103. Step 2 is the framework
        # INFERRING from a key the backend never offered for this purpose, and an inference
        # has no standing to blow up the call: it declines and falls through.
        source_url = rec.get("source_url")
        out["authoritative_source"] = (rec.get("authoritative_source")
                                       or (source_url if isinstance(source_url, str)
                                           else None)
                                       or self.config.authoritative_source)
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
        if self._reserved_clashes:
            # A FOURTH GRAPH CONDITION, reported like the other three (corpus-toolkit#105).
            # A relation name becomes a response key verbatim here, so a graph declaring one
            # of the reserved names would overwrite this corpus's own answer -- silently for
            # `id`/`title`, and as a hard serialization error for the envelope fields.
            # Refusing is deliberate: the relation is the corpus's OWN declared edge, and
            # dropping it quietly would be data loss the author never learns about.
            return {**self._envelope(),
                    "error": (f"this corpus's graph declares relation type(s) "
                              f"{', '.join(repr(c) for c in self._reserved_clashes)}, "
                              f"which collide with response keys this tool writes"),
                    "note": (f"a relation name becomes a response key verbatim, so these "
                             f"would overwrite the answer. Reserved: "
                             f"{', '.join(sorted(self._reserved_response_keys()))}. Rename "
                             f"the relation in {self.config.graph_path} and rebuild the "
                             f"graph. Other tools are unaffected.")}
        out = {**self._envelope(), "id": doc_id, "title": node["title"]}
        for k, targets in edges.get(doc_id, {}).items():
            out[k] = self._neighbour_records(targets, nodes)
        return out

    def corpus_overview(self) -> dict:
        out = {
            **self.backend.overview(),
            # EVERYTHING THIS FRAMEWORK ASSERTS GOES AFTER THE BACKEND'S MAPPING
            # (corpus-toolkit#104). `overview()` is documented as "counts, commit/source
            # stamp" and nothing forbids these keys — a proxy backend naming its upstream
            # under `corpus`, or stamping its terms of use under `disclaimer`, is a
            # plausible mistake, and this is the tool a client calls FIRST to learn what it
            # is talking to. The `config_warning` branch below already reads the CONFIG
            # value rather than the merged one, so the old order was internally
            # inconsistent as well as unsafe.
            #
            # `disclaimer` and `jurisdiction` are here for the same reason and not by
            # afterthought: the first pass at this fix moved only `_envelope()` and left
            # those two in front, which let a backend delete the NON-AUTHORITATIVE warning
            # from the one tool response convention 4 names as carrying it — while
            # `get_document`'s docstring claims "a new backend cannot forget the
            # disclaimer". The rule is the whole assembled front, not three named keys.
            "jurisdiction": self.config.jurisdiction,
            "disclaimer": self.disclaimer,
            **self._envelope(),
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
                "convention 1 requires on every response — set it to this corpus's "
                "front door, the one page a reader opens to reach its official text; "
                "it need not cover every publisher this corpus draws on")
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

        # STRIPPED ONCE, AND THE STRIPPED VALUE IS WHAT IS USED. `"  slug  "` otherwise
        # missed the exact-match branch, fell into the substring fallback, matched nothing,
        # and was reported as a slug the registry does not contain -- about one it does.
        slug = str(slug_or_query).strip()
        if not slug:
            # AN EMPTY QUERY IS A MISSING ARGUMENT, not a wildcard and not a name fragment.
            # The fallback below is a substring match, and `"" in name` is true for EVERY
            # entry -- so on a registry holding one entry that was exactly one hit, the
            # uniqueness test passed, and this served a full profile for an agency nobody
            # named. Inverted with corpus size: silent on a small registry, and only loud on
            # a large one because everything matched (corpus-toolkit#122).
            return {**self._envelope(),
                    "error": ("no issuing body given. An empty query is not a wildcard — "
                              "pass a registry slug, or a fragment of a body's name.")}
        if slug not in entries:
            hits = [(s, m) for s, o in entries.items()
                    if (m := _name_match(o, self.config.issuing_body_name_fields, slug))]
            if len(hits) != 1:
                return {**self._envelope(),
                        "error": f"no unique issuing body match for {slug!r}",
                        "candidates": [{"slug": s, "name": entries[s].get("name"),
                                        "matched_field": field, "matched_name": name}
                                       for s, (field, name) in hits[:8]]}
            slug = hits[0][0]

        # Through the seam, not around it. This ran raw SQL against FileBackend's `docs`
        # table via ensure_index(), which is why the tool could not exist for any other
        # backend and why three separate guards were needed to keep that from surfacing as
        # a crash (corpus-toolkit#75).
        docs, attribution = self._holdings(self.backend.holdings_for(slug))
        return {
            **self._envelope(),
            "slug": slug,
            "registry": entries[slug],
            "curated": curated.get(slug, {}),
            "in_repo": docs or _NO_HOLDINGS[attribution["complete"]],
            # WHAT THE COUNT COULD SEE. `in_repo` is a count of documents attributed to
            # this body, and attribution is per-document: a corpus can hold documents
            # attributed to nobody, and those are counted for nobody. Serving the bare
            # number is how a 97% under-report read as a confident answer for as long as
            # it did (corpus-toolkit#71) — the number was populated, the call succeeded,
            # and nothing said it was a lower bound.
            "attribution": attribution,
            "disclaimer": self.disclaimer,
        }

    def documents_by_agency(self, slug: str, limit: int = 50, offset: int = 0) -> dict:
        """This corpus's documents for one registry slug (corpus-toolkit#46).

        Exists so `corpus-gateway` can assemble `agency_profile(slug)` by ASKING each
        corpus rather than duplicating every corpus's agency crosswalk. The crosswalks are
        per-consumer by design -- "the table lives in the consumer, correctness belongs to
        the registry" -- so a gateway that copied them would re-centralise what was
        deliberately distributed, and would go stale silently every time one changed.

        FOUR ANSWERS THAT MUST NOT COLLAPSE INTO EACH OTHER, because conflating any pair is
        the defect this platform files bugs about:

          * documents, and `attribution.complete` true -- the whole answer;
          * documents, and `complete` false -- a FLOOR: this corpus holds documents it
            attributed to nobody, so the agency may have more here;
          * none, and `complete` true -- this corpus genuinely holds nothing for it;
          * none, and `complete` null -- nobody measured. NOT the same as none.

        `slug_in_registry` answers a DIFFERENT question and is deliberately separate: a
        corpus with no registry cannot check whether the slug names a real agency, and says
        so with null rather than guessing. Requiring a registry to serve this tool at all
        would leave it unregistered on oregon-kpm and oregon-audits, which declare none and
        are two of the three corpora `agency_profile` needs.
        """
        # CLAMPED HERE, AND THE CLAMPED VALUES ARE WHAT THE RESPONSE ECHOES. SQLite reads a
        # negative LIMIT as unbounded, so `limit=-1` returned every match in one response --
        # 1,929 documents for ERF's Department of Environmental Quality -- while the
        # response still said `limit: -1`. `limit=0` returned an empty list that the
        # contract's table reads as "this corpus genuinely holds nothing". Every sibling in
        # `backends.py` clamps; this did not.
        limit = max(1, min(int(limit), _MAX_DOCUMENTS_PER_PAGE))
        offset = max(0, int(offset))
        # STRIPPED ONCE, AND THE STRIPPED VALUE IS WHAT IS USED. The empty-slug guard below
        # stripped for its emptiness test only, so a padded slug reached the backend
        # verbatim: zero documents and `slug_in_registry: false` -- "a typo, or an agency
        # that does not exist" -- about a slug the registry does contain.
        slug = str(slug).strip()
        # A SENTINEL IS NOT AN AGENCY. It is this corpus positively asserting "these
        # documents belong to NO body" (corpus-toolkit#94), so returning them as that
        # body's holdings hands back, on ERF, 37,991 documents as `statewide`'s complete
        # collection. `slug_in_registry: false` would be wrong too -- its own comment
        # defines that as "a typo, or an agency that does not exist", and this is neither.
        if slug in self.config.issuing_body_slug_sentinels:
            known = self.config.issuing_body_slugs
            return self.with_envelope({
                "slug": slug,
                # NULL WHERE NOTHING WAS CHECKED, like every other path. Hardcoding False
                # here contradicted the comment above it, this method's docstring, the
                # contract and the CHANGELOG -- and did so on exactly the registry-less
                # corpora this tool exists for, telling a gateway "checked, and the registry
                # does not contain it" from a corpus with nothing to check against.
                "slug_in_registry": None if known is None else slug in known,
                "error": (f"{slug!r} is a declared no-body sentinel for this corpus "
                          f"(plugins.issuing_body_slug_sentinels), not an agency slug. The "
                          f"documents carrying it are the ones this corpus attributes to NO "
                          f"issuing body, deliberately, so they are not any agency's "
                          f"holdings."),
                "documents": [], "total": 0, "returned": 0,
                "limit": limit, "offset": offset,
                "disclaimer": self.disclaimer,
            })
        # An empty slug matched the `''` written for every unattributed document, so the
        # same response returned them as an agency's AND counted them under
        # `documents_with_no_issuing_body`. One response, two contradictory claims.
        if not slug:
            return self.with_envelope({
                "slug": slug,
                "slug_in_registry": None,
                "error": ("no slug given. An empty slug is not a wildcard and is not the "
                          "unattributed documents — those are counted under "
                          "`attribution.documents_with_no_issuing_body` and belong to no "
                          "agency."),
                "documents": [], "total": 0, "returned": 0,
                "limit": limit, "offset": offset,
                "disclaimer": self.disclaimer,
            })
        raw = self.backend.documents_for_slug(slug, limit=limit, offset=offset)
        # NAMED, NOT KeyError. The registration gate checks the method EXISTS; a backend
        # implementing it with a different shape passes that and then dies on every call --
        # the registered-landmine outcome the gate was added for (corpus-toolkit#38), one
        # layer in. `_holdings` is defensive about the coverage block throughout and this
        # was not about the rest.
        # TYPES, NOT ONLY PRESENCE. Checking that the keys exist let `{"documents": 5}`
        # through, which then died on `len(5)` -- a TypeError naming no backend, i.e. the
        # unattributable failure this check exists to replace, reached by a shorter route.
        wanted = {"documents": (list, tuple), "total": int}
        bad = [k for k, t in wanted.items()
               if not isinstance(raw, dict) or not isinstance(raw.get(k), t)
               or isinstance(raw.get(k), bool)]
        if bad:
            raise TypeError(
                f"backend {self.backend.name!r} implements documents_for_slug but its "
                f"result is not the declared shape: "
                + "; ".join(f"{k} is {type(raw.get(k)).__name__ if isinstance(raw, dict) else type(raw).__name__}"
                            for k in bad)
                + ". The declared shape is {documents: [...], total: int, "
                  "coverage: {...}}. See RetrievalBackend.documents_for_slug.")
        _counts, attribution = self._holdings(raw)
        known = self.config.issuing_body_slugs
        return self.with_envelope({
            "slug": slug,
            # NULL, NOT FALSE, where there is no registry to ask. False means "checked, and
            # the registry does not contain it" -- a typo, or an agency that does not
            # exist. Null means the question was not asked. Reporting the second as the
            # first tells a caller its slug is wrong on every corpus that has no registry.
            "slug_in_registry": None if known is None else slug in known,
            "documents": raw["documents"],
            "total": raw["total"],
            "returned": len(raw["documents"]),
            "limit": limit,
            "offset": offset,
            # WHAT THE ANSWER COULD SEE. Same measure `issuing_body_profile` serves, from
            # the same backend coverage block, because a list of documents and a count of
            # documents are the same claim about the same corpus -- two measures would let
            # a caller read one as complete and the other as a floor.
            "attribution": attribution,
            "disclaimer": self.disclaimer,
        })

    # The counts `_holdings` needs before it will call coverage measured on the FOUR-BUCKET
    # path. Every one, as an int: a backend that reports some of them has measured some of
    # the question, and a partial measurement served as a complete one is the failure this
    # whole block exists to stop (corpus-toolkit#71).
    #
    # A corpus with no readable registry takes a different path with a smaller required set
    # — `in_registry`/`no_registry_entry` are the only two that need a registry, and are
    # omitted rather than guessed there (corpus-toolkit#46). That path is gated on the
    # CONFIG, not on which keys the backend happened to send, so a partial report still
    # lands here and is still refused.
    _COVERAGE_COUNTS = ("documents", "in_registry", "no_registry_entry", "unattributed")

    def _holdings(self, raw: dict) -> tuple[dict, dict]:
        """Split a backend's holdings answer into (counts, attribution).

        Accepts BOTH shapes `RetrievalBackend.holdings_for` has had: the current
        `{"counts": ..., "coverage": ...}` and v1.25.0's bare `{content_mode: count}`.

        `complete` has three values and they are not interchangeable. TRUE means every
        document in the corpus is counted for some registry body, so this count is the
        whole answer. FALSE means some are counted for none — they name a body the registry
        does not contain, or carry no attribution at all — so this count is a floor. NULL
        means nobody measured: a backend on the old shape, a backend that reported coverage
        without the counts, or an index holding nothing at all. Unknown is not none, and a
        half-measurement is not a measurement; collapsing those is the one thing CONTEXT.md
        says a new mechanism may not do.
        """
        counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else raw
        coverage = raw.get("coverage") if isinstance(raw.get("coverage"), dict) else None
        basis = (coverage or {}).get("basis") or "unknown"

        # `declared_no_body` is required only when this corpus DECLARES sentinels.
        #
        # A backend on the four-key shape is still reporting a complete measurement for a
        # corpus with no sentinels — every document it saw fell into one of the three
        # buckets, and the fourth would be zero. Demanding the key anyway would degrade
        # every existing custom backend to `complete: None` for no gain.
        #
        # Where sentinels ARE declared, the key is required and its absence is decisive:
        # such a backend counted every sentinel document as `no_registry_entry`, so its
        # split is not merely incomplete but WRONG, and `complete` would read False for a
        # corpus that is in fact fully attributed. "Could not check" is not "is not there"
        # (corpus-toolkit#94).
        required = self._COVERAGE_COUNTS
        if self.config.issuing_body_slug_sentinels:
            required = required + ("declared_no_body",)

        # NO REGISTRY IS A DIFFERENT SHAPE, NOT A MISSING MEASUREMENT. Only `in_registry`
        # and `no_registry_entry` need a registry to tell apart; whether a document carries
        # a slug at all does not. A backend reporting `documents` + `unattributed` without
        # the registry pair has measured the one fact that decides whether a per-slug answer
        # is a FLOOR, and reporting that as unknown discards it.
        #
        # GATED ON THE CORPUS, NOT ONLY ON THE SHAPE, and the first version was not. Asking
        # only "did the backend omit the registry pair?" made this fire for a corpus that
        # DOES have a registry against a backend that measured 2 of the 4 buckets: the
        # half-measurement was served as a measurement, the diagnostic naming what was
        # missing disappeared, and the note asserted a config fact this code had never
        # looked at. A half-measurement is not a measurement (CONTEXT.md).
        registry_pair = ("in_registry", "no_registry_entry")
        needed = ("documents", "unattributed") + tuple(
            k for k in required if k not in registry_pair
            and k not in ("documents", "unattributed"))
        if (self.config.issuing_body_slugs is None
                and coverage is not None
                and all(isinstance(coverage.get(k), int) for k in needed)
                and not any(isinstance(coverage.get(k), int) for k in registry_pair)):
            unattributed = coverage["unattributed"]
            in_corpus = coverage["documents"]
            _d = coverage.get("declared_no_body")
            # DECLARED-BUT-UNREADABLE IS NOT DECLARES-NONE. `issuing_body_slugs` answers
            # None to both, and collapsing them tells an operator their corpus is configured
            # the way they meant while its registry path is in fact broken -- absorbing the
            # last signal that anything is wrong into a positive statement about intent.
            why = ("this corpus declares no issuing-body registry, so whether a slug names "
                   "a real body was NOT checked here"
                   if not self.config.issuing_body_registry else
                   f"this corpus declares an issuing-body registry at "
                   f"{self.config.issuing_body_registry} but it could not be read, so "
                   f"whether a slug names a real body could NOT be checked — this is a "
                   f"broken configuration, not a corpus without a registry")
            if in_corpus == 0:
                # An empty index is not a fully attributed corpus. The four-bucket path says
                # so; dropping it here claimed "every document carries a slug" about a corpus
                # with none -- on exactly the configuration this branch was written for.
                tail = ("; and this corpus's index holds NO documents, so this is not a "
                        "measurement of attribution at all")
            elif unattributed:
                # FALSE IS PROVABLE WITHOUT A REGISTRY. Documents carrying no slug cannot
                # appear under any slug, so a non-zero count makes the answer a floor.
                tail = (f"; {unattributed} document(s) carry no slug at all and can appear "
                        f"under none, so this answer is a floor")
            else:
                # TRUE IS NOT PROVABLE. A mistyped slug is invisible without a registry to
                # check against -- which is exactly what `no_registry_entry` exists to
                # surface -- so completeness is unknown rather than yes.
                tail = ("; every document carries a slug, but a mistyped one would be "
                        "invisible without a registry, so whether this answer is complete "
                        "is UNKNOWN — not yes")
            return counts, {
                "complete": False if (in_corpus and unattributed) else None,
                "basis": basis,
                "documents_in_corpus": in_corpus,
                "documents_with_no_issuing_body": unattributed,
                "documents_declared_no_issuing_body": _d if isinstance(_d, int) else 0,
                "note": why + tail,
            }

        # A BACKEND CLAIMING REGISTRY BUCKETS AGAINST A CONFIG WITH NO REGISTRY IS A
        # DISAGREEMENT, and the strict path below would resolve it in the backend's favour
        # -- serving `complete: true` ("every document is matched to a registry entry") in
        # the same response as `slug_in_registry: null` ("there is no registry to ask").
        # Those cannot both hold. Newly reachable because `documents_by_agency` is
        # deliberately not registry-gated where `issuing_body_profile` is, so on main no
        # tool reached here without a registry.
        if (self.config.issuing_body_slugs is None and coverage is not None
                and any(isinstance(coverage.get(k), int) for k in registry_pair)):
            return counts, {
                "complete": None,
                "basis": basis,
                "note": (f"this corpus's backend ({self.backend.name}) reported documents "
                         f"matched against an issuing-body registry, but this corpus "
                         f"declares no readable one — so what those counts were measured "
                         f"against is UNKNOWN. Backend and config disagree; that is the "
                         f"finding, not a count."),
            }

        if coverage is None or not all(
                isinstance(coverage.get(k), int) for k in required):
            missing = "reports no attribution coverage" if coverage is None else (
                "reported attribution coverage without "
                + ", ".join(k for k in required
                            if not isinstance(coverage.get(k), int)))
            return counts, {
                "complete": None,
                "basis": basis,
                "note": (f"this corpus's backend ({self.backend.name}) {missing}, so "
                         "whether it holds documents this count could not see is "
                         "UNKNOWN — not none"),
            }

        total = coverage["documents"]
        matched, unmatched = coverage["in_registry"], coverage["no_registry_entry"]
        unattributed = coverage["unattributed"]
        # Absent means zero ONLY because the guard above already required this key wherever
        # its absence could hide something (i.e. wherever sentinels are declared).
        #
        # TYPE-CHECKED like its four siblings. Where sentinels are not declared this key is
        # not in `required`, so without this a backend's string or float would pass straight
        # into the response and into the prose note's arithmetic — the same "a value from a
        # backend reaches the response unchecked" asymmetry the envelope commits in this
        # stack closed. A non-int is treated as absent rather than raising: it cannot be
        # load-bearing here, since the guard above already covers every case where its value
        # could change an answer.
        _declared = coverage.get("declared_no_body")
        declared_none = _declared if isinstance(_declared, int) else 0
        out = {
            # A DECLARED SENTINEL IS RESOLVED, NOT A GAP (corpus-toolkit#94). It is the
            # corpus saying "this document belongs to no body", which is an answer; only an
            # UNDECLARED out-of-registry value and a missing value leave something
            # unaccounted for. Before sentinels existed, ERF's 37,991 deliberate
            # `statewide` documents made `complete` False permanently, for a reason that
            # was 99.997% legitimate.
            "complete": None if total == 0 else (unmatched == 0 and unattributed == 0),
            "basis": basis,
            "documents_in_corpus": total,
            "documents_matched_to_a_registry_entry": matched,
            # Its OWN field, never added to the matched count. "Counted for a registry body"
            # and "deliberately counted for no body" answer different questions, and folding
            # them would rebuild corpus-toolkit#71 one level up — a corpus calling itself
            # fully attributed while half of it reaches no per-body count.
            "documents_declared_no_issuing_body": declared_none,
            "documents_naming_no_registry_entry": unmatched,
            "documents_with_no_issuing_body": unattributed,
        }
        if total == 0:
            # An empty index is not a corpus where every document is attributed. It is a
            # corpus that answers nothing about anything, and platform-deploy's MIN_DOCS
            # abort exists because an empty index otherwise serves green.
            out["note"] = ("this corpus's index holds NO documents, so this is not a count "
                           "of zero for this body — it is no measurement at all; check "
                           "corpus_overview and the server's health before reading it")
        elif out["complete"]:
            declared = (f" ({declared_none} of them to no body, by this corpus's declared "
                        f"sentinels)" if declared_none else "")
            out["note"] = (f"every one of this corpus's {total} documents is accounted "
                           f"for{declared}, so this count is complete")
        else:
            gaps = []
            if unmatched:
                gaps.append(
                    f"{unmatched} {'names' if unmatched == 1 else 'name'} a value the "
                    "registry does not contain and this corpus has not declared as a "
                    "sentinel (so: a typo, or a deliberate value that should be added to "
                    "plugins.issuing_body_slug_sentinels)")
            if unattributed:
                gaps.append(f"{unattributed} "
                            f"{'carries' if unattributed == 1 else 'carry'} no "
                            "issuing-body attribution at all")
            # LEADS WITH THE GAP, NOT THE MATCHED PERCENTAGE (corpus-toolkit#94). The
            # `complete` branch above was updated to name the sentinel count and this one
            # was not, so a corpus declaring sentinels with ONE unexplained value read
            # "37913 of 75905 documents (50%) are attributed to a registry body" — the
            # 37,991 deliberate ones absent, and a caller concluding half the corpus was
            # unaccounted for when the real gap was one document. That is the message this
            # whole feature exists to remove, surviving in the branch that fires whenever a
            # single typo does.
            unaccounted = unmatched + unattributed
            accounted = (
                f"{matched} attributed to a registry body and {declared_none} declared to "
                f"belong to no issuing body" if declared_none
                else f"{matched} attributed to a registry body")
            out["note"] = (
                f"{unaccounted} of this corpus's {total} documents "
                f"({unaccounted / total:.1%}) are unaccounted for: " + " and ".join(gaps)
                + f". The other {total - unaccounted}: {accounted}. Those unaccounted for "
                "are counted for NO body, so every per-body count here is a LOWER BOUND, "
                "not a total")
        return counts, out
