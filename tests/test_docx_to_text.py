"""Seams: corpus_toolkit.docx_to_text.docx_to_text / docx_blocks (corpus-toolkit#219).

The fixtures in tests/fixtures/docx/ are real Word files, copied byte for byte from the corpora that
hold them (states-government-it `_meta/snapshots/`, cte-oregon `_meta/snapshots/`). The sibling
reader this module replaces is frozen in tests/docx_sibling_reference.py; the default text must be
its text for every file the four defects in #219 do not touch.

Skipped without pandoc on a developer machine only: in CI (CI=true) a missing pandoc fails these
tests, so the gate cannot pass unread."""
import os
import shutil
from pathlib import Path

import pytest

import docx_sibling_reference as sibling
from corpus_toolkit import docx_to_text as dt

FIX = Path(__file__).resolve().parent / "fixtures" / "docx"

needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None and not os.environ.get("CI"),
                                  reason="pandoc not installed")


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


# Files the four defects do not touch: no Title-style first paragraph, no text box.
UNTOUCHED = ["wa-ea-185-10.docx", "wv-csr-163-2.docx", "me-cmr-29-250-ch950.docx", "wa-data-01-04-g.docx",
             "ca-simm-18b.docx", "or-perkins-plan-app-h.docx", "or-perkins-plan-app-c.docx"]


@needs_pandoc
@pytest.mark.parametrize("name", UNTOUCHED)
def test_default_text_is_the_sibling_readers_text_for_untouched_files(name):
    assert dt.docx_to_text(raw(name)) == sibling.docx_to_text(raw(name))


# ---- the pandoc requirement ----------------------------------------------------------------
def _fake_pandoc(tmp_path, monkeypatch, banner: str):
    exe = tmp_path / "pandoc"
    exe.write_text(f"#!/bin/sh\n[ \"$1\" = --version ] && echo '{banner}' && exit 0\nexit 9\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))


def test_a_pandoc_older_than_3_11_is_refused_with_the_requirement_named(tmp_path, monkeypatch):
    _fake_pandoc(tmp_path, monkeypatch, "pandoc 3.1.3")
    with pytest.raises(dt.DocxReaderError, match=r"pandoc >= 3\.11.*3\.1\.3"):
        dt.docx_to_text(b"PK\x03\x04")


def test_a_missing_pandoc_is_refused_with_the_requirement_named(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))      # an empty directory: no pandoc
    with pytest.raises(dt.DocxReaderError, match=r"pandoc >= 3\.11"):
        dt.docx_to_text(b"PK\x03\x04")


def test_a_newer_pandoc_than_the_floor_is_accepted(tmp_path, monkeypatch):
    _fake_pandoc(tmp_path, monkeypatch, "pandoc 3.12.1")
    with pytest.raises(dt.DocxReaderError, match="not readable as a Word file"):   # got past the version gate
        dt.docx_to_text(b"PK\x03\x04")


@needs_pandoc
def test_bytes_that_are_not_a_word_file_are_refused_not_guessed():
    with pytest.raises(ValueError, match="not readable as a Word file"):
        dt.docx_to_text(b"PK\x03\x04 not really a word file")


@needs_pandoc
def test_a_word_file_with_no_text_is_refused_so_the_entry_can_be_a_stub():
    with pytest.raises(ValueError, match="no text"):
        dt.docx_to_text(raw("wa-ea-185-10.docx"), min_words=10_000)


# ---- structure: kind and heading level -------------------------------------------------------
@needs_pandoc
def test_blocks_carry_heading_levels_from_the_heading_styles():
    blocks = dt.docx_blocks(raw("or-perkins-plan-app-h.docx"))
    assert [(b.level, b.text) for b in blocks if b.kind == "heading"] == [
        (1, "Appendix H: Perkins Budget"), (2, "State Name: OREGON"), (2, "Fiscal Year (FY): 2023")]
    assert blocks[0] == dt.Block("heading", "Appendix H: Perkins Budget", 1)
    row = next(b for b in blocks if b.text.startswith("Line Number |"))
    assert (row.kind, row.level) == ("table_row", None)
    assert next(b for b in blocks if b.text.startswith("4 | "))


@needs_pandoc
def test_heading_levels_never_reach_the_default_text_as_markdown_markers():
    # the toolkit chunks documents on "### " headings, so a "#" the reader invented would mis-chunk them
    text = dt.docx_to_text(raw("or-perkins-plan-app-h.docx"))
    assert text.startswith("Appendix H: Perkins Budget\n")
    assert not any(line.startswith("#") for line in text.splitlines())


@needs_pandoc
@pytest.mark.parametrize("name", sorted(p.name for p in FIX.glob("*.docx")))
def test_blocks_hold_the_same_words_in_the_same_order_as_the_default_text(name):
    blocks = dt.docx_blocks(raw(name))
    assert [w for b in blocks for w in b.text.split()] == dt.docx_to_text(raw(name)).split()


# ---- the renderer, on hand-built syntax trees (no pandoc needed) ------------------------------
def test_an_unknown_construct_in_the_syntax_tree_is_refused():
    tree = {"pandoc-api-version": [1, 23], "meta": {}, "blocks": [{"t": "Mystery", "c": []}]}
    with pytest.raises(ValueError, match="Mystery"):
        dt.render_pandoc_ast(tree)


def _lists_notes_and_inlines_tree():
    s = lambda w: {"t": "Str", "c": w}
    sp = {"t": "Space"}
    para = lambda *w: {"t": "Para", "c": list(w)}
    return {"meta": {}, "blocks": [
        {"t": "Header", "c": [2, ["", [], []], [s("Part"), sp, s("one")]]},
        para(s("Intro"), sp, {"t": "Note", "c": [para(s("A"), sp, s("note"))]}),
        {"t": "OrderedList", "c": [[3, {"t": "LowerAlpha"}, {"t": "OneParen"}],
                                   [[para(s("first"))], [para(s("second")), {"t": "BulletList", "c": [[para(s("inner"))]]}]]]},
        {"t": "HorizontalRule"},
        para(s("Link:"), sp, {"t": "Link", "c": [["", [], []], [s("text")], ["https://x", ""]]},
             {"t": "LineBreak"}, {"t": "Image", "c": [["", [], []], [s("logo")], ["a.png", ""]]}, s("end")),
    ]}


def test_the_rendering_of_lists_notes_and_inlines():
    assert dt.render_pandoc_ast(_lists_notes_and_inlines_tree()) == (
        "Part one\n\nIntro [1]\n\nc) first\nd) second\n   • inner\n\nLink: text\nend\n\n[1] A note\n")


def test_blocks_of_the_same_tree_keep_kind_level_and_marker():
    assert dt._render(_lists_notes_and_inlines_tree())[1] == [
        dt.Block("heading", "Part one", 2),
        dt.Block("paragraph", "Intro [1]"),
        dt.Block("list_item", "c) first"),
        dt.Block("list_item", "d) second"),
        dt.Block("list_item", "• inner"),
        dt.Block("paragraph", "Link: text\nend"),
        dt.Block("note", "[1] A note"),
    ]


def test_same_words_in_a_different_layout_are_the_same_text():
    assert dt.same_words("a | b\n\nc d\n", "a  |  b c\nd")
    assert not dt.same_words("a | b c", "a | b d")
    assert not dt.same_words("a b", "a b c")


# ---- defect 1 of #219: a Title-style first paragraph is moved into metadata by pandoc ---------
@needs_pandoc
def test_a_title_style_first_paragraph_is_restored_at_the_start_of_the_text():
    # ut-dts-4000-0008.docx: the first paragraph of word/document.xml is styled "Title" ("Generative
    # AI Policy"); the sibling reader never read pandoc's metadata and began at "DTS POLICY 4000-0008".
    text = dt.docx_to_text(raw("ut-dts-4000-0008.docx"))
    assert text.startswith("Generative AI Policy\n\nDTS POLICY 4000-0008\n\n")
    assert text == "Generative AI Policy\n\n" + sibling.docx_to_text(raw("ut-dts-4000-0008.docx"))
    assert dt.docx_blocks(raw("ut-dts-4000-0008.docx"))[0] == dt.Block("title", "Generative AI Policy")


def test_front_matter_styles_pandoc_moves_into_metadata_are_restored_in_reading_order():
    s = lambda w: {"t": "Str", "c": w}
    inl = lambda *w: {"t": "MetaInlines", "c": list(w)}
    tree = {"meta": {"title": inl(s("T")), "subtitle": inl(s("Sub")),
                     "author": {"t": "MetaList", "c": [inl(s("A")), inl(s("B"))]},
                     "date": inl(s("2024")),
                     "abstract": {"t": "MetaBlocks", "c": [{"t": "Para", "c": [s("Abs")]}]}},
            "blocks": [{"t": "Para", "c": [s("Body")]}]}
    assert dt.render_pandoc_ast(tree) == "T\n\nSub\n\nA\n\nB\n\n2024\n\nAbs\n\nBody\n"
    assert [b.kind for b in dt._render(tree)[1]] == ["title"] + ["paragraph"] * 6


def test_metadata_this_reader_does_not_know_is_refused_not_dropped():
    tree = {"meta": {"mystery": {"t": "MetaMap", "c": {}}}, "blocks": []}
    with pytest.raises(ValueError, match="mystery"):
        dt.render_pandoc_ast(tree)


# ---- defects 3 and 4 of #219: text boxes are dropped, and a shape inside a word splits it -------
def _lines(name: str) -> list[str]:
    return dt.docx_to_text(raw(name)).splitlines()


@needs_pandoc
def test_a_text_box_pandoc_does_not_read_is_kept_once_and_with_its_split_runs_joined():
    # or-eis-ai-attachment-a.docx: the cover's title sits in a text box whose runs are split inside a
    # word ("A" + "rtificia" + "l"); the sibling reader dropped the whole box.
    lines = _lines("or-eis-ai-attachment-a.docx")
    assert lines.count("Agency Artificial Intelligence Adoption Strategy") == 1
    assert "Agency Artificial Intelligence Adoption Strategy" not in sibling.docx_to_text(
        raw("or-eis-ai-attachment-a.docx")).splitlines()


@needs_pandoc
def test_a_shape_between_two_runs_of_a_word_no_longer_splits_the_word():
    # The Oregon plan's cover (first page of or-perkins-plan.docx, cte-oregon): "2024 – 202" + a
    # floating rectangle + "8" in one paragraph. pandoc cut the paragraph at the rectangle.
    lines = _lines("or-perkins-plan-cover-and-text-boxes.docx")
    assert lines[:3] == ["2024 – 2028", "", "Oregon’s CTE State Plan"]
    assert "2024 – 202" in sibling.docx_to_text(raw("or-perkins-plan-cover-and-text-boxes.docx")).splitlines()


@needs_pandoc
def test_text_boxes_inside_group_shapes_are_kept_and_the_ones_pandoc_read_are_not_doubled():
    lines = _lines("or-perkins-plan-cover-and-text-boxes.docx")
    for kept in ("Figure 2: Oregon Career Connected Learning Framework",           # group shape, was dropped
                 "Photo Courtesy of Clackamas Community College",                  # group shape, was dropped
                 "Photo courtesy of Southwestern Oregon Community College"):       # plain box, was kept
        assert lines.count(kept) == 1, kept
    assert lines.count("Vision for CTE") == 1                                     # plain box, was kept


# ---- the same two fixes on minimal hand-built packages ------------------------------------------
import io
import zipfile

_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:v="urn:schemas-microsoft-com:vml" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
       'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')


def _docx(body: str, styles: str | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   + ('<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>' if styles else "")
                   + "</Types>")
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        if styles:
            z.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="r2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
            z.writestr("word/styles.xml", f'<w:styles {_NS}>{styles}</w:styles>')
        z.writestr("word/document.xml", f"<w:document {_NS}><w:body>{body}</w:body></w:document>")
    return out.getvalue()


def _p(text: str, style: str | None = None) -> str:
    st = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{st}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _box(*paragraphs: str) -> str:
    return f"<w:r><w:pict><v:shape><v:textbox><w:txbxContent>{''.join(paragraphs)}</w:txbxContent></v:textbox></v:shape></w:pict></w:r>"


@needs_pandoc
def test_a_text_box_with_no_fallback_in_a_table_cell_and_a_nested_box_are_kept_in_order():
    inner = _box(_p("inner note"))
    body = ("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r>"
            + _box(_p("boxed one"), "<w:p>" + inner + "<w:r><w:t>boxed two</w:t></w:r></w:p>")
            + "</w:p></w:tc><w:tc>" + _p("next cell") + "</w:tc></w:tr></w:tbl>" + _p("after the table"))
    text = dt.docx_to_text(_docx(body), min_words=1)
    assert text == "cell boxed one boxed two inner note | next cell\n\nafter the table\n"
    assert [b.kind for b in dt.docx_blocks(_docx(body))] == ["table_row", "paragraph"]


@needs_pandoc
def test_a_word_split_by_a_text_box_in_a_plain_paragraph_is_joined_and_the_box_follows_the_paragraph():
    body = '<w:p><w:r><w:t>202</w:t></w:r>' + _box(_p("boxed")) + '<w:r><w:t>8 plan</w:t></w:r></w:p>' + _p("next")
    assert dt.docx_to_text(_docx(body), min_words=1) == "2028 plan\n\nboxed\n\nnext\n"


STYLES = ('<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
          '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>')


@needs_pandoc
def test_every_title_paragraph_is_kept_where_it_stands_not_only_the_last():
    # pandoc keeps only the last of several Title paragraphs; a two-line title lost its first line
    body = _p("Agency Adoption Strategy", "Title") + _p("Template and Instructions", "Title") + _p("Body text")
    assert dt.docx_to_text(_docx(body, STYLES), min_words=1) == (
        "Agency Adoption Strategy\n\nTemplate and Instructions\n\nBody text\n")
    assert [(b.kind, b.text) for b in dt.docx_blocks(_docx(body, STYLES))] == [
        ("title", "Agency Adoption Strategy"), ("title", "Template and Instructions"), ("paragraph", "Body text")]


@needs_pandoc
def test_a_title_style_defined_under_another_id_is_found_by_its_name():
    styles = '<w:style w:type="paragraph" w:styleId="DocName"><w:name w:val="Title"/></w:style>'
    assert dt.docx_to_text(_docx(_p("The name", "DocName") + _p("Body"), styles), min_words=1) == "The name\n\nBody\n"


@needs_pandoc
def test_a_package_with_no_text_box_and_no_front_matter_reaches_pandoc_untouched():
    pkg = _docx(_p("plain") + _p("Heading", "Heading1"), STYLES)
    assert dt.prepare(pkg) is pkg
