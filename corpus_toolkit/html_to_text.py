"""Extract plain text from an HTML/XML snapshot, for hashing and quote
verification. Generic tag-stripping; no corpus-specific knowledge."""
import html
import re


def html_to_text(raw: bytes) -> str:
    m = re.search(rb"charset=([\w-]+)", raw[:2048])
    text = raw.decode(m.group(1).decode() if m else "utf-8", errors="replace")
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text))
