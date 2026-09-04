"""`corpus_toolkit.crosswalk` is the one copy of the `basis: exact` name rule. These are the
cases the corpora's verbatim copies were written against, including the measured bug."""
import pytest

from corpus_toolkit.crosswalk import names_agree, norm_variants


@pytest.mark.parametrize("a, b", [
    # catalog inversion: the comma marks "Department of" moved to the back
    ("Administrative Services, Department of", "Department of Administrative Services"),
    # parent/child qualifier: the comma must NOT be inverted — the measured bug
    ("Secretary of State, Audits Division", "Secretary of State Audits Division"),
    # case and punctuation
    ("OREGON HEALTH AUTHORITY", "Oregon Health Authority"),
    ("Dept. of Corrections", "Dept of Corrections"),
    # a leading Oregon / State of Oregon
    ("Oregon Youth Authority", "Youth Authority"),
    ("State of Oregon Department of Justice", "Department of Justice"),
    # the curly apostrophe the third copy once wrote as a literal (oregon-budget#44)
    ("Psychologist Examiners’ Board", "Psychologist Examiners' Board"),
])
def test_the_permitted_moves_agree(a, b):
    assert names_agree(a, b), (norm_variants(a), norm_variants(b))


@pytest.mark.parametrize("a, b", [
    ("Board of Psychology", "Board of Pharmacy"),
    ("Department of Corrections", "Department of Justice"),
    # a qualifier is not a move: the division is not its parent
    ("Secretary of State, Audits Division", "Secretary of State"),
])
def test_different_names_do_not_agree(a, b):
    assert not names_agree(a, b)


def test_both_comma_readings_are_produced():
    got = norm_variants("Administrative Services, Department of")
    assert "administrative services department of" in got
    assert "department of administrative services" in got
