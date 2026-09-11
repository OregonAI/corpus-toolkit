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
import io
import zipfile

import pytest

from corpus_toolkit.repo import content_hash
from corpus_toolkit.sources.changes import _format_for

# Long enough that content_hash's extracted-text path is taken, not the <200-char
# raw-byte fallback -- the same margin test_volatile_patterns.py's PAGE_V7 keeps.
XML_MEMBER = (
    b'<?xml version="1.0"?>\n<uslm><body><section>'
    + b"Some enacted statutory text, repeated so extraction clears the 200-char floor. " * 4
    + b"</section></body></uslm>"
)


def _zip_of(*members: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
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
    so two archives of the same text at different compression levels would read as drift."""
    zip_bytes = _zip_of(("usc20@119-103.xml", XML_MEMBER))

    assert content_hash(zip_bytes, "zip") != content_hash(zip_bytes, "html")
    assert content_hash(zip_bytes, "zip") != content_hash(zip_bytes, "xml")


def test_a_multi_member_zip_is_refused_rather_than_silently_picking_one():
    """USLM release points are single-member; a zip that is not must not be hashed by
    guessing, which would make the choice invisible in the manifest and unreproducible by
    a corpus that unzips differently."""
    zip_bytes = _zip_of(("a.xml", XML_MEMBER), ("b.xml", XML_MEMBER))

    with pytest.raises(ValueError, match="member"):
        content_hash(zip_bytes, "zip")


def test_format_for_infers_zip_from_the_url_same_as_pdf_or_xml_does():
    """A source with no declared `format:` -- the common case -- must reach the zip branch
    from its URL alone, the way `.pdf`/`.xml`/etc already do. Before this, `.zip` fell
    through `_format_for`'s undeclared-extension default straight to `"html"`, so
    OLRC's `.../xml_usc20@119-103.zip` was never a `zip` source in the detector's eyes at
    all -- it was hashed as an (unrecognisable) html page."""
    assert _format_for("https://uscode.house.gov/.../xml_usc20@119-103.zip", None) == "zip"
