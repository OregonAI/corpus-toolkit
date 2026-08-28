#!/usr/bin/env python3
"""Cross-corpus citation resolution (toolkit v1.1.0): the `siblings:` config
block, `corpus_toolkit.remote`'s cached sibling-index loader,
`register_scheme(..., corpus=...)`, and `corpus-generate-index`.

Stdlib unittest only — the toolkit's runtime deps are pyyaml + jsonschema and
CI shouldn't need a test framework on top of that. Run with:

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit import config as config_mod          # noqa: E402
from corpus_toolkit import index as index_mod            # noqa: E402
from corpus_toolkit.config import Sibling                # noqa: E402
from corpus_toolkit.mcp.framework import (               # noqa: E402
    CorpusFramework, clear_schemes, register_scheme,
)
from corpus_toolkit.remote import load_sibling_index     # noqa: E402

CORPUS_YML = """\
corpus:
  id: records-retention
  name: Records Retention
  jurisdiction: oregon
  archetype: document
  schema_version: 1
  contract_version: 1
content_roots:
  - path: schedules
    doc_type: retention-schedule
"""

SIBLING_BLOCK = """\
siblings:
  - id: executive-regulatory-frameworks
    index_url: https://example.invalid/corpus-index.json
    web_base: https://github.com/OregonAI/executive-regulatory-frameworks/blob/main/
"""

DOC = """\
---
id: schedule-166-300
title: Retention Schedule 166-300
doc_type: retention-schedule
citation: Schedule 166-300
status: active
source_url: https://example.org/166-300
retrieved: 2026-01-01
content_mode: summary
---

## At a glance

Retention rules for state agency records, implementing OAR 166-300-0040.
"""

SIBLING_INDEX = {
    "corpus": "executive-regulatory-frameworks",
    "contract_version": 1,
    "n_documents": 1,
    "documents": {
        "oar-166-300-0040": ["OAR 166-300-0040 — Retention of Records",
                             "administrative-rule",
                             "rules/oar-166/oar-166-300-0040.md"],
    },
}


def make_corpus(root: Path, *, siblings: str = "", index_path: Path | None = None,
                index_url: str | None = None, graph: bool = True) -> Path:
    """A minimal on-disk corpus. Returns the path to _meta/corpus.yml."""
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "schedules").mkdir(parents=True, exist_ok=True)
    (root / "schedules" / "schedule-166-300.md").write_text(DOC)

    text = CORPUS_YML
    if siblings:
        text += siblings
    elif index_path or index_url:
        text += "siblings:\n  - id: executive-regulatory-frameworks\n"
        if index_path:
            text += f"    index_path: {index_path}\n"
        if index_url:
            text += f"    index_url: {index_url}\n"
        text += ("    web_base: https://github.com/OregonAI/"
                 "executive-regulatory-frameworks/blob/main/\n")
    cfg = root / "_meta" / "corpus.yml"
    cfg.write_text(text)

    if graph:
        (root / "_meta" / "graph.json").write_text(json.dumps({
            "nodes": [{"id": "schedule-166-300", "title": "Retention Schedule 166-300",
                       "doc_type": "retention-schedule",
                       "path": "schedules/schedule-166-300.md"}],
            "edges": [],
        }))
    return cfg


class CrossCorpusTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="corpus-xc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        clear_schemes()
        self.addCleanup(clear_schemes)

    def framework(self, cfg_path: Path) -> CorpusFramework:
        return CorpusFramework(config_mod.load(cfg_path))


class TestSiblingResolution(CrossCorpusTestCase):
    def test_local_fixture_index_resolves_with_corpus_and_url(self):
        idx = self.tmp / "sibling-index.json"
        idx.write_text(json.dumps(SIBLING_INDEX))
        cfg = make_corpus(self.tmp / "repo", index_path=idx)
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040")

        self.assertNotIn("unresolved", out)
        self.assertEqual(out["resolved_via"], "sibling:executive-regulatory-frameworks")
        self.assertEqual(len(out["matches"]), 1)
        hit = out["matches"][0]
        self.assertEqual(hit["id"], "oar-166-300-0040")
        self.assertEqual(hit["corpus"], "executive-regulatory-frameworks")
        self.assertEqual(hit["doc_type"], "administrative-rule")
        self.assertEqual(hit["url"], "https://github.com/OregonAI/"
                                     "executive-regulatory-frameworks/blob/main/"
                                     "rules/oar-166/oar-166-300-0040.md")
        self.assertNotIn("sibling_index_stale", out)

    def test_unreachable_sibling_is_unresolved_and_fabricates_nothing(self):
        # index_url points at an unresolvable host; no cache has ever been written.
        cfg = make_corpus(self.tmp / "repo",
                          index_url="https://nonexistent.invalid/corpus-index.json")
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040")

        self.assertTrue(out["unresolved"])
        self.assertEqual(out["matches"], [])
        self.assertEqual(out["sibling_unavailable"], "executive-regulatory-frameworks")
        self.assertIn("could not be loaded", out["note"])
        self.assertIn("NOT evidence the document is absent", out["note"])

    def test_sibling_available_but_document_absent_says_so(self):
        idx = self.tmp / "sibling-index.json"
        idx.write_text(json.dumps(SIBLING_INDEX))
        cfg = make_corpus(self.tmp / "repo", index_path=idx)
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 999-999-9999")

        self.assertTrue(out["unresolved"])
        self.assertNotIn("sibling_unavailable", out)     # we DID look; it isn't there
        self.assertIn("holds no document", out["note"])

    def test_illegal_candidate_id_is_a_scheme_bug_not_an_absent_document(self):
        """The distinction this whole class turns on.

        `test_sibling_available_but_document_absent_says_so` above is the true absence:
        we looked, the sibling answered, the document is not there. THIS is a different
        thing wearing the same clothes — the scheme built an id outside the frontmatter
        schema's charset (`^[a-z0-9][a-z0-9._-]+$`), so nothing could have matched no
        matter what the sibling holds.

        Told apart only by the note, and told apart matters: reported as an absence, two
        corpora filed coverage-gap issues against a sibling holding every document they
        claimed was missing. `ors-{num}` templated straight from citation text produced
        `ors-279A.010` while the sibling held `ors-279a.010`, for all 37 lettered ORS
        chapters."""
        idx = self.tmp / "sibling-index.json"
        idx.write_text(json.dumps(SIBLING_INDEX))
        cfg = make_corpus(self.tmp / "repo", index_path=idx)
        register_scheme("oar-rule", r"OAR\s+(?P<num>[\dA-Za-z-]+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040X")

        self.assertTrue(out["unresolved"])
        self.assertEqual(out["matches"], [])
        self.assertIn("citation-scheme bug", out["note"])
        self.assertIn("oar-166-300-0040X", out["note"])   # names the id it built
        self.assertIn("oar-rule", out["note"])            # and the scheme that built it
        # Must NOT read as a coverage gap — that phrasing is what sent the issues.
        self.assertNotIn("holds no document", out["note"])

    def test_legal_lowercase_candidate_still_resolves_normally(self):
        """The guard drops only ids that could never match. Same scheme, legal id."""
        idx = self.tmp / "sibling-index.json"
        idx.write_text(json.dumps(SIBLING_INDEX))
        cfg = make_corpus(self.tmp / "repo", index_path=idx)
        register_scheme("oar-rule", r"OAR\s+(?P<num>[\dA-Za-z-]+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040")

        self.assertNotIn("unresolved", out)
        self.assertEqual(out["matches"][0]["id"], "oar-166-300-0040")
        self.assertIsNone(out.get("note"))

    def test_suspended_sibling_document_is_surfaced_not_flattened_to_superseded(self):
        """Acceptance criterion (corpus-toolkit#159): `suspended` travels the same
        4th-element path `superseded` always did, and the not-current rendering in
        `_resolve_in_sibling` already treats it correctly without change -- confirmed
        here rather than assumed. Without the enum value, a corpus holding a suspended
        rule had no truthful way to populate this row in the first place; this is what
        a sibling resolving into one now sees."""
        idx = self.tmp / "sibling-index.json"
        suspended_index = json.loads(json.dumps(SIBLING_INDEX))
        suspended_index["documents"]["oar-166-300-0040"].append("suspended")
        idx.write_text(json.dumps(suspended_index))
        cfg = make_corpus(self.tmp / "repo", index_path=idx)
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040")

        hit = out["matches"][0]
        self.assertEqual(hit["status"], "suspended")
        self.assertIn("SUSPENDED — not current text", hit["note"])
        self.assertNotIn("superseded", hit["note"].lower())

    def test_scheme_names_an_undeclared_sibling(self):
        cfg = make_corpus(self.tmp / "repo")             # no siblings: block at all
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040")

        self.assertTrue(out["unresolved"])
        self.assertEqual(out["sibling_unavailable"], "executive-regulatory-frameworks")
        self.assertIn("siblings", out["note"])

    def test_stale_cache_is_served_when_the_fetch_fails(self):
        cache = self.tmp / "cache"
        cache.mkdir()
        cached = cache / "executive-regulatory-frameworks.json"
        cached.write_text(json.dumps(SIBLING_INDEX))
        old = time.time() - 10 * 86400
        os.utime(cached, (old, old))                     # older than the 1-day ttl
        sib = Sibling(id="executive-regulatory-frameworks",
                      index_url="https://nonexistent.invalid/corpus-index.json",
                      web_base="https://example.org/blob/main/")

        got = load_sibling_index(sib, cache)

        self.assertIsNotNone(got)
        self.assertTrue(got["_stale"])
        self.assertEqual(got["_source"], "cache-stale")
        self.assertIn("oar-166-300-0040", got["documents"])

    def test_stale_cache_surfaces_on_the_resolve_response(self):
        repo = self.tmp / "repo"
        cfg = make_corpus(repo, index_url="https://nonexistent.invalid/corpus-index.json")
        cache = repo / "_meta" / ".cache" / "siblings"
        cache.mkdir(parents=True)
        cached = cache / "executive-regulatory-frameworks.json"
        cached.write_text(json.dumps(SIBLING_INDEX))
        old = time.time() - 10 * 86400
        os.utime(cached, (old, old))
        register_scheme("oar-rule", r"OAR\s+(?P<num>\d+-\d+-\d+)", "oar-{num}",
                        corpus="executive-regulatory-frameworks")

        out = self.framework(cfg).resolve_citation("OAR 166-300-0040")

        self.assertEqual(len(out["matches"]), 1)
        self.assertTrue(out["sibling_index_stale"])
        self.assertIn("STALE", out["note"])

    def test_fresh_cache_is_served_without_a_fetch(self):
        cache = self.tmp / "cache"
        cache.mkdir()
        (cache / "executive-regulatory-frameworks.json").write_text(json.dumps(SIBLING_INDEX))
        sib = Sibling(id="executive-regulatory-frameworks",
                      index_url="https://nonexistent.invalid/corpus-index.json")

        got = load_sibling_index(sib, cache)

        self.assertEqual(got["_source"], "cache")
        self.assertNotIn("_stale", got)

    def test_http_fetch_populates_the_cache(self):
        """The real urllib path, against a loopback server — no external network."""
        import http.server
        import threading

        body = json.dumps(SIBLING_INDEX).encode()
        seen_agents = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen_agents.append(self.headers.get("User-Agent", ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/corpus-index.json"
        cache = self.tmp / "cache"
        sib = Sibling(id="executive-regulatory-frameworks", index_url=url)

        got = load_sibling_index(sib, cache)

        self.assertEqual(got["_source"], "fetch")
        self.assertIn("oar-166-300-0040", got["documents"])
        self.assertTrue((cache / "executive-regulatory-frameworks.json").is_file())
        self.assertIn("corpus-toolkit", seen_agents[0])
        # second call is served from the fresh cache, not the server
        self.assertEqual(load_sibling_index(sib, cache)["_source"], "cache")
        self.assertEqual(len(seen_agents), 1)
        # ttl=0 forces a refetch
        self.assertEqual(load_sibling_index(sib, cache, ttl_seconds=0)["_source"], "fetch")

    def test_gzip_is_requested_and_decompressed(self):
        """A corpus index is ~5x smaller gzipped (7.22 MiB -> 1.36 MiB for the real
        executive-regulatory-frameworks index), and GitHub serves it compressed. urllib
        neither requests nor decompresses gzip on its own, so without an explicit header
        and an explicit decompress every cache miss pulls the full uncompressed file.
        This asserts both halves: that we ask, and that we can read the answer."""
        import gzip as gziplib
        import http.server
        import threading

        raw = json.dumps(SIBLING_INDEX).encode()
        packed = gziplib.compress(raw)
        seen_encoding = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen_encoding.append(self.headers.get("Accept-Encoding", ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(packed)))
                self.end_headers()
                self.wfile.write(packed)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        sib = Sibling(id="executive-regulatory-frameworks",
                      index_url=f"http://127.0.0.1:{srv.server_address[1]}/i.json")

        got = load_sibling_index(sib, self.tmp / "cache")

        self.assertIn("gzip", seen_encoding[0])          # we asked
        self.assertEqual(got["_source"], "fetch")
        self.assertIn("oar-166-300-0040", got["documents"])   # and we could read it
        self.assertLess(len(packed), len(raw))

    def test_uncompressed_response_still_works_when_gzip_is_ignored(self):
        """Content-Encoding is advisory — a server may ignore the request header. The
        loader must branch on what actually arrived, not on what it asked for."""
        import http.server
        import threading

        body = json.dumps(SIBLING_INDEX).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        sib = Sibling(id="executive-regulatory-frameworks",
                      index_url=f"http://127.0.0.1:{srv.server_address[1]}/i.json")

        got = load_sibling_index(sib, self.tmp / "cache")
        self.assertEqual(got["_source"], "fetch")
        self.assertIn("oar-166-300-0040", got["documents"])

    def test_malformed_index_is_treated_as_unavailable(self):
        bad = self.tmp / "bad-index.json"
        bad.write_text('{"corpus": "x", "documents": "not-a-dict"}')
        sib = Sibling(id="executive-regulatory-frameworks", index_path=bad)

        self.assertIsNone(load_sibling_index(sib, self.tmp / "cache"))

    def test_loader_never_raises_on_a_garbage_sibling(self):
        sib = Sibling(id="x", index_url="not-a-url://///")
        self.assertIsNone(load_sibling_index(sib, self.tmp / "nonexistent-cache"))


class TestBackwardCompatibility(CrossCorpusTestCase):
    def test_no_siblings_config_resolves_locally_exactly_as_before(self):
        cfg = make_corpus(self.tmp / "repo")
        register_scheme("retention-schedule", r"Schedule\s+(?P<num>[\d-]+)", "schedule-{num}")

        config = config_mod.load(cfg)
        self.assertEqual(config.siblings, [])

        out = CorpusFramework(config).resolve_citation("Schedule 166-300")

        self.assertEqual(out["matches"], [{"id": "schedule-166-300",
                                           "title": "Retention Schedule 166-300",
                                           "doc_type": "retention-schedule"}])
        self.assertNotIn("resolved_via", out)
        self.assertNotIn("sibling_unavailable", out)
        self.assertNotIn("sibling_index_stale", out)
        self.assertNotIn("unresolved", out)

    def test_local_miss_keeps_the_original_unresolved_message(self):
        cfg = make_corpus(self.tmp / "repo")
        register_scheme("retention-schedule", r"Schedule\s+(?P<num>[\d-]+)", "schedule-{num}")

        out = self.framework(cfg).resolve_citation("Schedule 999-999")

        self.assertTrue(out["unresolved"])
        self.assertEqual(out["note"],
                         "scheme 'retention-schedule' matched but no such document exists")
        self.assertEqual(out["schemes_attempted"], ["retention-schedule"])
        self.assertIn("search_fallback", out)

    def test_unrecognized_format_is_unchanged(self):
        cfg = make_corpus(self.tmp / "repo")
        register_scheme("retention-schedule", r"Schedule\s+(?P<num>[\d-]+)", "schedule-{num}")

        out = self.framework(cfg).resolve_citation("something else entirely")

        self.assertTrue(out["unresolved"])
        self.assertIn("no citation scheme recognized", out["note"])

    def test_existing_resolver_signatures_still_work(self):
        cfg = make_corpus(self.tmp / "repo")
        register_scheme("two-arg", r"^ARG2 (.+)$",
                        resolver=lambda m, nodes: ([n for n in nodes], "note-from-resolver"))
        out = self.framework(cfg).resolve_citation("ARG2 anything")
        self.assertEqual(out["matches"][0]["id"], "schedule-166-300")
        self.assertEqual(out["note"], "note-from-resolver")


class TestGenerateIndex(CrossCorpusTestCase):
    def run_cli(self, *argv):
        old = sys.argv
        sys.argv = ["corpus-generate-index", *argv]
        try:
            index_mod.main()
            return 0
        except SystemExit as e:
            return e.code or 0
        finally:
            sys.argv = old

    def test_build_from_graph_and_check_detects_staleness(self):
        repo = self.tmp / "repo"
        cfg = make_corpus(repo)
        out_rel = "_meta/corpus-index.json"

        self.assertEqual(self.run_cli("--config", str(cfg), "--output", out_rel, "--check"), 1,
                         "missing index must fail --check")

        self.assertEqual(self.run_cli("--config", str(cfg), "--output", out_rel), 0)
        written = json.loads((repo / out_rel).read_text())
        self.assertEqual(written["corpus"], "records-retention")
        self.assertEqual(written["n_documents"], 1)
        # 4th element (v1.19.0): status. "" here because this fixture's graph nodes
        # predate status emission — and "" must read as UNKNOWN, never as current.
        self.assertEqual(written["documents"]["schedule-166-300"],
                         ["Retention Schedule 166-300", "retention-schedule",
                          "schedules/schedule-166-300.md", ""])
        self.assertNotIn("generated", written)      # no wall-clock stamp
        self.assertEqual(self.run_cli("--config", str(cfg), "--output", out_rel, "--check"), 0)

        # a new document makes the committed index stale
        (repo / "schedules" / "schedule-166-400.md").write_text(
            DOC.replace("166-300", "166-400"))
        (repo / "_meta" / "graph.json").unlink()    # force the content-walk path
        self.assertEqual(self.run_cli("--config", str(cfg), "--output", out_rel, "--check"), 1)

        self.assertEqual(self.run_cli("--config", str(cfg), "--output", out_rel), 0)
        self.assertEqual(self.run_cli("--config", str(cfg), "--output", out_rel, "--check"), 0)
        written = json.loads((repo / out_rel).read_text())
        self.assertEqual(written["n_documents"], 2)

    def test_status_flows_into_the_index_row_unchanged(self):
        """Acceptance criterion (corpus-toolkit#159): `status` already travels as an
        opaque string on the index row's 4th element -- confirmed here rather than
        assumed, so a sibling resolving into a suspended document learns that via this
        row instead of being told `superseded`. `build_index` never inspects the
        enum; a schema-illegal value would flow through identically, which is the
        point -- the toolkit is not the place that closes the enum, the schema is."""
        repo = self.tmp / "repo"
        cfg = make_corpus(repo, graph=False)
        (repo / "schedules" / "schedule-166-300.md").write_text(
            DOC.replace("status: active", "status: suspended"))
        config = config_mod.load(cfg)

        written = index_mod.build_index(config)

        self.assertEqual(written["documents"]["schedule-166-300"],
                         ["Retention Schedule 166-300", "retention-schedule",
                          "schedules/schedule-166-300.md", "suspended"])

    def test_output_is_byte_stable_and_sorted(self):
        repo = self.tmp / "repo"
        cfg = make_corpus(repo, graph=False)
        for n in ("166-900", "166-100", "166-500"):
            (repo / "schedules" / f"schedule-{n}.md").write_text(DOC.replace("166-300", n))
        config = config_mod.load(cfg)

        first = index_mod.build_index(config)
        second = index_mod.build_index(config)

        self.assertEqual(first, second)
        self.assertEqual(list(first["documents"]), sorted(first["documents"]))

    def test_explicit_generated_flag_is_ignored_by_check(self):
        repo = self.tmp / "repo"
        cfg = make_corpus(repo)
        self.assertEqual(self.run_cli("--config", str(cfg), "--generated", "2026-07-25"), 0)
        written = json.loads((repo / "_meta" / "corpus-index.json").read_text())
        self.assertEqual(written["generated"], "2026-07-25")
        # a --check run without the flag must not call the file stale over that field
        self.assertEqual(self.run_cli("--config", str(cfg), "--check"), 0)


class TestConfig(CrossCorpusTestCase):
    def test_siblings_block_parses(self):
        cfg = make_corpus(self.tmp / "repo", siblings=SIBLING_BLOCK)
        config = config_mod.load(cfg)
        self.assertEqual(len(config.siblings), 1)
        sib = config.siblings[0]
        self.assertEqual(sib.id, "executive-regulatory-frameworks")
        self.assertEqual(sib.index_url,
                         "https://example.invalid/corpus-index.json")
        self.assertTrue(sib.web_base.endswith("/blob/main/"))
        self.assertIsNone(sib.index_path)
        self.assertIs(config.sibling("executive-regulatory-frameworks"), sib)
        self.assertIsNone(config.sibling("nope"))

    def test_index_path_wins_over_index_url(self):
        idx = self.tmp / "sibling-index.json"
        idx.write_text(json.dumps(SIBLING_INDEX))
        cfg = make_corpus(self.tmp / "repo", index_path=idx,
                          index_url="https://nonexistent.invalid/corpus-index.json")
        sib = config_mod.load(cfg).siblings[0]
        got = load_sibling_index(sib, self.tmp / "cache")
        self.assertEqual(got["_source"], "local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
