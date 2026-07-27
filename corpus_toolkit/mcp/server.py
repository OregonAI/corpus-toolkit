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
    args = ap.parse_args()

    config = config_mod.load(args.config)
    mcp = build_server(config)

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if args.public_hostname:
            from mcp.server.transport_security import TransportSecuritySettings
            mcp.settings.transport_security = TransportSecuritySettings(
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                              args.public_hostname],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*",
                                 "http://[::1]:*", f"https://{args.public_hostname}"],
            )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
