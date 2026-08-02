"""v1.19.0: the archetype is a contract, index rows carry status, schemes accumulate.

Each test is named as the claim it enforces, per this suite's house style."""
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus_toolkit import config as config_mod
from corpus_toolkit.config import _validated_archetype, _validated_extra_doc_types
from corpus_toolkit.remote import lookup


def test_a_typod_archetype_fails_at_load_not_at_query_time():
    with pytest.raises(ValueError) as e:
        _validated_archetype("hybird")
    assert "hybrid" in str(e.value)  # the error names the legal set


def test_absent_archetype_still_defaults_to_document():
    assert _validated_archetype(None) == "document"


def test_a_hybrid_with_no_tools_module_refuses_to_start(tmp_path):
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(
        "corpus:\n  id: t\n  name: T\n  jurisdiction: oregon\n  archetype: hybrid\n"
        "content_roots:\n  - path: documents\n    doc_type: policy\n")
    cfg = config_mod.load(tmp_path / "_meta" / "corpus.yml")
    from corpus_toolkit.mcp.server import build_server
    with pytest.raises(RuntimeError) as e:
        build_server(cfg)
    assert "tools_module" in str(e.value)


def test_lookup_returns_status_and_an_old_three_element_row_reads_unknown():
    idx = {"documents": {"new": ["T", "rule", "p.md", "superseded"],
                         "old": ["T", "rule", "p.md"],
                         "rich": {"title": "T", "doc_type": "rule", "path": "p",
                                  "status": "repealed"}}}
    assert lookup(idx, "new")["status"] == "superseded"
    # "" is UNKNOWN, never an implicit "current" — the whole point of the field.
    assert lookup(idx, "old")["status"] == ""
    assert lookup(idx, "rich")["status"] == "repealed"


def test_extra_doc_types_validate_loudly():
    assert _validated_extra_doc_types(
        {"doc_types": [{"name": "transmittal", "verbatim": False}]}) == {"transmittal": False}
    with pytest.raises(ValueError):
        _validated_extra_doc_types({"doc_types": [{"name": "Bad-Name", "verbatim": True}]})
    with pytest.raises(ValueError):
        _validated_extra_doc_types({"doc_types": [{"name": "ok"}]})  # verbatim is required


def test_all_matching_schemes_accumulate_and_a_refusal_note_survives(tmp_path):
    """federal-reference#12: the CJIS refusal must not be dropped because CFR also hit."""
    (tmp_path / "_meta").mkdir()
    (tmp_path / "documents").mkdir()
    (tmp_path / "_meta" / "corpus.yml").write_text(
        "corpus:\n  id: t\n  name: T\n  jurisdiction: us\n  archetype: document\n"
        "content_roots:\n  - path: documents\n    doc_type: policy\n")
    cfg = config_mod.load(tmp_path / "_meta" / "corpus.yml")
    from corpus_toolkit.mcp.framework import CorpusFramework
    fw = CorpusFramework(cfg)
    fw.schemes.append(("cfr", re.compile(r"2 CFR (?P<sec>200\.\d+)"), None,
                       lambda m: [f"cfr-{m.group('sec').replace('.', '-')}"], None))
    fw.schemes.append(("cjis", re.compile(r"CJIS.*?(?P<v>5\.9\.\d+)"), None,
                       lambda m: ([], f"version {m.group('v')} is not held — refusing"), None))
    name, corpus, cands, note = fw._match_schemes("CJIS Policy 5.9.4 and 2 CFR 200.303")
    assert name == "cfr+cjis" or name == "cjis+cfr"
    assert "cfr-200-303" in cands
    assert note and "refusing" in note  # the second scheme's refusal SURVIVES
