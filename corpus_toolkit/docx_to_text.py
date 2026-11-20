"""Word files (.docx) as text, read with pandoc (corpus-toolkit#219).

pandoc reads the file into its JSON syntax tree; this module renders the tree, so the layout
(tables as one line per row, lists, notes) is the platform's and does not depend on pandoc's own
plain-text writer, whose output changes between releases. Generic; no corpus-specific knowledge.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import zipfile
from dataclasses import dataclass
from xml.dom import minidom
from xml.parsers.expat import ExpatError

MIN_WORDS = 30   # default for `docx_to_text`: a file with fewer words is refused as having no text

_BULLET = "\u2022 "


@dataclass(frozen=True)
class Block:
    """One run of lines the reader renders as a unit, in document order.

    `kind` is one of `title` (a Title-style first paragraph), `heading` (with `level`, 1 for
    Heading 1, as the document's styles say), `paragraph`, `list_item` (text starts with its
    marker), `table_row` (cells joined by ` | `), `code` and `note` (`[n] text`); `level` is None
    for every kind but `heading`. The words of all blocks, in order, are the words of the default
    text: this is the same rendering with its structure kept, so a consumer can cut on headings
    without the text carrying any `#` marker."""
    kind: str
    text: str
    level: int | None = None


def _roman(n: int) -> str:
    out = ""
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                       (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out, n = out + sym, n - value
    return out


def _alpha(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return out


def _marker(n: int, style: str, delim: str) -> str:
    body = {"LowerAlpha": _alpha(n), "UpperAlpha": _alpha(n).upper(), "LowerRoman": _roman(n),
            "UpperRoman": _roman(n).upper()}.get(style, str(n))
    return {"OneParen": f"{body})", "TwoParens": f"({body})"}.get(delim, f"{body}.") + " "


class _Pandoc:
    """Renders one pandoc syntax tree (the `blocks` list) to lines, collecting footnotes."""

    def __init__(self):
        self.notes: list[str] = []
        self.out: list[Block] = []     # structure, in document order (see `Block`)
        self._quiet = 0                # >0 while rendering text that is folded into another block
        self._in_item = 0

    def emit(self, kind: str, lines: list[str], level: int | None = None) -> None:
        if lines and not self._quiet:
            self.out.append(Block(kind, "\n".join(lines), level))

    def inlines(self, items) -> str:
        return "".join(self.inline(i) for i in items)

    def inline(self, i) -> str:
        t, c = i["t"], i.get("c")
        if t == "Str":
            return c
        if t == "Space" or t == "SoftBreak":
            return " "
        if t == "LineBreak":
            return "\n"
        if t in ("Emph", "Strong", "Underline", "Strikeout", "Superscript", "Subscript", "SmallCaps"):
            return self.inlines(c)
        if t == "Span":
            return self.inlines(c[1])
        if t == "Link":
            return self.inlines(c[1])
        if t == "Cite":
            return self.inlines(c[1])
        if t == "Quoted":
            q = ("\u201c", "\u201d") if c[0]["t"] == "DoubleQuote" else ("\u2018", "\u2019")
            return q[0] + self.inlines(c[1]) + q[1]
        if t in ("Code", "Math"):
            return c[1]
        if t in ("Image", "RawInline"):
            return ""                     # a picture or markup is not the instrument's words
        if t == "Note":
            n = len(self.notes) + 1
            self.notes.append("")         # reserve the number: a note may hold a note
            self._quiet += 1
            self.notes[n - 1] = " ".join(self.blocks(c)).strip()
            self._quiet -= 1
            return f"[{n}]"
        raise DocxReaderError(f"Word file holds an inline element this reader does not know: {t}")

    def text_lines(self, inlines, kind: str | None = None, level: int | None = None) -> list[str]:
        text = self.inlines(inlines)
        front = _FRONT_MARK.search(text)          # a paragraph `prepare` restyled: see above
        if front:
            text, kind = _FRONT_MARK.sub("", text), ("title" if front.group(1) == "title" else "paragraph")
        lines = [" ".join(p.split()) for p in text.replace("\xa0", " ").split("\n") if p.strip()]
        if kind:
            self.emit(kind, lines, level)
        return lines

    def blocks(self, blocks, tight=False) -> list[str]:
        """Lines of a block list; blocks are separated by a blank line unless `tight`."""
        out: list[str] = []
        for b in blocks:
            lines = self.block(b)
            if lines:
                if out and not tight:
                    out.append("")
                out += lines
        return out

    def item(self, marker: str, blocks) -> list[str]:
        first = len(self.out)
        self._in_item += 1
        lines = self.blocks(blocks, tight=True)
        self._in_item -= 1
        if len(self.out) > first and not self._quiet:       # the marker belongs to the item's first line
            head = self.out[first]
            self.out[first] = Block(head.kind, marker + head.text, head.level)
        pad = " " * len(marker)
        return [(marker if k == 0 else pad) + l if l else l for k, l in enumerate(lines)]

    def cell_text(self, cell) -> str:
        self._quiet += 1
        text = " ".join(l.strip() for l in self.blocks(cell[4], tight=True) if l.strip())
        self._quiet -= 1
        return text

    def table(self, c) -> list[str]:
        _, caption, _, head, bodies, foot = c
        rows = list(head[1])
        for body in bodies:
            rows += body[2] + body[3]
        rows += foot[1]
        lines = self.blocks(caption[1], tight=True)
        for row in rows:
            cells = [self.cell_text(cell) for cell in row[1]]
            if any(cells):
                lines.append(" | ".join(cells))
                self.emit("table_row", lines[-1:])
        return lines

    # Word styles pandoc moves out of the body into document metadata, in reading order. A
    # reader that renders only the body would drop a Title-style first paragraph.
    FRONT_MATTER = ("title", "subtitle", "author", "date", "abstract")

    def front_matter(self, meta: dict) -> list[list[str]]:
        """Line groups for the metadata, one per paragraph; a key this does not know is refused."""
        unknown = sorted(set(meta) - set(self.FRONT_MATTER))
        if unknown:
            raise DocxReaderError(f"Word file holds metadata this reader does not know: {', '.join(unknown)}")
        groups: list[list[str]] = []
        for key in self.FRONT_MATTER:
            if key in meta:
                for value in (meta[key]["c"] if meta[key]["t"] == "MetaList" else [meta[key]]):
                    groups += self.meta_value(value, "title" if key == "title" else "paragraph")
        return groups

    def meta_value(self, v, kind: str) -> list[list[str]]:
        t, c = v["t"], v.get("c")
        if t == "MetaInlines":
            lines = self.text_lines(c, kind)
            return [lines] if lines else []
        if t == "MetaString":
            lines = [" ".join(c.split())] if c.strip() else []
            self.emit(kind, lines)
            return [lines] if lines else []
        if t == "MetaBlocks":
            lines = self.blocks(c)
            return [lines] if lines else []
        raise DocxReaderError(f"Word file holds metadata this reader does not know: {t}")

    def block(self, b) -> list[str]:
        t, c = b["t"], b.get("c")
        if t in ("Plain", "Para"):
            return self.text_lines(c, "list_item" if self._in_item else "paragraph")
        if t == "Header":
            return self.text_lines(c[2], "heading", c[0])
        if t == "BlockQuote":
            return self.blocks(c)
        if t == "BulletList":
            return [l for item in c for l in self.item(_BULLET, item)]
        if t == "OrderedList":
            (start, style, delim), items = c
            return [l for k, item in enumerate(items)
                    for l in self.item(_marker(start + k, style["t"], delim["t"]), item)]
        if t == "DefinitionList":
            return [l for term, defs in c for l in
                    self.text_lines(term, "paragraph") + [x for d in defs for x in self.blocks(d, tight=True)]]
        if t == "Div":
            return self.blocks(c[1])
        if t == "LineBlock":
            lines = [l for line in c for l in self.text_lines(line)]
            self.emit("paragraph", lines)
            return lines
        if t == "CodeBlock":
            lines = [l.rstrip() for l in c[1].splitlines() if l.strip()]
            self.emit("code", lines)
            return lines
        if t == "Table":
            return self.table(c)
        if t == "Figure":
            return self.blocks(c[2])
        if t in ("HorizontalRule", "RawBlock", "Null"):
            return []
        raise DocxReaderError(f"Word file holds a block element this reader does not know: {t}")


def _render(tree: dict) -> tuple[str, list[Block]]:
    r = _Pandoc()
    lines: list[str] = []
    for group in r.front_matter(tree.get("meta", {})):
        lines += ([""] if lines else []) + group
    body = r.blocks(tree["blocks"])
    lines += ([""] if lines and body else []) + body
    if r.notes:
        lines += [""] + [f"[{n}] {t}" for n, t in enumerate(r.notes, 1)]
        r.out += [Block("note", f"[{n}] {t}") for n, t in enumerate(r.notes, 1)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in lines)).strip("\n") + "\n"
    return text, r.out


def render_pandoc_ast(tree: dict) -> str:
    """A pandoc syntax tree (`pandoc -t json`) as text: one line per paragraph, heading, list
    item and table row (cells joined by ` | `), a blank line between blocks, footnotes after the
    body as `[n] text`. Pictures, rules and raw markup are not text. A construct this does not
    know is refused, so a new kind of content is noticed instead of dropped."""
    return _render(tree)[0]


# ---- text boxes -------------------------------------------------------------------------------
#
# pandoc reads some text boxes, drops others (every one inside a group shape, the usual home of a
# caption or a photo credit), and cuts the paragraph that holds a box in two at the box, so a word
# whose runs straddle a floating shape ("202" + shape + "8" on the Oregon plan's cover) comes out as
# two paragraphs. The reader takes the boxes out of pandoc's hands: each shape that holds a text
# box is removed from its paragraph and the box's paragraphs are put right after that paragraph, once
# each (a shape carries its box twice, in `mc:Choice` and again in the VML `mc:Fallback`; the Choice
# is the one kept). pandoc then sees an unbroken paragraph followed by ordinary paragraphs. A file
# with no text box and no front-matter style is handed to pandoc byte for byte as it came.

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_SHAPES = {(_MC, "AlternateContent"), (_W, "drawing"), (_W, "pict")}


def _is(node, ns: str, local: str) -> bool:
    return node.nodeType == node.ELEMENT_NODE and node.namespaceURI == ns and node.localName == local


def _elements(node) -> list:
    return [c for c in node.childNodes if c.nodeType == c.ELEMENT_NODE]


def _holds_box(node) -> bool:
    return bool(node.getElementsByTagNameNS(_W, "txbxContent"))


def _top_boxes(node) -> list:
    """The text boxes under `node` that are not themselves inside another text box, in order."""
    return [node] if _is(node, _W, "txbxContent") else [b for c in _elements(node) for b in _top_boxes(c)]


def _boxes_of(shape) -> list:
    """The blocks of the shape's text boxes, each box once: an AlternateContent's first Choice that
    holds a box, else its Fallback; any other shape's own boxes."""
    if _is(shape, _MC, "AlternateContent"):
        branches = [c for c in _elements(shape) if _holds_box(c)]
        shape = next((c for c in branches if c.localName == "Choice"), branches[0])
    blocks: list = []
    for box in _top_boxes(shape):
        holder = box.cloneNode(True)
        _lift(holder, None)                       # a box inside this box is lifted the same way
        blocks += [b for b in _elements(holder)
                   if not _is(b, _W, "p") or b.getElementsByTagNameNS(_W, "t")]
    return blocks


def _lift(parent, extras) -> None:
    """Remove every shape holding a text box under `parent`; `extras` collects the boxes' blocks for
    the nearest enclosing paragraph, which places them after itself."""
    for child in _elements(parent):
        if (child.namespaceURI, child.localName) in _SHAPES and _holds_box(child):
            if extras is not None:
                extras += _boxes_of(child)
            parent.removeChild(child)
        elif _is(child, _W, "p"):
            after: list = []
            _lift(child, after)
            ref = child.nextSibling
            for block in after:
                parent.insertBefore(block, ref)
        else:
            _lift(child, extras)


# ---- front-matter styles ------------------------------------------------------------------------
#
# pandoc moves a paragraph styled Title, Subtitle, Author, Date or Abstract out of the body into
# document metadata, keeps only the LAST of several Title paragraphs, so a reader that renders only the body
# loses them. Each such paragraph is restyled to Normal in place, with a marker run at its
# start (private-use characters around the style name, which survive pandoc as text; an empty
# bookmark does not, pandoc drops it) that the renderer strips and reads back as the block's kind,
# so the paragraph is read where it stands, every one of them (a two-line title in a text box lost
# its first line to the overwrite). Whatever metadata pandoc still produces is restored by
# `front_matter`.

_FRONT_STYLES = ("title", "subtitle", "author", "date", "abstract")
_FRONT_MARK = re.compile("\ue000\ue001front-(\\w+)\ue001\ue000")


def _front_style_ids(zin: zipfile.ZipFile) -> dict[str, str]:
    """style id (lower case) -> front-matter style name, for the paragraph styles that pandoc
    treats as metadata. A pStyle with no definition in styles.xml is taken by its id."""
    defined: dict[str, str] = {}
    try:
        styles = minidom.parseString(zin.read("word/styles.xml"))
    except (KeyError, ExpatError):
        styles = None
    for st in (styles.getElementsByTagNameNS(_W, "style") if styles else []):
        name = next((n.getAttributeNS(_W, "val") for n in st.getElementsByTagNameNS(_W, "name")), "")
        defined[st.getAttributeNS(_W, "styleId").lower()] = name.lower()
    ids = {sid: name for sid, name in defined.items() if name in _FRONT_STYLES}
    ids.update({n: n for n in _FRONT_STYLES if n not in defined})
    return ids


def _mark_front(dom, ids: dict[str, str]) -> int:
    count = 0
    for p in dom.getElementsByTagNameNS(_W, "p"):
        ppr = next((c for c in _elements(p) if _is(c, _W, "pPr")), None)
        pstyle = next((c for c in _elements(ppr) if _is(c, _W, "pStyle")), None) if ppr else None
        style = ids.get(pstyle.getAttributeNS(_W, "val").lower()) if pstyle else None
        if style is None:
            continue
        ppr.removeChild(pstyle)
        count += 1
        run = dom.createElementNS(_W, "w:r")
        text = dom.createElementNS(_W, "w:t")
        text.appendChild(dom.createTextNode(f"\ue000\ue001front-{style}\ue001\ue000"))
        run.appendChild(text)
        p.insertBefore(run, ppr.nextSibling if ppr else p.firstChild)
    return count


def prepare(raw: bytes) -> bytes:
    """The Word file with its text boxes lifted out of their shapes and its front-matter paragraphs
    marked (see above); `raw` itself when it has neither, or when it is not a readable package
    (pandoc then refuses it by name)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zin:
            document = zin.read("word/document.xml")
            ids = _front_style_ids(zin)
            dom = minidom.parseString(document)
            had_box = b"txbxContent" in document
            if had_box:
                _lift(dom.documentElement, None)
            marked = _mark_front(dom, ids)
            if not had_box and not marked:
                return raw
            lifted = dom.toxml(encoding="utf-8")
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    zout.writestr(item, lifted if item.filename == "word/document.xml"
                                  else zin.read(item.filename))
            return out.getvalue()
    except (zipfile.BadZipFile, KeyError, ExpatError, NotImplementedError, ValueError, OSError, EOFError):
        return raw  # pandoc refuses what is not a Word file, by name (DocxReaderError)


PANDOC_MIN_VERSION = (3, 11)
_PANDOC_NEEDS = ("this reader needs pandoc >= 3.11 on PATH (3.1.3, Ubuntu 24.04's package, drops text from "
                 "some Word files; https://github.com/jgm/pandoc/releases)")


class DocxReaderError(ValueError):
    """A Word file the reader cannot turn into text, or a pandoc it cannot use. A ValueError, so
    a caller that already refuses bad input with ValueError keeps working."""


def pandoc_version() -> tuple[int, ...]:
    """The installed pandoc's version as a tuple, or a DocxReaderError naming the requirement when
    pandoc is missing, unrunnable, or older than PANDOC_MIN_VERSION."""
    try:
        proc = subprocess.run(["pandoc", "--version"], capture_output=True, check=False)
    except FileNotFoundError:
        raise DocxReaderError(f"pandoc is not installed: {_PANDOC_NEEDS}") from None
    m = re.match(r"pandoc(?:\.exe)? (\d+(?:\.\d+)*)", proc.stdout.decode("utf-8", "replace"))
    if proc.returncode != 0 or not m:
        raise DocxReaderError(f"pandoc did not report a version: {_PANDOC_NEEDS}")
    version = tuple(int(n) for n in m.group(1).split("."))
    if version < PANDOC_MIN_VERSION:
        raise DocxReaderError(f"pandoc {m.group(1)} is installed: {_PANDOC_NEEDS}")
    return version


def _tree(raw: bytes) -> dict:
    """pandoc's JSON syntax tree for the Word file, tracked changes accepted (what the document
    reads as)."""
    pandoc_version()
    proc = subprocess.run(["pandoc", "-f", "docx", "-t", "json", "--track-changes=accept"],
                          input=prepare(raw), capture_output=True, check=False)
    if proc.returncode != 0:
        raise DocxReaderError(f"not readable as a Word file: {proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return json.loads(proc.stdout)


def docx_to_text(raw: bytes, min_words: int = MIN_WORDS) -> str:
    """The body text of a Word file, through pandoc's docx reader with tracked changes accepted
    (what the document reads as). Running headers and footers and pictures are not text here (the
    conversion notes say so). Raises DocxReaderError (a ValueError) for bytes that are not a Word
    file, a pandoc below 3.11, or a file with fewer than `min_words` words, so the caller can
    treat it as having no extractable text."""
    text = _render(_tree(raw))[0]
    if len(text.split()) < min_words:
        raise DocxReaderError(f"Word file has no text (fewer than {min_words} words): make the entry a stub")
    return text


def docx_blocks(raw: bytes) -> list[Block]:
    """The same reading as `docx_to_text`, with its structure kept: a list of `Block`s in
    document order, each with its kind and, for a heading, its level. Raises DocxReaderError for
    bytes that are not a Word file or a pandoc below 3.11; it does not apply the minimum-words
    refusal (the caller decides what a short file is)."""
    return _render(_tree(raw))[1]


def same_words(a: str, b: str) -> bool:
    """The same words in the same order, whatever the line breaks and spacing. For a caller that
    compares text it stored earlier with what the installed pandoc makes of the file now, so a
    change of line layout alone is not a difference."""
    return a.split() == b.split()
