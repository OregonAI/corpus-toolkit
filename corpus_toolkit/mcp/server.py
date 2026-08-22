#!/usr/bin/env python3
"""corpus-mcp-serve — MCP server implementing docs/mcp-interface-contract.md
over corpus_toolkit.mcp.framework. Ported from oregon-policy-repo/src/mcp_server.py;
generic across every corpus (name/instructions/tools come from corpus.yml — no
Oregon-specific tool, wording, or citation logic here).

  corpus-mcp-serve --config _meta/corpus.yml                  # stdio
  corpus-mcp-serve --config _meta/corpus.yml --http           # streamable-HTTP
  corpus-mcp-serve --config _meta/corpus.yml --http --host 0.0.0.0 --port 8080
  corpus-mcp-serve --config _meta/corpus.yml --http --public-hostname mcp.example.com

Requires the `mcp` SDK: `pip install corpus-toolkit[mcp]`. The query engine
itself (framework.py) is stdlib-only and can be exercised without it.

Works against mcp 1.x AND 2.x. The SDK class moved (`FastMCP` -> `MCPServer`) and 2.0.0
deleted the old module outright, so everything version-dependent is behind
`corpus_toolkit.mcp._sdk` — read that file's header before changing anything here. The
corpus pins a toolkit tag but never pins the SDK, so the SDK floats at image-build time
no matter how carefully the pin is managed; spanning both majors is the only thing that
survives that."""
import argparse

import sys

from corpus_toolkit.mcp import _sdk

from corpus_toolkit import config as config_mod
from corpus_toolkit.mcp.framework import CorpusFramework
from corpus_toolkit.mcp.responses import ResponseEnvelope


# WHY OBJECT-SHAPED TOOLS ARE ANNOTATED `-> ResponseEnvelope` AND NOT A TypedDict, AND WHY
# THEY STILL RETURN PLAIN DICTS.
#
# Response convention 1 (`corpus`, `archetype`, `authoritative_source` on every
# object-shaped response) is declared, because it was invisible to field-level validation
# while `dict[str, Any]` emitted `{"additionalProperties": true}` and no properties at all
# (corpus-toolkit#15). It is declared as an OPEN pydantic model. Do not turn it into a
# TypedDict, and do not close it: v1.24.0 did the first, which does the second, and took
# all four live corpora down (corpus-toolkit#61).
#
# The generated model from a TypedDict does two things a shared response floor must never
# do:
#
#   1. `authoritative_source: str` REJECTS None — and None is the documented value for a
#      corpus that declares no source (docs/mcp-interface-contract.md convention 1;
#      framework.py emits it). `total=False` makes a key optional, not nullable, so
#      corpus_overview, resolve_citation and unknown-id get_document all became hard
#      ValidationErrors on every corpus at once.
#   2. Keys the model does not declare are DROPPED on the way out. get_document returned
#      its three envelope fields and no document body — the payload deleted at
#      serialization time, silently, with the call still reporting success.
#
# The v1.24.0 reasoning measured `additionalProperties` on the emitted schema (absent, so
# extras validate) and concluded extras were safe. That was the wrong layer: extras clear
# validation and are then discarded by the model that does the serializing. A schema check
# cannot see this — only a round-trip through `convert_result` can, which
# tests/test_output_schemas.py and tests/test_result_marshalling.py both pin.
#
# WHAT IS DIFFERENT ABOUT `ResponseEnvelope`: `extra="allow"`. `dict[str, Any]` was never
# "no model in the response path" — the SDK builds `RootModel[dict[str, Any]]` for it and
# dumps every response through that (measured, both majors). The envelope model is the same
# pass-through with three fields named on top, so the declaration describes the response
# instead of becoming it. See corpus_toolkit/mcp/responses.py for the measurements.
#
# THE TOOL BODIES STILL RETURN `fw.<tool>()`'s DICT. The annotation is a declaration to the
# SDK, not a constructor: `model_validate` takes the mapping. Returning model INSTANCES
# would move `_sdk.call_tool(convert_result=False)` and the release gate off the toolkit's
# own answer and onto the SDK's marshalling, which is the separation those two exist for.


# Tool names the MCP interface contract owns. A `plugins.tools_module` may not take one,
# whether or not this corpus happens to register it: `authority_chain` and
# `issuing_body_profile` are CONDITIONAL — on archetype and on the backend implementing
# `holdings_for` — so keying the check on what is present let a corpus claim the name today
# and turn fatal the day the condition changed, with no edit to its tools module
# (corpus-toolkit#111).
RESERVED_TOOL_NAMES = frozenset({
    "search_corpus", "get_document", "resolve_citation", "graph_neighbors",
    "corpus_overview", "authority_chain", "issuing_body_profile",
    # CONDITIONAL like the two above, and reserved for the same reason: keying the check on
    # what is present would let a corpus claim the name today and turn fatal the day the
    # condition changed, with no edit to its tools module (corpus-toolkit#111).
    "documents_by_agency",
})


def build_server(config):
    # A DECLARED ARCHETYPE IS A PROMISE ABOUT THE TOOL SURFACE, enforced here because it
    # was broken silently once: oregon-legislature declared `hybrid`, registered no
    # tools_module, started clean, and stamped `archetype: hybrid` on every response while
    # serving none of the hybrid extension tools — a client reading the archetype and
    # calling list_datasets got tool-not-found with nothing anywhere saying why
    # (corpus-toolkit#38, oregon-legislature#11). Same policy as the tools_module gate
    # below: refusing to start beats starting as something the corpus does not claim to be.
    if config.archetype in ("hybrid", "api") and not config.tools_module:
        raise RuntimeError(
            f"corpus.archetype is {config.archetype!r} but plugins.tools_module is not "
            f"declared, so none of the {config.archetype} extension tools "
            f"(list_datasets, query_dataset, ...) would exist. Declare the tools_module "
            f"that registers them, or declare the archetype this corpus actually serves "
            f"(corpus-toolkit#38; the live incident was oregon-legislature#11).")

    fw = CorpusFramework(config)
    # Warm the backend and REPORT what it found. ensure_index() was called directly here,
    # which (a) raised AttributeError for any backend without an FTS index, making the
    # non-file archetypes this seam exists for unstartable, and (b) warmed the cache
    # without ever looking at the result — so an empty or unsearchable corpus started
    # cleanly and answered "no results" to everything.
    _h = fw.backend.health()
    print(f"[corpus-mcp] {config.id}: {_h.get('detail', '')}", file=sys.stderr)
    if not _h.get("reachable"):
        print("[corpus-mcp] WARNING: backend reports itself UNREACHABLE — the server "
              "will start, but expect every query to return nothing.", file=sys.stderr)
    # SAID ONCE, TO THE OPERATOR, because the per-call answer only reaches the agent.
    #
    # A corpus that declares a registry it cannot read now STARTS and answers: every
    # body-shaped tool degrades to "could not check" rather than raising
    # (corpus-toolkit#136). That is the right answer for the caller and an invisible one
    # for the person who can fix the file, so the fault is named here too — the same
    # reasoning as the backend gate above, which validates its plug-in at startup
    # precisely so a fault does not first surface on some later query. It is a warning and
    # not a refusal: a broken registry costs this corpus one class of question, and
    # refusing to start would cost it every other one as well.
    if config.issuing_body_registry_fault:
        print(f"[corpus-mcp] WARNING: {config.issuing_body_registry_fault} — the server "
              f"will start, but issuing_body_profile can look up no body, and every tool "
              f"that reports whether a slug names a registry entry answers 'could not "
              f"check' instead. Fix the file and restart.", file=sys.stderr)
    # THE FILE BESIDE IT, ON THE SAME TERMS (corpus-toolkit#150). `issuing_body_profile`
    # reads two declared files, and after corpus-toolkit#143 an unreadable overlay was
    # reported only in that tool's per-call `curated_warning` — the one surface that
    # reaches the agent and never the operator. So a corpus deployed a malformed overlay,
    # served every body with an empty curated block, and nothing on the way in said so.
    #
    # A WARNING AND NOT A REFUSAL, for less reason than the registry even: the overlay is
    # the optional half of one tool's answer, so the corpus loses its curated notes and
    # keeps everything else. The GATE that refuses is `corpus-validate-frontmatter`, where
    # this same fault is an error; a running server can only mention.
    if config.issuing_body_profiles_fault:
        print(f"[corpus-mcp] WARNING: {config.issuing_body_profiles_fault} — the server "
              f"will start, and issuing_body_profile still answers with registry identity, "
              f"holdings and attribution, but no body gets its curated notes and every "
              f"answer says so. Fix the file and restart.", file=sys.stderr)
    # Log the SDK. The failure that motivated the compat seam is invisible in the
    # toolkit's own version number: same toolkit, different SDK major, unstartable server.
    _sdk.report()
    mcp = _sdk.Server(
        config.mcp_server_name,
        instructions=(
            f"Non-authoritative knowledge base for {config.name} ({config.jurisdiction}, "
            f"{config.archetype} archetype). Start with corpus_overview or resolve_citation; "
            "walk authority_chain for 'what requires/implements this' questions. ALWAYS cite "
            "each document's source_url when answering — this server is never the official "
            "text."))

    @mcp.tool()
    def search_corpus(query: str, doc_type: str = "", issuing_body: str = "",
                      limit: int = 10, mode: str = "hybrid") -> list[dict]:
        """Search the corpus. Returns ranked matches with snippets, never whole
        documents. Optional filters: doc_type, issuing_body. mode: 'hybrid'
        (default, keyword+semantic when this corpus has semantic search
        configured), 'keyword' (BM25 only), 'semantic' (vector only, falls
        back to keyword if unavailable).

        `issuing_body` accepts EITHER an issuing-body registry slug (the same
        identity issuing_body_profile and documents_by_agency take) OR the
        free-text `issuing_body` frontmatter string, which often names a
        sub-unit. A value that is a registry slug is filtered as one; anything
        else is filtered as frontmatter text. Every hit carries
        `issuing_body_filter` saying which matched, and a body-filtered search
        with no matches returns one `no_hits` record saying what was searched
        for — never a bare empty list, which cannot tell "this body has nothing
        here" from "you passed the other kind of string"."""
        return fw.search_corpus(query, doc_type or None, issuing_body or None, limit, mode)

    @mcp.tool()
    def get_document(doc_id: str, part: str = "auto") -> ResponseEnvelope:
        """Fetch one document by id, with provenance metadata and the
        non-authoritative disclaimer. Oversized documents return an at-a-glance
        summary plus a section list — pass part='<heading>' to page in content."""
        return fw.get_document(doc_id, part)

    @mcp.tool()
    def resolve_citation(citation: str) -> ResponseEnvelope:
        """Map a citation string to in-corpus document id(s) via the corpus's
        registered citation schemes, or an explicit `unresolved` result with the
        schemes attempted. Never guesses. Citations belonging to a sibling
        corpus resolve remotely: those matches carry `corpus` + `url` and the
        response is tagged `resolved_via: sibling:<id>`. If a sibling's index
        cannot be loaded the result is `unresolved` with `sibling_unavailable`
        set — that means "could not check", NOT "no such document"."""
        return fw.resolve_citation(citation)

    @mcp.tool()
    def graph_neighbors(doc_id: str) -> ResponseEnvelope:
        """All relationship edges of a document, grouped by type, one hop only."""
        return fw.graph_neighbors(doc_id)

    @mcp.tool()
    def corpus_overview() -> ResponseEnvelope:
        """What this corpus contains and does not: doc counts by type, archetype,
        contract version, and the non-authoritative disclaimer. Call this first."""
        return fw.corpus_overview()

    if config.archetype in ("document", "hybrid"):
        @mcp.tool()
        def authority_chain(doc_id: str, direction: str = "both", depth: int = 3) -> ResponseEnvelope:
            """Walk the authority graph: 'up' toward what authorizes this document,
            'down' toward what implements it, 'both' does both."""
            return fw.authority_chain(doc_id, direction, depth)

        # ...AND the backend must be able to answer it. Registering nothing is honest;
        # registering a landmine is not — a backend that cannot count holdings would give
        # a tool that raises on every call, the one configuration the release gate did not
        # cover (corpus-toolkit#38).
        #
        # The capability is `holdings_for`, an optional member of RetrievalBackend. This
        # used to test for `ensure_index` — FileBackend's private FTS connection — so the
        # gate asked "are you the file backend?" rather than "can you answer this?", and no
        # other backend could pass it at any price (corpus-toolkit#75).
        if config.issuing_body_registry and callable(
                getattr(fw.backend, "holdings_for", None)):
            @mcp.tool()
            def issuing_body_profile(slug_or_query: str) -> ResponseEnvelope:
                """Context about an issuing body: registry identity, curated notes,
                and what this corpus holds for it. Accepts a slug or name fragment."""
                return fw.issuing_body_profile(slug_or_query)
        elif config.issuing_body_registry:
            print(f"[corpus-mcp] {config.id}: issuing_body_profile NOT registered — the "
                  f"backend ({fw.backend.name}) implements no holdings_for(slug), so the "
                  f"tool would raise on every call. Implement it to serve this tool.",
                  file=sys.stderr)

    # NOT gated on a registry, unlike `issuing_body_profile` directly above.
    #
    # That tool needs one because it reports registry IDENTITY — name, statutory basis,
    # curated notes. This one answers "which of my documents carry this slug", which needs
    # no registry at all; whether the slug names a real body is a separate question it
    # reports as UNKNOWN where it cannot check (corpus-toolkit#46).
    #
    # The difference is load-bearing, not stylistic. oregon-kpm has its registry commented
    # out and oregon-audits states it has none — two of the three corpora `agency_profile`
    # must ask — so mirroring the gate above would leave this tool unregistered on exactly
    # the corpora it exists to serve, shipping the issue's letter and none of its purpose.
    #
    # Gated on the capability all the same: registering a tool that raises on every call is
    # the configuration the release gate did not cover (corpus-toolkit#38).
    if callable(getattr(fw.backend, "documents_for_slug", None)):
        @mcp.tool()
        def documents_by_agency(slug: str, limit: int = 50,
                                offset: int = 0) -> ResponseEnvelope:
            """This corpus's documents for one agency registry slug, with an explicit
            statement of how much of the corpus that answer could see."""
            return fw.documents_by_agency(slug, limit=limit, offset=offset)

    # Corpus-specific tools, registered LAST so they see a fully-built server. The seam
    # exists because the built-in seven are a closed set: a hybrid corpus needs tools keyed
    # on a dataset rather than a document id, and there was no way to express that short of
    # forking this file.
    #
    # THIS USED TO SAY extension tools "cannot be shadowed by a built-in added later",
    # which is true and beside the point: no built-in is added later. The direction that
    # bites is the reverse — a built-in ALREADY PRESENT wins the name, because both SDK
    # majors keep the first registration — and nothing addressed it until
    # corpus-toolkit#111. A comment that reassures about the harmless direction while the
    # dangerous one goes unmentioned is worse than no comment.
    #
    # A FAILURE HERE IS FATAL, DELIBERATELY. Catching the import and starting anyway
    # would produce a server that looks healthy, answers every built-in call correctly,
    # and is silently missing the tools the corpus was built to provide — the caller has
    # no way to tell "this corpus has no join_lookup" from "join_lookup failed to load".
    # Refusing to start is the only signal that reaches anyone.
    if config.tools_module:
        from corpus_toolkit.plugins import load_attr
        before = _sdk.tool_names(mcp)
        register = load_attr(config.tools_module, config.root)

        # RECORD WHAT THE MODULE ATTEMPTS, because the difference below cannot see a
        # collision (corpus-toolkit#111). Both SDK majors keep the EXISTING tool when a
        # name is registered twice, so a corpus tool named for a built-in is discarded and
        # the built-in answers in its place. The name was already in `before`, so it can
        # never appear in `added`, and the summary reported success while the corpus's tool
        # did not exist. Same outcome the block above refuses to allow for a module that
        # fails to load, reached by a different route.
        #
        # `tool()` has an identical signature on 1.28.1 and 2.0.0, so one wrapper serves
        # both; it is installed only for the duration of the hook.
        # WRAPPED AT `add_tool`, NOT `tool`. `add_tool` is public on both majors and is
        # what the `@mcp.tool()` decorator calls internally, so it is the single choke
        # point every registration passes through. Wrapping only the decorator left a
        # corpus free to call `mcp.add_tool(fn)` directly and walk past the guard —
        # corpus-toolkit#111 intact via a different public API.
        #
        # `add_tool(fn, name=None, ...)` is identical on 1.28.1 and 2.0.0, so one wrapper
        # serves both. It is installed only for the duration of the hook.
        attempted: list[str] = []
        original_add_tool = mcp.add_tool

        def _recording_add_tool(*args, **kwargs):
            fn = args[0] if args else kwargs.get("fn")
            # `name` is the first parameter AFTER fn, and a corpus may pass it
            # positionally: `@mcp.tool("corpus_overview")` reaches here as
            # add_tool(fn, "corpus_overview"). Reading only the keyword recorded the
            # FUNCTION's name instead, the check found nothing, and #111 survived its
            # own fix.
            declared = args[1] if len(args) > 1 else kwargs.get("name")
            attempted.append(declared or getattr(fn, "__name__", "?"))
            return original_add_tool(*args, **kwargs)

        mcp.add_tool = _recording_add_tool
        try:
            register(mcp, fw)
        finally:
            mcp.add_tool = original_add_tool

        # THREE WAYS A REGISTRATION CAN FAIL TO REACH THE SURFACE, all of them silent.
        seen: dict[str, int] = {}
        for name in attempted:
            seen[name] = seen.get(name, 0) + 1

        collided = sorted(n for n in seen if n in before)
        reserved = sorted(n for n in seen if n in RESERVED_TOOL_NAMES and n not in before)
        duplicated = sorted(n for n, count in seen.items() if count > 1)

        if collided or reserved or duplicated:
            parts = []
            if collided:
                parts.append(
                    f"{', '.join(collided)} — already registered by this server, so the "
                    f"SDK kept the built-in and DISCARDED the corpus's version")
            if reserved:
                parts.append(
                    f"{', '.join(reserved)} — reserved by the MCP interface contract. "
                    f"Not registered on THIS corpus (those tools are conditional), so it "
                    f"would have started clean and served corpus semantics under a core "
                    f"tool's name, then turned fatal the day the condition changed")
            if duplicated:
                parts.append(
                    f"{', '.join(duplicated)} — registered more than once by this module, "
                    f"so every copy after the first was discarded")
            raise RuntimeError(
                f"plugins.tools_module '{config.tools_module}' cannot register these "
                f"names:\n  " + "\n  ".join(parts) + "\n"
                f"Each is silent at runtime, which is why this refuses to start. Rename "
                f"them (corpus-toolkit#111).")

        added = sorted(_sdk.tool_names(mcp) - before)
        if not added:
            raise RuntimeError(
                f"plugins.tools_module '{config.tools_module}' registered no tools. "
                f"Declaring the hook and adding nothing is almost certainly a mistake — "
                f"remove the key, or register the tools it promises.")
        print(f"[corpus-mcp] {config.id}: +{len(added)} corpus tool(s): "
              f"{', '.join(added)}", file=sys.stderr)

    llms_txt_path = config.root / "llms.txt"
    if llms_txt_path.is_file():
        @mcp.resource("repo://llms.txt", name="Corpus index (llms.txt)",
                      description="Curated machine-readable index of every document.")
        def llms_txt() -> str:
            return llms_txt_path.read_text()

    return mcp


def build_arg_parser() -> argparse.ArgumentParser:
    """The parser `corpus-mcp-serve` runs on.

    EXPOSED SO THE ARGV CAN BE VALIDATED WITHOUT STARTING A SERVER (corpus-toolkit#116).
    The container starts from the template's `CMD`, not from `--help` -- and argparse answers
    `--help` with exit 0 whatever options exist, so renaming a flag left the unit suite, the
    entrypoints job and the release gate all green while every corpus crash-looped on
    `unrecognized arguments`. The gate now parses the extracted `CMD` argv through this.

    `main()` calls it rather than building its own: two parsers would drift, and the drift
    would be invisible -- the gate would be validating an argv `main` no longer accepts.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--http", action="store_true", help="streamable-HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--public-hostname", default="",
                    help="Host header to additionally allow (e.g. mcp.example.com) — "
                         "required behind a reverse proxy/tunnel that forwards a "
                         "different Host header than --host.")
    ap.add_argument("--allowed-origin", action="append", default=[],
                    metavar="ORIGIN",
                    help="browser Origin to accept, repeatable (e.g. https://claude.ai). "
                         "Without at least one, the server emits no CORS headers at all "
                         "and a browser-based MCP client cannot talk to it: the preflight "
                         "405s because OPTIONS is not a route, and even an allowed origin "
                         "gets no Access-Control-Allow-Origin header back. Must be exact "
                         "origins — '*' is refused, see the note where it is handled.")
    ap.add_argument("--path", default="/mcp",
                    help="URL path to mount the streamable-HTTP endpoint on (default "
                         "/mcp). Set this when several corpora share one hostname behind "
                         "a path-routing proxy: a Cloudflare Tunnel matches on path but "
                         "does NOT strip it, so the server must mount at the same prefix "
                         "the route matches (e.g. /oregon-legislature/mcp) or every "
                         "request 404s.")
    return ap


def main():
    ap = build_arg_parser()
    args = ap.parse_args()

    config = config_mod.load(args.config)
    mcp = build_server(config)

    if args.http:
        security = None
        if args.public_hostname or args.allowed_origin:
            # THE ORIGIN ALLOW-LIST AND CORS ARE TWO SEPARATE GATES, and a browser client
            # has to clear both. The SDK's transport security rejects an unlisted Origin
            # with 403 before any handler runs; CORS middleware then decides whether the
            # browser is allowed to READ the response. Passing --allowed-origin without
            # adding it here would 403 the preflight and never reach the middleware.
            # '*' IS REFUSED, not quietly downgraded. The SDK exact-matches Origin against
            # this list (the only wildcard it understands is a trailing ':*' on the PORT),
            # so a literal '*' entry would match nothing and 403 every browser — a flag
            # that reads as "allow all" while denying all. The only real "any origin"
            # switch is enable_dns_rebinding_protection=False, and that same flag also
            # disables the HOST check, which is what stops a tunnelled deployment from
            # 421-ing. Trading that away silently for a convenience flag is not a call to
            # make on the operator's behalf.
            if "*" in args.allowed_origin:
                sys.exit(
                    "ERROR: --allowed-origin '*' is not supported. The MCP SDK matches "
                    "Origin exactly, so '*' would reject every browser rather than accept "
                    "them; the only blanket switch also turns off the Host check that "
                    "keeps this server usable behind a proxy. List the origins you mean, "
                    "e.g. --allowed-origin https://claude.ai.")
            origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
            if args.public_hostname:
                origins.append(f"https://{args.public_hostname}")
            origins += args.allowed_origin
            hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
            if args.public_hostname:
                hosts.append(args.public_hostname)
            security = _sdk.TransportSecuritySettings(
                allowed_hosts=hosts, allowed_origins=origins,
            )
        # ONE dict, used for the verification build AND for run(). This is not tidiness.
        # On mcp 2.x, run() constructs its own app from its own arguments, so verifying a
        # separately-argued build would be a check on a different object than the one
        # served — it would pass while the served app mounted somewhere else. Deriving
        # both from the same dict removes the possibility.
        http = _sdk.http_kwargs(host=args.host, port=args.port, path=args.path,
                                transport_security=security)

        # Assert rather than assume, and assert on BEHAVIOUR rather than on the SDK's
        # internals. The previous version checked `hasattr(settings,
        # "streamable_http_path")`, which was a check on a 1.x implementation detail: mcp
        # 2.0 moved the mount to a run() kwarg and deleted the setting, so that guard
        # would have exited on a perfectly working SDK while a genuinely broken mount
        # went undetected. What actually matters is whether the app answers at the path
        # the proxy routes — a Cloudflare Tunnel matches on path but does NOT strip it,
        # so a wrong mount 404s every request with nothing in any log to explain it.
        app = _sdk.build_http_app(mcp, http)
        mounted = [getattr(r, "path", None) for r in app.routes]
        if args.path not in mounted:
            sys.exit(f"ERROR: asked to mount at {args.path!r} but the app exposes {mounted!r}. "
                     f"Refusing to start: behind a path-routing proxy this would 404 every "
                     f"request with no other symptom.")
        # Report what the session manager actually captured, not what was requested — the
        # two diverged once already. On 1.x the manager froze settings.transport_security
        # at the first app build and silently ignored anything set afterwards, which left
        # the allow-list at localhost-only and made every tunnelled request 421 while the
        # correct hostname sat unused in config. 2.x honours each build's own kwargs, so
        # the trap is gone there — the check stays anyway, because its purpose was never
        # to describe one SDK's caching but to refuse to serve a configuration that was
        # dropped somewhere between here and the socket.
        _hosts = _sdk.session_allowed_hosts(mcp)
        if args.public_hostname and args.public_hostname not in _hosts:
            sys.exit(f"ERROR: --public-hostname {args.public_hostname!r} did not reach the "
                     f"session manager (allowed_hosts={_hosts!r}). Refusing to start: every "
                     f"request through a proxy would 421 Invalid Host header.")
        # SERVE THE APP THAT WAS JUST VERIFIED, rather than letting the SDK build another.
        # On 2.x `run(transport="streamable-http")` constructs its own Starlette app, so
        # the mount assertion above was a check on a DIFFERENT object than the one served
        # — which is the entire reason http_kwargs is passed around as one dict. Serving
        # `app` directly removes that divergence instead of guarding against it, and it is
        # the only way to wrap middleware at all: the SDK exposes no hook (#37).
        served = app
        if args.allowed_origin:
            served = _sdk.with_cors(app, args.allowed_origin)
        print(f"[corpus-mcp] {config.id}: serving streamable-http at {args.path} "
              f"(allowed hosts: {', '.join(_hosts) or 'defaults'}; "
              f"cors: {', '.join(args.allowed_origin) or 'off'})",
              file=sys.stderr, flush=True)
        _sdk.run_http_app(mcp, served, http)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
