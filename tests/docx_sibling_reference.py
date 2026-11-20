"""FROZEN REFERENCE: the sibling Word reader this toolkit's `corpus_toolkit.docx_to_text` replaces.

This is `src/ingest_docx.py` from OregonAI/states-government-it at commit 63fe68a (corpus-toolkit#219),
byte for byte except for this docstring and the first import line, which becomes a constant here
(MIN_PDF_WORDS lived in that repo's ingest_common). It exists only so tests/test_docx_to_text.py can
prove the toolkit reader's default text is the sibling's text for every file the four documented
defects do not touch. Do not edit it, and do not import it from the package.
"""
from __future__ import annotations

import json
import re
import subprocess

MIN_PDF_WORDS = 30


# ---- Word files (.docx) -----------------------------------------------------------------
#
# pandoc reads the file into its JSON syntax tree; this module renders the tree, so that the
# layout (tables as one line per row, lists, notes) is this corpus's and does not depend on
# pandoc's own plain-text writer, whose output changes between releases. The tree is also far
# steadier across releases than the writer's output; `check` still compares word for word, not
# byte for byte, because the pandoc that CI installs (Ubuntu's) is not the one that made the text.

_BULLET = "\u2022 "


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
            self.notes[n - 1] = " ".join(self.blocks(c)).strip()
            return f"[{n}]"
        raise ValueError(f"Word file holds an inline element this reader does not know: {t}")

    def text_lines(self, inlines) -> list[str]:
        return [" ".join(p.split()) for p in self.inlines(inlines).replace("\xa0", " ").split("\n")
                if p.strip()]

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
        lines = self.blocks(blocks, tight=True)
        pad = " " * len(marker)
        return [(marker if k == 0 else pad) + l if l else l for k, l in enumerate(lines)]

    def cell_text(self, cell) -> str:
        return " ".join(l.strip() for l in self.blocks(cell[4], tight=True) if l.strip())

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
        return lines

    def block(self, b) -> list[str]:
        t, c = b["t"], b.get("c")
        if t in ("Plain", "Para"):
            return self.text_lines(c)
        if t == "Header":
            return self.text_lines(c[2])
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
                    self.text_lines(term) + [x for d in defs for x in self.blocks(d, tight=True)]]
        if t == "Div":
            return self.blocks(c[1])
        if t == "LineBlock":
            return [l for line in c for l in self.text_lines(line)]
        if t == "CodeBlock":
            return [l.rstrip() for l in c[1].splitlines() if l.strip()]
        if t == "Table":
            return self.table(c)
        if t == "Figure":
            return self.blocks(c[2])
        if t in ("HorizontalRule", "RawBlock", "Null"):
            return []
        raise ValueError(f"Word file holds a block element this reader does not know: {t}")


def render_pandoc_ast(tree: dict) -> str:
    """A pandoc syntax tree (`pandoc -t json`) as text: one line per paragraph, heading, list
    item and table row (cells joined by ` | `), a blank line between blocks, footnotes after the
    body as `[n] text`. Pictures, rules and raw markup are not text. A construct this does not
    know is refused, so a new kind of content is noticed instead of dropped."""
    r = _Pandoc()
    lines = r.blocks(tree["blocks"])
    if r.notes:
        lines += [""] + [f"[{n}] {t}" for n, t in enumerate(r.notes, 1)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in lines)).strip("\n") + "\n"


def docx_to_text(raw: bytes) -> str:
    """The body text of a Word file, through pandoc's docx reader with tracked changes accepted
    (what the document reads as). Running headers and footers, pictures and text boxes' frames are
    not read by pandoc and are not text here (the conversion notes say so)."""
    try:
        proc = subprocess.run(["pandoc", "-f", "docx", "-t", "json", "--track-changes=accept"],
                              input=raw, capture_output=True, check=False)
    except FileNotFoundError:
        raise ValueError("pandoc is not installed: it reads the Word files (apt-get install pandoc)") from None
    if proc.returncode != 0:
        raise ValueError(f"not readable as a Word file: {proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    text = render_pandoc_ast(json.loads(proc.stdout))
    if len(text.split()) < MIN_PDF_WORDS:
        raise ValueError("Word file has no text (fewer than 30 words): make the entry a stub")
    return text


def same_words(a: str, b: str) -> bool:
    """The same words in the same order, whatever the line breaks and spacing. How `--check`
    compares a Word file's committed text with what the installed pandoc makes of it."""
    return a.split() == b.split()
