"""The snapshot recorder, the baseline recorder and the document writer, on a real corpus on
disk (ADR-0016). Together they are what an ingester calls after `Fetcher.get`."""
import textwrap
from pathlib import Path

import pytest
import yaml

from corpus_toolkit import config as config_mod
from corpus_toolkit import documents
from corpus_toolkit.repo import content_hash, hash_snapshot, parse_frontmatter
from corpus_toolkit.sources import manifest, snapshots

CORPUS_YML = """\
schema_version: 1
corpus:
  id: test-corpus
  name: Test Corpus
  jurisdiction: oregon
  archetype: document
content_roots:
  - path: "documents"
    doc_type: rule
source_manifest_path: _meta/sources
snapshot_dir: _meta/snapshots
disclaimer_marker: "NON-AUTHORITATIVE"
"""

GROUP_YML = """\
# Curated by hand -- comments and quoting are part of the review record.
group: rules
sources:
  - id: rule-a
    url: https://example.gov/a        # the canonical page
    format: html
    sha256: ''
  - id: rule-b
    url: https://example.gov/b
    format: html
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
"""

HTML_A = b"<html><body><p>" + b"Rule A text. " * 40 + b"</p></body></html>"


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "_meta" / "sources").mkdir(parents=True)
    (tmp_path / "documents").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(CORPUS_YML)
    (tmp_path / "_meta" / "sources" / "rules.yml").write_text(GROUP_YML)
    return config_mod.load(tmp_path / "_meta" / "corpus.yml")


# ---------------------------------------------------------------- record_baseline ------

def test_record_baseline_edits_one_line_and_nothing_else(corpus):
    path = corpus.source_manifest_path / "rules.yml"
    before = path.read_text()
    written = manifest.record_baseline(corpus, "rule-a", "a" * 64)
    assert written == path
    after = path.read_text()
    changed = [(o, n) for o, n in zip(before.splitlines(), after.splitlines()) if o != n]
    assert changed == [("    sha256: ''", f'    sha256: "{"a" * 64}"')]
    assert "# the canonical page" in after and after.startswith("# Curated by hand")
    assert yaml.safe_load(after)["sources"][1]["sha256"] == "0" * 64


def test_record_baseline_is_a_no_op_when_already_current(corpus):
    path = corpus.source_manifest_path / "rules.yml"
    before = path.read_bytes()
    assert manifest.record_baseline(corpus, "rule-b", "0" * 64) is None
    assert path.read_bytes() == before


def test_record_baseline_refuses_an_undeclared_or_duplicated_id(corpus):
    with pytest.raises(manifest.UndeclaredSource):
        manifest.record_baseline(corpus, "nope", "a" * 64)
    path = corpus.source_manifest_path / "rules.yml"
    path.write_text(path.read_text() + "  - id: rule-a\n    url: https://example.gov/dup\n")
    before = path.read_bytes()
    with pytest.raises(manifest.BaselineRefused, match="declared 2 times"):
        manifest.record_baseline(corpus, "rule-a", "a" * 64)
    assert path.read_bytes() == before


def test_record_baseline_keeps_crlf_endings(corpus):
    path = corpus.source_manifest_path / "rules.yml"
    path.write_bytes(GROUP_YML.replace("\n", "\r\n").encode())
    manifest.record_baseline(corpus, "rule-a", "b" * 64)
    data = path.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_changes_still_exposes_the_editor_under_its_old_names():
    from corpus_toolkit.sources import changes
    assert changes._rewrite_sha256 is manifest.rewrite_sha256
    assert changes._plan_sha_edits is manifest.plan_sha_edits
    assert changes._rewrite_problem is manifest.rewrite_problem


# ---------------------------------------------------------------- record_snapshot ------

def test_record_snapshot_writes_both_files_hashes_both_ways_and_moves_the_baseline(corpus):
    text = "Rule A text. " * 40
    snap = snapshots.record_snapshot(corpus, "rule-a", HTML_A, "html", text)
    assert snap.fresh is True
    assert snap.raw_path == corpus.snapshot_dir / "rule-a.html" and snap.raw_path.read_bytes() == HTML_A
    assert snap.text_path == corpus.snapshot_dir / "rule-a.txt" and snap.text_path.read_text() == text
    assert snap.sha256 == hash_snapshot("rule-a", "html", corpus.snapshot_dir)
    assert snap.content_hash == content_hash(HTML_A, "html", corpus.volatile_patterns)
    assert snap.baseline == "written"
    group = yaml.safe_load((corpus.source_manifest_path / "rules.yml").read_text())
    assert group["sources"][0]["sha256"] == snap.content_hash


def test_record_snapshot_over_unchanged_bytes_is_not_fresh_and_leaves_the_text_alone(corpus):
    snapshots.record_snapshot(corpus, "rule-a", HTML_A, "html", "first extraction " * 20)
    again = snapshots.record_snapshot(corpus, "rule-a", HTML_A, "html", "a DIFFERENT extractor " * 20)
    assert again.fresh is False
    assert again.text_path.read_text().startswith("first extraction")   # committed text wins
    assert again.baseline == "current"


def test_record_snapshot_for_a_source_the_manifest_does_not_declare(corpus):
    # html, not pdf: the baseline hash of a PDF runs pdftotext, which CI runners lack.
    page = b"<html><body><p>" + b"Orphan text. " * 30 + b"</p></body></html>"
    snap = snapshots.record_snapshot(corpus, "orphan", page, "html")
    assert snap.baseline == "undeclared" and snap.text_path is None
    assert (corpus.snapshot_dir / "orphan.html").is_file()
    assert snapshots.record_snapshot(corpus, "orphan", page, "html",
                                     baseline=False).baseline == "skipped"


def test_retrieved_date_advances_only_on_a_real_fetch(tmp_path):
    doc = tmp_path / "d.md"
    doc.write_text("---\nid: d\nretrieved: '2026-01-15'\n---\n\nbody\n")
    assert snapshots.retrieved_date(True, doc, today="2026-09-04") == "2026-09-04"
    assert snapshots.retrieved_date(False, doc, today="2026-09-04") == "2026-01-15"
    snap = tmp_path / "s.pdf"
    snap.write_bytes(b"x")
    assert snapshots.retrieved_date(False, tmp_path / "missing.md", snap) == \
        __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(snap.stat().st_mtime))


# ---------------------------------------------------------------- write_document -------

def good_frontmatter(**over):
    fm = {
        "id": "rule-a", "title": "Rule A", "doc_type": "rule", "citation": "OAR 000-000-0000",
        "issuing_body": "Test Body", "source_url": "https://example.gov/a",
        "source_format": "html", "status": "current", "maintainer": "tests",
        "retrieved": "2026-09-04", "source_sha256": "a" * 64, "content_mode": "verbatim",
        "authority_level": "administrative-rule",
    }
    fm.update(over)
    return fm


BODY = "\n# Rule A\n\nNON-AUTHORITATIVE mirror.\n\nText.\n"


def test_write_document_orders_keys_fills_platform_defaults_and_round_trips(corpus):
    path = corpus.root / "documents" / "rule-a.md"
    documents.write_document(corpus, path, good_frontmatter(zzz_custom="kept last"), BODY)
    text = path.read_text()
    assert text.startswith("---\nschema_version: 1\ncorpus: test-corpus\njurisdiction: oregon\nid: rule-a\n")
    keys = list(parse_frontmatter(path)[0])
    order = documents.canonical_order()
    known = [k for k in keys if k in order]
    assert known == sorted(known, key=order.index)           # platform order
    assert keys[-1] == "zzz_custom"                           # unknown keys follow
    fm, body = parse_frontmatter(path)
    assert fm["last_verified"] == "" and fm["verified_by"] == ""
    assert fm["retrieved"] == "2026-09-04"                    # a string, not a date
    assert body.strip() == BODY.strip()


def test_write_document_refuses_before_writing_and_names_every_finding(corpus):
    path = corpus.root / "documents" / "rule-a.md"
    bad = good_frontmatter(id="other-id", doc_type="statute")
    del bad["title"]
    with pytest.raises(documents.DocumentError) as ei:
        documents.write_document(corpus, path, bad, "no marker here")
    msgs = ei.value.findings
    assert any("'title' is a required property" in m for m in msgs)
    assert any("id 'other-id' != filename stem 'rule-a'" in m for m in msgs)
    assert any("NON-AUTHORITATIVE" in m for m in msgs)
    assert any("doc_type 'statute' does not belong under 'documents/'" in m for m in msgs)
    assert not path.exists()


def test_write_document_honours_corpus_declared_doc_types(tmp_path):
    (tmp_path / "_meta").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(textwrap.dedent("""\
        schema_version: 1
        corpus: {id: test-two, name: T, jurisdiction: oregon, archetype: document}
        content_roots:
          - path: "docs"
            doc_type: county_policy
        schema:
          doc_types:
            - name: county_policy
              verbatim: true
        disclaimer_marker: "NON-AUTHORITATIVE"
        """))
    cfg = config_mod.load(tmp_path / "_meta" / "corpus.yml")
    assert cfg.extra_doc_types == {"county_policy": True}
    path = tmp_path / "docs" / "rule-a.md"
    with pytest.raises(documents.DocumentError):          # the shared enum alone refuses it
        documents.write_document(None, path, good_frontmatter(doc_type="county_policy",
                                                              corpus="test-two", jurisdiction="oregon"), BODY)
    documents.write_document(cfg, path, good_frontmatter(doc_type="county_policy"), BODY)
    assert parse_frontmatter(path)[0]["doc_type"] == "county_policy"


def test_render_document_is_pure_and_dates_become_quoted_strings():
    import datetime
    text = documents.render_document({"id": "x", "retrieved": datetime.date(2026, 9, 4)}, "b")
    assert "retrieved: '2026-09-04'\n" in text
    assert text.endswith("---\n\nb\n")
    assert yaml.safe_load(text.split("---")[1])["retrieved"] == "2026-09-04"
