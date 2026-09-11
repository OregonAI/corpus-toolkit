"""corpus-detect-changes could not hash a zip-wrapped source (corpus-toolkit#199).

`content_hash()` had no zip branch: a source whose fetched bytes were a zip archive (OLRC's
per-title USLM release points, e.g. `.../xml_usc20@119-103.zip`, are the first of these on
the platform) was hashed as if the zip bytes themselves were the declared format -- binary
fallback, sha256 of the archive. Meanwhile a corpus that unzips its own fetch and caches the
decompressed member (federal-reference's ADR-0006 fetcher) hashes THAT, so a baseline seeded
from the corpus's own ingestion could never be reproduced by a later `corpus-detect-changes`
run on the same URL: two different digests, by construction, forever.

Fixed by giving `content_hash` a "zip" format: unzip (exactly one member -- every known zip
source on the platform, including OLRC's, has one) and hash the decompressed bytes as
whatever format the member's own filename implies, the same extension rule `content_hash`
already runs a raw fetch through. That makes `content_hash(zip_bytes, "zip")` equal to
`content_hash(member_bytes, "xml")` -- exactly what a corpus caching the unzipped XML would
compute on its own ingestion for the same source.
"""
import hashlib
import io
import zipfile

import pytest

from corpus_toolkit.repo import ArchiveUnreadable, content_hash
from corpus_toolkit.sources.changes import _format_for

# Long enough that content_hash's extracted-text path is taken, not the <200-char
# raw-byte fallback -- the same margin test_volatile_patterns.py's PAGE_V7 keeps.
XML_MEMBER = (
    b'<?xml version="1.0"?>\n<uslm><body><section>'
    + b"Some enacted statutory text, repeated so extraction clears the 200-char floor. " * 4
    + b"</section></body></uslm>"
)


def _zip_of(*members: tuple[str, bytes], compresslevel: int | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {} if compresslevel is None else {"compresslevel": compresslevel}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, **kwargs) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def test_a_single_member_zip_hashes_as_its_members_own_format():
    """The USLM shape: a zip wrapping one .xml member. Its digest must equal the digest of
    hashing that member directly with format inferred from its own name -- the exact
    computation a corpus caching the decompressed bytes runs on its own ingestion."""
    zip_bytes = _zip_of(("usc20@119-103.xml", XML_MEMBER))

    assert content_hash(zip_bytes, "zip") == content_hash(XML_MEMBER, "xml")


def test_it_does_not_degrade_to_hashing_the_archive_bytes_themselves():
    """The pre-fix behaviour: whatever format the zip bytes fell through to (binary
    fallback, or html/xml tag-stripping over garbage) hashed the ARCHIVE, not its content --
    so two archives of the same text at different compression levels would read as drift,
    even though nothing a corpus mirrors changed.

    Asserting only that `content_hash(zip, "zip")` differs from hashing the archive as html
    or xml is not enough to indict the bug: any distinct branch satisfies that, including
    the buggy binary fallback itself (`hashlib.sha256(zip_bytes)` also differs from
    `content_hash(zip_bytes, "html")`, whether or not #199 is fixed). The property that
    actually matters -- and that the fix must deliver -- is that two archives disagreeing
    only in HOW they were compressed hash identically."""
    low = _zip_of(("usc20@119-103.xml", XML_MEMBER), compresslevel=1)
    high = _zip_of(("usc20@119-103.xml", XML_MEMBER), compresslevel=9)

    assert hashlib.sha256(low).hexdigest() != hashlib.sha256(high).hexdigest(), (
        "test setup is broken: the two archives must actually differ at the byte level "
        "for this test to demonstrate anything")
    assert content_hash(low, "zip") == content_hash(high, "zip")


def test_a_multi_member_zip_is_refused_rather_than_silently_picking_one():
    """USLM release points are single-member; a zip that is not must not be hashed by
    guessing, which would make the choice invisible in the manifest and unreproducible by
    a corpus that unzips differently.

    `ArchiveUnreadable` is a `ValueError` subclass specifically so this assertion -- and any
    other caller checking the pre-existing contract -- keeps working; `changes.py`'s fetch
    loop is what needs the more specific type, to keep this out of `failed` (see
    tests/test_drift_reporting.py's zip-source tests)."""
    zip_bytes = _zip_of(("a.xml", XML_MEMBER), ("b.xml", XML_MEMBER))

    with pytest.raises(ValueError, match="member"):
        content_hash(zip_bytes, "zip")
    with pytest.raises(ArchiveUnreadable):
        content_hash(zip_bytes, "zip")


def test_an_empty_zip_is_refused_the_same_way_as_a_multi_member_one():
    """Zero members is the same "not exactly one" refusal as two -- there is exactly as
    little to guess from."""
    zip_bytes = _zip_of()

    with pytest.raises(ArchiveUnreadable, match="0 member"):
        content_hash(zip_bytes, "zip")


def test_bytes_that_are_not_a_zip_at_all_fall_back_to_html_rather_than_raising():
    """A `.zip` url can still serve a login page or a plain error response with a 200 --
    ordinary for civic portals (`sources.fetch.sniff`'s docstring). Before corpus-toolkit#199
    gave `.zip` its own branch, `_format_for` did not recognise the extension and this exact
    case fell through to `html`; raising here instead would make a `.zip` url the one
    extension where an unrelated response crashes the run rather than degrading to the
    same wrong-but-stable hash every other extension already falls back to."""
    not_a_zip = b"<html><body>Please log in</body></html>"

    assert content_hash(not_a_zip, "zip") == content_hash(not_a_zip, "html")


def test_a_member_that_is_itself_a_zip_is_not_recursed_into():
    """Deliberate, not an oversight: nesting is not a shape any known source has, and
    recursing silently would make a `.zip`-inside-a-`.zip` behave differently from every
    other unrecognised member extension. The inner archive hashes as html over its own raw
    bytes, exactly like any other member whose extension `ZIP_MEMBER_FORMATS` excludes."""
    inner_zip = _zip_of(("usc20@119-103.xml", XML_MEMBER))
    outer_zip = _zip_of(("inner.zip", inner_zip))

    assert content_hash(outer_zip, "zip") == content_hash(inner_zip, "html")


def test_format_for_infers_zip_from_the_url_same_as_pdf_or_xml_does():
    """A source with no declared `format:` -- the common case -- must reach the zip branch
    from its URL alone, the way `.pdf`/`.xml`/etc already do. Before this, `.zip` fell
    through `_format_for`'s undeclared-extension default straight to `"html"`, so
    OLRC's `.../xml_usc20@119-103.zip` was never a `zip` source in the detector's eyes at
    all -- it was hashed as an (unrecognisable) html page."""
    assert _format_for("https://uscode.house.gov/.../xml_usc20@119-103.zip", None) == "zip"
