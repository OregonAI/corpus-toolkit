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
itself (framework.py) is stdlib-only and can be exercised without it."""
import argparse

import sys

from mcp.server.fastmcp import FastMCP

from corpus_toolkit import config as config_mod
from corpus_toolkit.mcp.framework import CorpusFramework


def build_server(config) -> FastMCP:
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
    mcp = FastMCP(
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
        back to keyword if unavailable)."""
        return fw.search_corpus(query, doc_type or None, issuing_body or None, limit, mode)

    @mcp.tool()
    def get_document(doc_id: str, part: str = "auto") -> dict:
        """Fetch one document by id, with provenance metadata and the
        non-authoritative disclaimer. Oversized documents return an at-a-glance
        summary plus a section list — pass part='<heading>' to page in content."""
        return fw.get_document(doc_id, part)

    @mcp.tool()
    def resolve_citation(citation: str) -> dict:
        """Map a citation string to in-corpus document id(s) via the corpus's
        registered citation schemes, or an explicit `unresolved` result with the
        schemes attempted. Never guesses. Citations belonging to a sibling
        corpus resolve remotely: those matches carry `corpus` + `url` and the
        response is tagged `resolved_via: sibling:<id>`. If a sibling's index
        cannot be loaded the result is `unresolved` with `sibling_unavailable`
        set — that means "could not check", NOT "no such document"."""
        return fw.resolve_citation(citation)

    @mcp.tool()
    def graph_neighbors(doc_id: str) -> dict:
        """All relationship edges of a document, grouped by type, one hop only."""
        return fw.graph_neighbors(doc_id)

    @mcp.tool()
    def corpus_overview() -> dict:
        """What this corpus contains and does not: doc counts by type, archetype,
        contract version, and the non-authoritative disclaimer. Call this first."""
        return fw.corpus_overview()

    if config.archetype in ("document", "hybrid"):
        @mcp.tool()
        def authority_chain(doc_id: str, direction: str = "both", depth: int = 3) -> dict:
            """Walk the authority graph: 'up' toward what authorizes this document,
            'down' toward what implements it, 'both' does both."""
            return fw.authority_chain(doc_id, direction, depth)

        if config.issuing_body_registry:
            @mcp.tool()
            def issuing_body_profile(slug_or_query: str) -> dict:
                """Context about an issuing body: registry identity, curated notes,
                and what this corpus holds for it. Accepts a slug or name fragment."""
                return fw.issuing_body_profile(slug_or_query)

    # Corpus-specific tools, registered LAST so they see a fully-built server and cannot
    # be shadowed by a built-in added later. The seam exists because the built-in seven
    # are a closed set: a hybrid corpus needs tools keyed on a dataset rather than a
    # document id, and there was no way to express that short of forking this file.
    #
    # A FAILURE HERE IS FATAL, DELIBERATELY. Catching the import and starting anyway
    # would produce a server that looks healthy, answers every built-in call correctly,
    # and is silently missing the tools the corpus was built to provide — the caller has
    # no way to tell "this corpus has no join_lookup" from "join_lookup failed to load".
    # Refusing to start is the only signal that reaches anyone.
    if config.tools_module:
        from corpus_toolkit.plugins import load_attr
        # FastMCP.list_tools is a coroutine; the tool manager's is not. Using the sync one
        # keeps this out of an event loop for what is only a before/after name diff.
        def _tool_names():
            return {t.name for t in mcp._tool_manager.list_tools()}

        before = _tool_names()
        register = load_attr(config.tools_module, config.root)
        register(mcp, fw)
        added = sorted(_tool_names() - before)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--http", action="store_true", help="streamable-HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--public-hostname", default="",
                    help="Host header to additionally allow (e.g. mcp.example.com) — "
                         "required behind a reverse proxy/tunnel that forwards a "
                         "different Host header than --host.")
    ap.add_argument("--path", default="/mcp",
                    help="URL path to mount the streamable-HTTP endpoint on (default "
                         "/mcp). Set this when several corpora share one hostname behind "
                         "a path-routing proxy: a Cloudflare Tunnel matches on path but "
                         "does NOT strip it, so the server must mount at the same prefix "
                         "the route matches (e.g. /oregon-legislature/mcp) or every "
                         "request 404s.")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    mcp = build_server(config)

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # Assert rather than assume. This is a settings field on the MCP SDK's FastMCP, and
        # the SDK has been reshaping this area (FastMCP -> MCPServer; the mount has appeared
        # as both a setting and a run() kwarg). If a future version stops honouring it, the
        # server would start happily and serve at /mcp while the proxy routes /<corpus>/mcp
        # — every request 404s with nothing in any log to explain it. Fail at startup instead.
        if not hasattr(mcp.settings, "streamable_http_path"):
            sys.exit("ERROR: this mcp SDK has no settings.streamable_http_path, so --path "
                     "cannot be honoured. Pin an SDK that supports it, or drop --path and "
                     "give this corpus its own hostname.")
        mcp.settings.streamable_http_path = args.path

        # EVERY setting must be applied before the first streamable_http_app() call below.
        # That call creates FastMCP's session manager, which captures
        # settings.transport_security AT THAT MOMENT and is then cached for the process —
        # so anything set afterwards is silently ignored. Setting --public-hostname after
        # the mount check left the allow-list at localhost-only and made every tunnelled
        # request 421, with the correct value sitting unused in settings.
        if args.public_hostname:
            from mcp.server.transport_security import TransportSecuritySettings
            mcp.settings.transport_security = TransportSecuritySettings(
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                              args.public_hostname],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*",
                                 "http://[::1]:*", f"https://{args.public_hostname}"],
            )

        # First build: validates the mount AND freezes the settings above into the session
        # manager that run() will reuse.
        mounted = [getattr(r, "path", None) for r in mcp.streamable_http_app().routes]
        if args.path not in mounted:
            sys.exit(f"ERROR: asked to mount at {args.path!r} but the app exposes {mounted!r}. "
                     f"Refusing to start: behind a path-routing proxy this would 404 every "
                     f"request with no other symptom.")
        # Report what the session manager actually captured, not what was requested — the
        # two diverged once already.
        _sec = getattr(mcp._session_manager, "security_settings", None)
        _hosts = getattr(_sec, "allowed_hosts", []) if _sec else []
        if args.public_hostname and args.public_hostname not in _hosts:
            sys.exit(f"ERROR: --public-hostname {args.public_hostname!r} did not reach the "
                     f"session manager (allowed_hosts={_hosts!r}). Refusing to start: every "
                     f"request through a proxy would 421 Invalid Host header.")
        print(f"[corpus-mcp] {config.id}: serving streamable-http at {args.path} "
              f"(allowed hosts: {', '.join(_hosts) or 'defaults'})",
              file=sys.stderr, flush=True)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
