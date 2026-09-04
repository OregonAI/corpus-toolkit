"""The name-agreement rule behind `basis: exact` in an agency crosswalk.

An agency crosswalk (ADR 0009: reference data, our own editorial assertion) may claim that
two spellings denote one body on `basis: exact` only when they are the same name under a
small, fixed set of permitted moves: case, punctuation, comma-inversion, a leading "Oregon".
Every corpus that builds a crosswalk must apply the SAME moves, or one pair of names is
exact in one repo and not in another — so the rule lives here, once, and the corpora import
it (oregon-budget#44 tracked the three verbatim copies this replaces).

The function bodies are the copies' bodies, byte for byte, so adopting this module changes
no answer.
"""


def norm_variants(name: str) -> set[str]:
    """Every reading the crosswalk note permits `basis: exact` to use.

    The note lists the allowed moves as "case, punctuation, comma-inversion, a leading
    Oregon" -- a SET of moves, not a pipeline that must apply all of them. A comma does two
    different jobs in these strings: catalog inversion ("Administrative Services, Department
    of") and a parent/child qualifier ("Secretary of State, Audits Division"). Inverting the
    second is wrong and dropping the comma in the first is not enough, so both readings are
    produced and a match on either is a match.

    Written this way because forcing a single reading is a MEASURED bug, not a hypothetical:
    always-invert reported 'Secretary of State Audits Division' as failing to match an
    oar_name that is the same name with a comma in it.
    """
    n = name.strip().replace("’", "'")
    readings = {n.replace(",", " ")}
    if "," in n:
        head, tail = n.rsplit(",", 1)
        readings.add(f"{tail.strip()} {head.strip()}")
    out = set()
    for r in readings:
        r = " ".join(r.lower().replace(".", "").split())
        for pre in ("oregon ", "state of oregon "):
            if r.startswith(pre):
                r = r[len(pre):]
        out.add(r)
    return out


def names_agree(a: str, b: str) -> bool:
    """True when two names are the same name under any reading the note permits."""
    return bool(norm_variants(a) & norm_variants(b))
