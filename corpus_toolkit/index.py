#!/usr/bin/env python3
"""corpus-generate-index — regenerate `_meta/corpus-index.json`, the COMPACT
public lookup table a *sibling* corpus resolves citations against.

Why a separate artifact: sibling corpora cite each other constantly (a
records-retention corpus cites `OAR 166-300-0040`, which lives in the rules
corpus). A sibling can't read `_meta/graph.json` for that — it carries every
node's metadata plus every edge and runs to tens of megabytes. The index is
the minimum needed to turn an id into a citation: title, doc_type, and the
repo-relative path (which a sibling's `web_base` turns into a URL).

Shape:

    {"corpus": "<id>", "contract_version": 1, "n_documents": 3,
     "documents": {"<doc_id>": ["<title>", "<doc_type>", "<repo path>"], ...}}

Derived from `_meta/graph.json`'s nodes when that file exists (it already
carries id/title/doc_type/path), otherwise by walking the configured content
roots. Keys are emitted in sorted order so the file is byte-stable and
`--check` means something.

  --config PATH      path to _meta/corpus.yml (required)
  --output PATH      where to write, repo-relative (default _meta/corpus-index.json)
  --generated DATE   stamp an explicit `generated` date (omitted by default —
                     a wall-clock stamp would make --check fail every day)
  --check            exit 1 if the committed file is stale
"""
import argparse
import json
import sys

from corpus_toolkit import config as config_mod
from corpus_toolkit.repo import content_files, parse_frontmatter

DEFAULT_OUTPUT = "_meta/corpus-index.json"


def _from_graph(config) -> dict[str, list[str]] | None:
    """{id: [title, doc_type, path]} from _meta/graph.json, or None if that
    file is absent/unusable (nodes must carry a `path` to be useful here)."""
    path = config.graph_path
    if path is None or not path.is_file():
        return None
    try:
        g = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    docs = {}
    for n in g.get("nodes", []) or []:
        doc_id = n.get("id")
        if not doc_id or not n.get("path"):
            continue
        docs[doc_id] = [n.get("title", ""), n.get("doc_type", ""), str(n["path"])]
    return docs or None


def _from_content(config) -> dict[str, list[str]]:
    """{id: [title, doc_type, path]} by walking the content roots."""
    docs = {}
    for p in content_files(config):
        try:
            fm, _ = parse_frontmatter(p)
        except (ValueError, OSError):
            continue
        doc_id = fm.get("id")
        if not doc_id:
            continue
        rel = p.resolve().relative_to(config.root)
        docs[doc_id] = [fm.get("title", ""), fm.get("doc_type", ""), str(rel)]
    return docs


def build_index(config, generated: str | None = None) -> dict:
    """The index payload. `generated` is only ever set from an explicit
    caller-supplied date — never a wall clock, which would churn the file."""
    docs = _from_graph(config)
    if docs is None:
        docs = _from_content(config)
    payload = {
        "corpus": config.id,
        "contract_version": config.contract_version,
    }
    if generated:
        payload["generated"] = generated
    payload["n_documents"] = len(docs)
    payload["documents"] = {k: docs[k] for k in sorted(docs)}
    return payload


def _comparable(payload: dict) -> dict:
    """`generated` is metadata, not derived from the documents — ignore it when
    deciding whether a committed index is stale."""
    return {k: v for k, v in payload.items() if k != "generated"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="path to _meta/corpus.yml")
    ap.add_argument("--output", default=DEFAULT_OUTPUT,
                    help=f"path to write, relative to the repo root (default {DEFAULT_OUTPUT})")
    ap.add_argument("--generated", help="explicit YYYY-MM-DD to stamp as `generated`")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed --output file is stale")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    payload = build_index(config, args.generated)
    out_path = (config.root / args.output).resolve()

    if args.check:
        hint = (f"{args.output} is stale — run: corpus-generate-index "
                f"--config {args.config} --output {args.output}")
        if not out_path.is_file():
            print(f"{args.output} is missing — run: corpus-generate-index "
                  f"--config {args.config} --output {args.output}")
            sys.exit(1)
        try:
            current = json.loads(out_path.read_text())
        except ValueError:
            print(f"{args.output} is not valid JSON — {hint}")
            sys.exit(1)
        if _comparable(current) != _comparable(payload):
            n_now = len((current or {}).get("documents") or {})
            print(f"{hint}\n  committed: {n_now} document(s); "
                  f"generated: {payload['n_documents']} document(s)")
            sys.exit(1)
        print(f"{args.output} is up to date ({payload['n_documents']} documents).")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.output} ({payload['n_documents']} documents)")


if __name__ == "__main__":
    main()
