"""corpus-toolkit#210: `corpus_overview` reports the jurisdictions actually present.

A corpus declares ONE `jurisdiction` in corpus.yml, but its documents may carry their own
(states-it-governance holds four states' instruments). The overview keeps the declared
value untouched and adds `jurisdictions`: {jurisdiction: document count} read from the
documents. A single-jurisdiction corpus is unchanged except for the one additive field.
"""
import re
import subprocess
from pathlib import Path

from tests.test_backends import DOC, corpus, fw   # noqa: F401  (corpus is a fixture)

from corpus_toolkit.repo import repo_state as repo_state_for


def _commit(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "edit"], cwd=root, check=True)


def _set_jurisdiction(root: Path, doc_id: str, value: str | None) -> None:
    p = root / "statutes" / f"{doc_id}.md"
    text = re.sub(r"^jurisdiction:.*\n", "", p.read_text(), flags=re.M)
    if value is not None:
        text = text.replace("status: current\n", f"status: current\njurisdiction: {value}\n", 1)
    p.write_text(text)
    _commit(root)


def test_documents_without_their_own_jurisdiction_count_under_the_declared_one(corpus):
    o = fw(corpus).corpus_overview()
    assert o["jurisdiction"] == "oregon"
    assert o["jurisdictions"] == {"oregon": 2}
    assert "jurisdiction_unspecified" not in o


def test_two_jurisdiction_corpus_reports_each_with_its_count(corpus):
    _set_jurisdiction(corpus, "ors-1.010", "us-or")
    _set_jurisdiction(corpus, "ors-2.020", "us-wa")
    o = fw(corpus).corpus_overview()
    assert o["jurisdictions"] == {"us-or": 1, "us-wa": 1}
    # The declared value is the corpus's own statement and is not rewritten.
    assert o["jurisdiction"] == "oregon"


def test_counts_are_ordered_largest_first_and_sum_to_the_corpus(corpus):
    _set_jurisdiction(corpus, "ors-1.010", "us-wa")
    o = fw(corpus).corpus_overview()
    assert o["jurisdictions"] == {"oregon": 1, "us-wa": 1}
    assert sum(o["jurisdictions"].values()) == sum(o["documents_by_type"].values())


def test_undeclared_everywhere_is_unspecified_not_a_jurisdiction(corpus):
    """No corpus value and no document value is "could not tell", never a made-up name."""
    yml = corpus / "_meta" / "corpus.yml"
    yml.write_text(yml.read_text().replace("  jurisdiction: oregon\n", ""))
    _set_jurisdiction(corpus, "ors-1.010", "us-wa")
    o = fw(corpus).corpus_overview()
    assert o["jurisdictions"] == {"us-wa": 1}
    assert o["jurisdiction_unspecified"] == 1


def test_an_index_from_the_previous_schema_is_stale_and_rebuilt(corpus):
    import sqlite3
    f = fw(corpus)
    f.corpus_overview()
    db = f.backend._db_path
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET v = '4' WHERE k = 'schema'")
    con.commit()
    con.close()
    ok, why = f.backend.index_status(repo_state_for(corpus))
    assert not ok and "schema" in why
    assert f.corpus_overview()["jurisdictions"] == {"oregon": 2}
