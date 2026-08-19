"""A JSON source declares which paths it watches (corpus-toolkit#72).

`content_hash` normalised only the html/xml branch; `pdf` got text extraction and everything
else — including `json` — fell through to a raw-byte sha256 of the whole document.

For a Socrata-backed corpus that is a permanent false-positive generator. The manifest
convention points `url` at the dataset's METADATA document rather than its rows, deliberately:
hashing 668,906 rows reports a change every run and says nothing. But that metadata carries
counters that move on their own —

    downloadCount   35632
    viewCount       13812
    rowsUpdatedAt   1765475245   (2025-12-11 — the data, unchanged for eight months)

— so the hash changed continuously while the data's own timestamp sat still. `oregon-budget`'s
three JSON sources produced six distinct hashes across two consecutive weekly runs, in a week
its `live-reconciliation` job passed.

AN ALLOWLIST, NOT A BLOCKLIST, and that was the decision this issue waited on. Declaring what
matters means a new upstream counter is inert by construction; a blocklist makes every new
counter a fresh false positive until somebody extends it — the same failure arriving slower.
It also matches how the rest of the platform works: a corpus declares its `index_headings`,
its `issuing_body_slug_sentinels`, its `doc_types`.

OPT-IN. A json source with no `watch` hashes exactly as before, so no committed baseline
moves for any corpus that does not adopt it. That is what makes this shippable without a
platform-wide re-hash.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus_toolkit.repo import content_hash


def socrata(*, view=13812, down=35632, rows_updated=1765475245, columns=None, extra=None):
    """A Socrata metadata document, in the shape `oregon-budget` actually watches."""
    doc = {
        "id": "y9g9-xsxs",
        "name": "Agency Expenditures",
        "description": "Statewide agency expenditures by fund and account. " * 8,
        "rowsUpdatedAt": rows_updated,
        "viewCount": view,
        "downloadCount": down,
        "totalTimesRated": 0,
        "columns": columns if columns is not None else [
            {"name": "agency", "dataTypeName": "text",
             "cachedContents": {"non_null": view, "largest": "DAS"}},
            {"name": "amount", "dataTypeName": "number",
             "cachedContents": {"non_null": view, "largest": str(view)}},
        ],
    }
    if extra:
        doc.update(extra)
    return json.dumps(doc).encode()


WATCH = ["rowsUpdatedAt", "columns[].name", "columns[].dataTypeName"]


def test_a_json_source_with_no_watch_hashes_exactly_as_before(tmp_path):
    """THE PROPERTY THAT MAKES THIS SHIPPABLE. Opt-in means no committed baseline moves for
    a corpus that does not adopt it — and a blanket change to JSON hashing would have made
    every JSON source on the platform read as drifted at once, which is this issue's own
    defect reproduced at scale by its fix."""
    import hashlib
    raw = socrata()

    assert content_hash(raw, "json") == hashlib.sha256(raw).hexdigest()


def test_counters_that_move_on_their_own_do_not_change_the_hash(tmp_path):
    """The reported bug. Only `viewCount`/`downloadCount`/`cachedContents` differ."""
    a = content_hash(socrata(view=13812, down=35632), "json", watch=WATCH)
    b = content_hash(socrata(view=13999, down=35750), "json", watch=WATCH)

    assert a == b


def test_a_watched_value_changing_does_change_the_hash(tmp_path):
    """The other half — a watch list that never fires is worth nothing."""
    a = content_hash(socrata(rows_updated=1765475245), "json", watch=WATCH)
    b = content_hash(socrata(rows_updated=1799999999), "json", watch=WATCH)

    assert a != b


def test_a_watched_path_reaches_into_arrays(tmp_path):
    """`columns[].name` is the documented case, and a schema change is exactly what these
    manifests are trying to notice."""
    renamed = [{"name": "agency", "dataTypeName": "text", "cachedContents": {}},
               {"name": "amount_usd", "dataTypeName": "number", "cachedContents": {}}]

    a = content_hash(socrata(), "json", watch=WATCH)
    b = content_hash(socrata(columns=renamed), "json", watch=WATCH)

    assert a != b


def test_reordering_keys_and_reserialising_does_not_change_the_hash(tmp_path):
    """Upstream re-serialising its JSON is not a content change. Without canonicalisation
    this whole mechanism would trade one false positive for another."""
    doc = json.loads(socrata().decode())
    reordered = {k: doc[k] for k in reversed(list(doc))}

    a = content_hash(socrata(), "json", watch=WATCH)
    b = content_hash(json.dumps(reordered, indent=4).encode(), "json", watch=WATCH)

    assert a == b


def test_a_declared_path_that_is_missing_is_REPORTED_not_hashed_as_empty(tmp_path):
    """"COULD NOT CHECK" IS NEVER "IS NOT THERE" — the one rule CONTEXT.md says outranks
    the vocabulary.

    Two documents that both lack a watched path would otherwise hash equal and read as
    "unchanged", which is the loudest possible way to be wrong: the corpus would report
    stability precisely when upstream removed the field it was watching."""
    from corpus_toolkit.repo import WatchedPathMissing

    with pytest.raises(WatchedPathMissing) as e:
        content_hash(socrata(), "json", watch=["rowsUpdatedAt", "schema.version"])

    assert "schema.version" in str(e.value)


def test_a_missing_path_inside_an_array_is_reported_too(tmp_path):
    """The array projection has the same hazard, one level down."""
    from corpus_toolkit.repo import WatchedPathMissing

    with pytest.raises(WatchedPathMissing) as e:
        content_hash(socrata(), "json", watch=["columns[].precision"])

    assert "columns[].precision" in str(e.value)


def test_a_corpus_wide_volatile_pattern_does_not_break_a_json_source(tmp_path):
    """THE OPT-IN GUARANTEE, and the first version broke it.

    `volatile_patterns` is declared CORPUS-WIDE and passed for every source, so refusing
    `json` + patterns made any json source raise forever in a corpus that declared one
    pattern for an unrelated HTML group — a source that opted into nothing and hashed fine
    before. The premise was wrong too: such a pattern is not "doing nothing", it is doing
    its job on the html sources.

    The #66 concern it was reaching for is already covered — the drift report lists any
    pattern that matched nothing, per run."""
    import hashlib, re
    raw = socrata()

    assert content_hash(raw, "json", [re.compile(rb'sid=\d+')]) == \
        hashlib.sha256(raw).hexdigest()


def test_a_document_that_is_not_json_is_reported_clearly(tmp_path):
    """A watch list against a body that will not parse is a real upstream condition — an
    error page served with a 200, say — and must not read as a hash."""
    from corpus_toolkit.repo import WatchedPathMissing

    with pytest.raises((WatchedPathMissing, ValueError)) as e:
        content_hash(b"<html>502 Bad Gateway</html>", "json", watch=WATCH)

    assert "json" in str(e.value).lower()


@pytest.mark.parametrize("fmt", ["html", "json"])
def test_a_short_watched_document_is_not_silently_byte_hashed(fmt, tmp_path):
    """The html path falls back to a raw-byte hash under 200 characters. Inheriting that
    here would make the mechanism work for large metadata documents and quietly not work
    for small ones — the difference invisible until it mattered.

    PARAMETRISED OVER `html` BECAUSE THAT IS THE FMT PRODUCTION SEES, and passing only
    `"json"` made this vacuous: `content_hash`'s `json` arm returns a raw-byte hash
    unconditionally and never reaches the <200 branch, so the assertion held no matter what
    the watch branch did. A Socrata entry is written `url: .../y9g9-xsxs.json` with no
    `format:` key, and `_format_for` maps an unrecognised `.json` extension to `"html"` —
    the one arm where the fallback is real."""
    tiny = json.dumps({"rowsUpdatedAt": 1, "viewCount": 2}).encode()

    a = content_hash(tiny, fmt, watch=["rowsUpdatedAt"])
    b = content_hash(json.dumps({"rowsUpdatedAt": 1, "viewCount": 999}).encode(),
                     fmt, watch=["rowsUpdatedAt"])

    assert a == b, "the under-200-char fallback swallowed the watch list"


def test_an_empty_array_projection_is_reported_not_silently_empty(tmp_path):
    """`_select_watched` returned [] once the projection hit an empty array, so no leaf key
    was ever checked — contradicting its own contract.

    Two consequences, both silent: a typo'd leaf validates against any document whose array
    happens to be empty, and if upstream starts returning `"columns": []` — a real degraded
    shape — the digest flips once and is then STABLE FOREVER while the watched schema is
    gone. That is the exact condition this exception class exists for."""
    from corpus_toolkit.repo import WatchedPathMissing, _select_watched

    with pytest.raises(WatchedPathMissing):
        _select_watched({"columns": [], "rowsUpdatedAt": 1}, "columns[].name")


def test_a_second_projection_in_one_path_is_refused(tmp_path):
    """Nested `[]` flattened, so meaningfully different documents hashed EQUAL:

        {"a": [{"b": [1, 2]}, {"b": [3]}]}
        {"a": [{"b": [1]}, {"b": [2, 3]}]}

    Drift between those could never be reported. Nothing in the grammar restricted `[]` to
    one segment, so it was reachable from a documented path. Narrow beats general: refuse
    it rather than grow a nesting-preserving encoder nobody asked for."""
    from corpus_toolkit.repo import _select_watched

    with pytest.raises(ValueError) as e:
        _select_watched({"a": [{"b": [1]}]}, "a[].b[]")

    assert "[]" in str(e.value)


def test_a_non_utf8_watched_value_does_not_hash_equal_to_a_different_one(tmp_path):
    """`errors="replace"` mapped every invalid byte to U+FFFD before parsing, so two
    distinct non-UTF-8 spellings of a watched value collided."""
    from corpus_toolkit.repo import WatchedPathMissing
    import json as _json

    a = b'{"n": "caf\xe9"}'
    b = b'{"n": "caf\xe8"}'

    hashes = set()
    for raw in (a, b):
        try:
            hashes.add(content_hash(raw, "json", watch=["n"]))
        except WatchedPathMissing:
            hashes.add(f"refused:{raw!r}")

    assert len(hashes) == 2, "two different documents produced one hash"


def test_a_bare_string_watch_is_refused_at_the_hash_too_not_walked(tmp_path):
    """DEFENCE AT THE CHOKE POINT, not only at the door.

    `_validated_watch` runs over the sources a drift run will fetch, which is one caller. `content_hash`
    is where a watch list is actually USED, and reached by `_record_baselines`, by a
    corpus's own scripts, and by anything that builds a source dict another way. A bare
    string is truthy and iterable, so unguarded here it is walked character by character
    and the caller is told `watched path 'r' is not present in this document` — a typo
    reported as an upstream schema change.

    corpus-toolkit#111 is the same lesson: a guard on the outer surface is not a guard."""
    with pytest.raises(ValueError) as e:
        content_hash(socrata(), "json", watch="rowsUpdatedAt")

    assert "list" in str(e.value)
    assert "'r'" not in str(e.value), "it walked the string instead of refusing it"


def test_a_utf8_bom_is_read_not_reported_as_an_error_page(tmp_path):
    """BOM-prefixed JSON is routine from IIS/.NET-backed government endpoints — which is
    the population this toolkit crawls.

    `json.loads(raw.decode("utf-8"))` rejects it, so adopting `watch` on such a source made
    it permanently uncomparable, exit 1 every run, with the operator told the response was
    an error page. `json.loads(bytes)` would have accepted it, because CPython sniffs
    utf-8-sig. Strict decoding was the right call for finding real mojibake; refusing a byte
    order mark is not what it was for, and the source hashed fine before adoption."""
    raw = b"\xef\xbb\xbf" + socrata()

    assert content_hash(raw, "json", watch=["rowsUpdatedAt"]) == \
        content_hash(socrata(), "json", watch=["rowsUpdatedAt"])


def test_a_missing_dot_before_a_projection_is_refused_as_a_typo(tmp_path):
    """`columns[]name` — a dot away from `columns[].name` — passed every check and was then
    reported as `watched path 'columns[]name' is not present ... Either upstream changed
    shape ... or the path is wrong`, on stderr, with a CI annotation and an unconditional
    exit 1. An authoring typo, dressed as an upstream schema change, after the crawl. That
    is the exact misdiagnosis load-time validation exists to prevent."""
    from corpus_toolkit.repo import validate_watch

    with pytest.raises(ValueError) as e:
        validate_watch(["columns[]name"])

    assert "[]" in str(e.value)


def test_a_padded_path_is_trimmed_rather_than_reported_missing(tmp_path):
    """`- " rowsUpdatedAt "` survived, because `path.strip()` was tested for emptiness and
    then thrown away. The lookup used the padded string and reported it missing."""
    from corpus_toolkit.repo import validate_watch

    assert validate_watch([" rowsUpdatedAt "]) == ["rowsUpdatedAt"]
    assert content_hash(socrata(), "json", watch=[" rowsUpdatedAt "]) == \
        content_hash(socrata(), "json", watch=["rowsUpdatedAt"])


def test_the_choke_point_enforces_every_check_the_door_does(tmp_path):
    """THE TWO GUARDS MUST NOT DISAGREE. `_watched_digest` calls itself "THE CHOKE POINT,
    not only the door" but replicated 2 of the door's 5 checks, so a direct caller passing
    `["a[].b[]"]` got a bare ValueError out of `_select_watched` — which `main`'s
    `except Exception` files under "a fact about our access, not about upstream".

    One grammar, one parser, in the module that owns it (corpus-toolkit#94's lesson)."""
    from corpus_toolkit import config as config_mod
    from corpus_toolkit.repo import validate_watch

    bad = ["a[].b[]", "a..b", "columns[]name", "columns[ ].name", "", "  ", "[]"]
    for path in bad:
        with pytest.raises(ValueError):
            validate_watch([path])
        with pytest.raises(ValueError):
            content_hash(socrata(), "json", watch=[path])
        with pytest.raises(ValueError):
            config_mod._validated_watch([path], "some-source")

    # THE EMPTY LIST, at the choke point specifically. `content_hash` routes on
    # `watch is not None` precisely so these reach the guard, and NOTHING pinned that:
    # mutating it back to `if watch:` — the regression the comment there warns about —
    # passed the entire suite. A caller writing `watch=cfg.get("watch", [])` or
    # `watch=[p for p in paths if p]` then got unwatched hashing while believing the source
    # was watch-scoped, which is this codebase's definition of the worst outcome: a silent
    # wrong answer where an error belongs.
    for empty in ([], ()):
        with pytest.raises(ValueError) as e:
            content_hash(socrata(), "json", watch=empty)
        assert "empty" in str(e.value)
        with pytest.raises(ValueError):
            validate_watch(empty)


def test_a_segment_that_is_only_a_projection_is_refused(tmp_path):
    """`[]`, `a.[]` and `[].name` passed the door with an EMPTY key, then surfaced from the
    crawl as `WATCH PATH MISSING` — "either upstream changed shape, or the path is wrong" —
    with an annotation and exit 1. The neighbouring rules already reject `.name` and
    `columns[].`; this was the one hole in the same check."""
    from corpus_toolkit.repo import validate_watch

    for path in ("[]", "a.[]", "[].name"):
        with pytest.raises(ValueError) as e:
            validate_watch([path])
        assert "segment" in str(e.value) or "key" in str(e.value), path


def test_a_document_that_is_a_bare_array_says_so(tmp_path):
    """Socrata's `/resource/{id}.json` — the sibling of the endpoint this feature is written
    for — returns a top-level ARRAY. No path in this grammar can address it, and every one
    reported "upstream changed shape", sending the operator to compare a schema that was
    never involved."""
    from corpus_toolkit.repo import WatchedDocumentUnreadable

    with pytest.raises(WatchedDocumentUnreadable) as e:
        content_hash(b'[{"a": 1}]', "json", watch=["a"])

    assert "array" in str(e.value).lower()


def test_the_type_error_does_not_explain_a_reason_that_does_not_apply(tmp_path):
    """`watch: 5` and `watch: {a: 1}` were both told "a bare string is iterated CHARACTER BY
    CHARACTER" — a rationale about a type they are not. Convention 5: name the condition
    that occurred."""
    from corpus_toolkit.repo import validate_watch

    for bad in (5, {"a": 1}):
        with pytest.raises(ValueError) as e:
            validate_watch(bad)
        assert "CHARACTER BY CHARACTER" not in str(e.value), bad

    with pytest.raises(ValueError) as e:
        validate_watch("rowsUpdatedAt")
    assert "CHARACTER BY CHARACTER" in str(e.value)


@pytest.mark.parametrize("path", ["columns[]. name", "columns[].\tname",
                                  " columns[] . name "])
def test_whitespace_inside_a_path_is_trimmed_not_looked_up(path, tmp_path):
    """`path.strip()` trimmed the whole string and not its segments, so a space after a dot
    survived and the lookup ran on `' name'` — reported as a path the document does not
    contain, with an annotation and exit 1."""
    from corpus_toolkit.repo import validate_watch

    cleaned = validate_watch([path])
    assert " " not in cleaned[0] and "\t" not in cleaned[0], cleaned


def test_a_segment_of_only_whitespace_is_refused_not_trimmed_away(tmp_path):
    """`a. .b` trims to `a..b` — a segment with no key. Trimming it away silently would
    turn a typo into a different, valid path and hash the wrong thing."""
    from corpus_toolkit.repo import validate_watch

    with pytest.raises(ValueError) as e:
        validate_watch(["a. .b"])

    assert "segment" in str(e.value)


@pytest.mark.parametrize("path", ["columns [].name", "columns\t[].name", " columns [] . name "])
def test_whitespace_before_a_projection_is_handled_like_whitespace_after_it(path, tmp_path):
    """Per-segment stripping trimmed segment EDGES, so whitespace before `[]` stayed
    interior: the segment still ended `[]`, the key became `'columns '`, and the crawl
    reported it as a path upstream does not have.

    `columns[] .name` — the same typo, one space to the right — was normalised and worked.
    Two spellings of one mistake, opposite outcomes, which is the asymmetry the per-segment
    fix was supposed to end rather than move. Both now normalise; whitespace around a
    segment or around `[]` is trimmed, and only a segment left with no key, or with a
    bracket still in it, is refused."""
    from corpus_toolkit.repo import validate_watch

    assert validate_watch([path]) == ["columns[].name"]


def test_a_bracket_that_is_not_a_clean_projection_is_refused(tmp_path):
    """`columns[ ].name` was accepted as a literal key named `columns[ ]` and then reported
    missing. A bracket in a real json key is vanishingly rare; a mistyped projection is
    not."""
    from corpus_toolkit.repo import validate_watch

    for path in ("columns[ ].name", "columns[0].name", "columns].name"):
        with pytest.raises(ValueError) as e:
            validate_watch([path])
        assert "[]" in str(e.value), path


def test_reordering_an_ARRAY_does_change_the_hash(tmp_path):
    """The counterpart to the key-order test, and MIGRATION said only half of it.

    Key order is normalised (`sort_keys=True`); array ELEMENT order is not, and must not be
    — `columns[].name` reordering is a schema change worth reporting. The docs claimed
    "upstream re-ordering its JSON is not a change" without that distinction."""
    cols = [{"name": "agency", "dataTypeName": "text", "cachedContents": {}},
            {"name": "amount", "dataTypeName": "number", "cachedContents": {}}]

    a = content_hash(socrata(columns=cols), "json", watch=WATCH)
    b = content_hash(socrata(columns=list(reversed(cols))), "json", watch=WATCH)

    assert a != b


@pytest.mark.parametrize("body,expect", [
    (b'[{"a": 1}]', "array"),
    (b'5', "int"),
    (b'"upstream is busy, try later"', "str"),
    (b'null', "null"),
])
def test_a_non_object_document_names_what_actually_arrived(body, expect, tmp_path):
    """The message named the real type in its first clause and then diagnosed an ARRAY
    regardless — "an array document cannot be watched — point `url` at the dataset's
    metadata document instead of its rows".

    For a bare string or number there is no array and no rows: an API answering `200` with
    a plain error string got advice about Socrata's `/resource` sibling, a condition that
    did not occur. Convention 5, on the error this feature added to satisfy convention 5."""
    from corpus_toolkit.repo import WatchedDocumentUnreadable

    with pytest.raises(WatchedDocumentUnreadable) as e:
        content_hash(body, "json", watch=["a"])

    assert expect in str(e.value)
    if expect != "array":
        assert "rows" not in str(e.value), "array advice for a document that is not one"
