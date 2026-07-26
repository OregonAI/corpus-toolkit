"""Detect frontmatter that asserts things its source does not say.

WHY THIS EXISTS. A corpus built on this toolkit already diffs `## Full text` against a
pinned snapshot, line by line, in order, with a coverage floor. Nothing does the
equivalent for FRONTMATTER — and that asymmetry is where every fabrication found so far
has lived. In oregon-records-retention the body text came through clean (6,569 record
series checked, none damaged) while `legal_authority` carried three plausible, entirely
invented citations on all 76 documents. Every validator passed the whole time, because
none of them compares an asserted field to the document it describes.

This is a REPORT, not a gate, and deliberately so. The first version of the quote
matcher this borrows from flagged 43 faithful quotes as fabrications, purely because the
source printed curly quotes where the catalog transcribed straight ones. A grounding
check earns teeth by being measured first; shipped as a gate on day one it would mostly
teach people to ignore it.

Three signals, strongest first:

  1. CITATION-SHAPED values absent from their own document. A citation asserted ABOUT a
     document should be findable IN it. Highest precision, and the signal that catches
     the failure above.
  2. CONSTANT-AND-ABSENT: a field carrying one identical value corpus-wide that appears
     in no snapshot at all. Hard-coding leaves exactly this fingerprint.
  3. PER-FIELD GROUNDING RATE: how often each field's value is findable in its own
     source. A field sitting at 0% that is not structural is worth a human look.

  corpus-detect-unsourced --config _meta/corpus.yml
  corpus-detect-unsourced --config _meta/corpus.yml --limit 500     # spot check
  corpus-detect-unsourced --config _meta/corpus.yml --field legal_authority
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

from . import config as config_mod
from .repo import content_files, parse_frontmatter

# Folding, ported from the conflict-quote grounding check that proved it. Sources print
# typographic quotes and en/em dashes where a transcriber types ASCII; comparing raw
# reports faithful values as ungrounded. This is not cosmetic — it was the difference
# between 43 false fabrication alarms and none.
_QUOTES = dict.fromkeys(map(ord, "\"'‘’“”«»"), None)
_DASHES = {ord(c): "-" for c in "‐‑‒–—―−"}


def fold(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).translate(_QUOTES).translate(_DASHES)).strip().lower()


# "ORS 192.005", "OAR 166-030-0027", "42 CFR 2.1" — an UPPERCASE authority abbreviation
# followed by a structured number. The uppercase requirement is what makes this precise:
# a first draft accepted any capitalised word plus digits, which swept in every
# "September 2015" edition date and buried the real signal in dates.
CITATION_RE = re.compile(r"^(?:\d+\s+)?[A-Z]{2,6}\.?\s+\d[\w.\-]*$")

# A citation list in a source shares ONE authority prefix across many sections:
#   "Statutory/Other Authority:  ORS 179.040, 423.020, 423.030 & 423.075"
# Frontmatter correctly expands that to "ORS 423.030", but the literal string never
# appears anywhere. Checking only the full form reported 7,331 fabrications against the
# mature corpus, every one of them this artifact. So a citation counts as grounded if
# EITHER the full form or its bare number is present. That trades a little sensitivity
# for precision, which is the right trade: a false accusation of fabrication costs far
# more than a missed one, and this tool exists to be believed.
CITATION_NUM_RE = re.compile(r"^(?:\d+\s+)?[A-Z]{2,6}\.?\s+(\S+)$")

# Registry slugs ("department-of-fish-and-wildlife") are controlled-vocabulary
# identifiers, not quotations. The source says "Department of Fish and Wildlife"; the
# hyphenated slug appears nowhere and never should.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")   # 1+ hyphens: "employment-department" counts

# Dates are asserted ABOUT a document in a normalised form the source never prints
# ("2017-08-31" for "August 2017"), so literal absence proves nothing. Excluded from the
# citation signal; still counted in the grounding-rate table.
DATEISH_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z.]*\s+"
    r"\d{1,2}?,?\s*\d{4})$", re.I)

# Fields that are structural or administrative — true of the document, not quoted from
# it — so their absence from the source says nothing. Not a whitelist for correctness,
# just noise suppression; anything here is still counted in the grounding-rate table.
STRUCTURAL = {
    "schema_version", "corpus", "jurisdiction", "id", "doc_type", "source_url",
    "source_format", "source_sha256", "retrieved", "last_verified", "verified_by",
    "maintainer", "content_mode", "snapshot_policy", "snapshot_id", "status",
    "authority_level", "issuing_body", "tags", "relationships", "conversion_notes",
    "content_exception", "migration_pending", "last_reviewed",
    # `citation` is the document's OWN name, not a claim about another document. A
    # statute's body is the text OF "ORS 100.022" and has no reason to restate its own
    # number, so checking it produced 1,792 false fabrication reports on the first run
    # over the mature corpus — the same failure mode as the quote matcher's 43. Identity
    # is not an assertion.
    "citation", "title",
    # Constructed provenance strings, not quotations: source_version reads
    # "DEQ 7-2015, effective 2015-04-16" — assembled from the rule's history line, and
    # never present verbatim. effective_date is a normalised ISO value the source does
    # not print in that form. Both were dominating the report with expected absences.
    "source_version", "effective_date", "expires",
}


def _values(value):
    """Scalar strings worth checking, flattened out of lists. Numbers and booleans are
    skipped: a bare year is present in half the corpus by chance, and matching it would
    manufacture confidence rather than measure it."""
    if isinstance(value, str):
        v = value.strip()
        return [v] if len(v) >= 3 else []
    if isinstance(value, (list, tuple)):
        return [x for item in value for x in _values(item)]
    return []


def scan(config, limit: int | None = None, only_field: str | None = None) -> dict:
    snap_dir = config.snapshot_dir
    # BOUNDED, and it has to be. executive-regulatory-frameworks carries 1.1 GB across
    # 77,028 snapshot files; caching every folded one would hold the whole corpus in
    # memory to answer questions about it. The cache exists so documents SHARING a
    # snapshot (a statute chapter sliced into sections) do not re-read it, and that
    # locality is local — a small window captures all of it.
    snap_cache: collections.OrderedDict[str, str] = collections.OrderedDict()
    CACHE_MAX = 256

    def source_text(fm: dict) -> str | None:
        """The folded snapshot for one document, or None when it has none to check
        against — a summary-only document is not evidence of fabrication."""
        sid = fm.get("snapshot_id") or fm.get("id")
        if not sid:
            return None
        if sid in snap_cache:
            snap_cache.move_to_end(sid)
        else:
            txt = snap_dir / f"{sid}.txt"
            snap_cache[sid] = fold(txt.read_text(encoding="utf-8", errors="replace")) \
                if txt.is_file() else ""
            if len(snap_cache) > CACHE_MAX:
                snap_cache.popitem(last=False)
        return snap_cache[sid] or None

    per_field = collections.defaultdict(lambda: {"checked": 0, "grounded": 0})
    value_docs = collections.defaultdict(set)      # (field, value) -> doc ids
    value_grounded = collections.defaultdict(int)  # (field, value) -> times found
    citation_misses = collections.defaultdict(set)
    n_docs = n_no_snapshot = 0

    for path in content_files(config):
        if limit and n_docs >= limit:
            break
        try:
            fm, _ = parse_frontmatter(path)
        except (ValueError, OSError):
            continue
        n_docs += 1
        src = source_text(fm)
        if src is None:
            n_no_snapshot += 1
            continue
        for field, raw in fm.items():
            if only_field and field != only_field:
                continue
            for value in _values(raw):
                key = (field, value)
                value_docs[key].add(fm.get("id", str(path)))
                found = fold(value) in src
                if not found:
                    num = CITATION_NUM_RE.match(value)
                    if num and fold(num.group(1)) in src:
                        found = True          # shared-prefix list form, see above
                per_field[field]["checked"] += 1
                per_field[field]["grounded"] += int(found)
                if found:
                    value_grounded[key] += 1
                elif (field not in STRUCTURAL and CITATION_RE.match(value)
                      and not DATEISH_RE.match(value)):
                    citation_misses[key].add(fm.get("id", str(path)))

    return {"n_docs": n_docs, "n_no_snapshot": n_no_snapshot, "per_field": dict(per_field),
            "value_docs": value_docs, "value_grounded": value_grounded,
            "citation_misses": citation_misses}


def report(res: dict, corpus_id: str, top: int = 15) -> list[str]:
    out = [f"corpus: {corpus_id} — {res['n_docs']} document(s) scanned, "
           f"{res['n_no_snapshot']} without a committed .txt to check against", ""]

    cites = sorted(res["citation_misses"].items(), key=lambda kv: -len(kv[1]))
    out.append("1. CITATION-SHAPED VALUES NOT FOUND IN THEIR OWN SOURCE")
    out.append("   A citation asserted about a document should be findable in it. Two")
    out.append("   things produce a hit: the citation was invented, or it was read from")
    out.append("   part of the source that never got committed. Both are worth knowing;")
    out.append("   only one is a fabrication, so read before concluding.")
    if not cites:
        out.append("   (none)")
    for (field, value), docs in cites[:top]:
        out.append(f"   {field}: {value!r} — asserted on {len(docs)} document(s), "
                   f"found in 0 (e.g. {sorted(docs)[0]})")
    if len(cites) > top:
        out.append(f"   … and {len(cites) - top} more")
    out.append("")

    out.append("2. ONE VALUE CORPUS-WIDE, PRESENT IN NO SOURCE")
    out.append("   Hard-coding leaves this fingerprint: every document, never the source.")
    const = [((f, v), docs) for (f, v), docs in res["value_docs"].items()
             if f not in STRUCTURAL and len(docs) > 1
             and not SLUG_RE.match(v)          # registry identifiers are not quotations
             and res["value_grounded"].get((f, v), 0) == 0]
    const.sort(key=lambda kv: -len(kv[1]))
    if not const:
        out.append("   (none)")
    for (field, value), docs in const[:top]:
        span = "all" if len(docs) == res["n_docs"] else str(len(docs))
        out.append(f"   {field}: {value[:60]!r} — {span} of {res['n_docs']} documents, "
                   "grounded in none")
    if len(const) > top:
        out.append(f"   … and {len(const) - top} more")
    out.append("")

    out.append("3. PER-FIELD GROUNDING RATE")
    out.append("   Low is not automatically wrong — a title or a curator summary is not")
    out.append("   quoted from the source. Zero on a field that should be transcribed is.")
    rows = sorted(res["per_field"].items(), key=lambda kv: (kv[1]["grounded"] / kv[1]["checked"]
                                                            if kv[1]["checked"] else 1.0))
    for field, s in rows:
        if not s["checked"]:
            continue
        pct = 100.0 * s["grounded"] / s["checked"]
        mark = "  <-- nothing grounded" if s["grounded"] == 0 and field not in STRUCTURAL else ""
        out.append(f"   {field:24s} {s['grounded']:6d}/{s['checked']:<6d} {pct:5.1f}%{mark}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--limit", type=int, help="scan at most N documents (spot check)")
    ap.add_argument("--field", help="restrict to one frontmatter field")
    ap.add_argument("--top", type=int, default=15, help="rows per section (default 15)")
    ap.add_argument("--fail-on-citation-miss", action="store_true",
                    help="exit 1 if section 1 is non-empty. OFF by default: this is a "
                         "report meant to be read before it is enforced.")
    args = ap.parse_args()

    config = config_mod.load(args.config)
    res = scan(config, args.limit, args.field)
    print("\n".join(report(res, config.id, args.top)))
    if args.fail_on_citation_miss and res["citation_misses"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
