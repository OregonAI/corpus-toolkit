"""Generic corpus-repo primitives: frontmatter parsing, content-file
discovery, snapshot hashing, git-diff scoping. Ported from
oregon-policy-repo/src/repo_lib.py with all Oregon-specific constants
(CONTENT_DIRS, DIR_DOC_TYPE, snapshot slicing) replaced by config-driven
equivalents — see corpus_toolkit/config.py and docs/reference-architecture.md."""
from __future__ import annotations

import datetime
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import yaml

from corpus_toolkit.config import CorpusConfig

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

NON_CONTENT_NAMES = {"CHANGELOG.md"}


def repo_state(root: Path) -> str:
    """Cheap fingerprint of the corpus: HEAD commit + hash of `git status`
    porcelain. Used as a cache-invalidation key by the MCP framework's FTS
    index and any other derived-data cache."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True).stdout
    return head + ":" + hashlib.sha256(status.encode()).hexdigest()[:16]


def yaml_load(text: str):
    """yaml.safe_load via the libyaml-backed CSafeLoader when available —
    matters at scale (large catalogs), falls back to pure-Python SafeLoader."""
    return yaml.load(text, Loader=_YAML_LOADER)


def _stringify_dates(value):
    """YAML parses bare dates into datetime.date; canonicalize to ISO strings."""
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _stringify_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_dates(v) for v in value]
    return value


def parse_frontmatter(path: Path):
    """Return (frontmatter dict, body str). Raises ValueError if malformed."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter (file must start with ---)")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("unterminated YAML frontmatter")
    fm = yaml.safe_load(text[4:end])
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a YAML mapping")
    body = text[end + 4:]
    return _stringify_dates(fm), body


_PUNCT_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def ws_only(s: str) -> str:
    """Collapse whitespace runs WITHOUT touching punctuation."""
    return re.sub(r"\s+", " ", s).strip()


def normalize_ws(s: str) -> str:
    """Collapse whitespace runs to single spaces and map curly quotes/apostrophes
    to straight ones, so punctuation style never causes a false mismatch."""
    return re.sub(r"\s+", " ", s.translate(_PUNCT_MAP)).strip()


FULLTEXT_RE = re.compile(r"^## Full text\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def extract_fulltext(body: str):
    """Return the '## Full text' section body, or None if absent."""
    m = FULLTEXT_RE.search(body)
    return m.group(1) if m else None


VERBATIM_RE = re.compile(r'\*\*\[VERBATIM\]\*\*\s*"(.*?)"', re.DOTALL)


def extract_verbatim_quotes(body: str):
    """Return the quoted text of every **[VERBATIM]** "..." block (a lighter-weight
    authoring convention than a full '## Full text' section), blockquote markers
    stripped."""
    cleaned = re.sub(r"^\s*>\s?", "", body, flags=re.MULTILINE)
    return [m.group(1) for m in VERBATIM_RE.finditer(cleaned)]


# Byte patterns that change on every fetch without any content change (session ids,
# CDN tokens). Strip before hashing so hash drift means real content drift.
VOLATILE_PATTERNS: list[bytes] = []


def normalize_volatile(data: bytes) -> bytes:
    for pat in VOLATILE_PATTERNS:
        data = re.sub(pat, b"", data)
    return data


def content_hash(raw: bytes, fmt: str) -> str:
    """Content hash of a freshly-fetched source: sha256 of the whitespace-normalized
    extracted text (pdftotext for PDFs, tag-stripping for HTML/XML). Falls back to
    the raw-byte hash when extraction yields <200 chars (e.g. image-only scans)."""
    if fmt == "pdf":
        proc = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw,
                              capture_output=True, check=False)
        text = proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""
    elif fmt in ("html", "xml"):
        from corpus_toolkit.html_to_text import html_to_text
        text = html_to_text(normalize_volatile(raw))
    else:
        # binary formats with no text extractor (xls/xlsx/docx): raw-byte hash
        return hashlib.sha256(raw).hexdigest()
    norm = normalize_ws(text)
    if len(norm) >= 200:
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return hashlib.sha256(raw).hexdigest()


def hash_snapshot(doc_id: str, fmt: str, snapshot_dir: Path) -> str:
    """CI-stable content hash: sha256 of the whitespace-normalized text already
    committed in <id>.txt (produced once at ingestion time), never re-derived from
    the source at verification time (poppler/text-extraction output can vary by
    machine/version). Falls back to the raw source file's bytes if no .txt exists
    or it's too short to be meaningful (image-only scans)."""
    raw = (snapshot_dir / f"{doc_id}.{fmt}").read_bytes()
    txt_path = snapshot_dir / f"{doc_id}.txt"
    if txt_path.is_file():
        norm = normalize_ws(txt_path.read_text(encoding="utf-8", errors="replace"))
        if len(norm) >= 200:
            return hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return hashlib.sha256(raw).hexdigest()


def _is_content_path(config: CorpusConfig, p: Path) -> bool:
    """True if p is a content document (under a content root, .md, not _index/CHANGELOG)."""
    if p.suffix != ".md" or p.name.startswith("_") or p.name in NON_CONTENT_NAMES:
        return False
    try:
        rel = p.resolve().relative_to(config.root)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in config.content_dirs


def content_files(config: CorpusConfig):
    """Yield every content document (excludes _index.md and CHANGELOG.md)."""
    for d in config.content_dirs:
        root = config.root / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            if p.name.startswith("_") or p.name in NON_CONTENT_NAMES:
                continue
            yield p


def changed_content_files(config: CorpusConfig, base_ref: str | None = None):
    """Content files added/modified relative to base_ref (default: merge-base with
    origin/main, else HEAD~1). Includes uncommitted working-tree changes. Returns a
    sorted list of existing paths — deletions are dropped (nothing to verify)."""
    def _git(*args):
        return subprocess.run(["git", "-C", str(config.root), *args],
                              capture_output=True, text=True)

    if base_ref is None:
        base_ref = "HEAD~1"
        mb = _git("merge-base", "origin/main", "HEAD")
        if mb.returncode == 0 and mb.stdout.strip():
            base_ref = mb.stdout.strip()

    names = set()
    for args in (("diff", "--name-only", "--diff-filter=d", f"{base_ref}...HEAD"),
                 ("diff", "--name-only", "--diff-filter=d", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard")):
        res = _git(*args)
        if res.returncode == 0:
            names.update(n for n in res.stdout.splitlines() if n.strip())

    out = []
    for n in names:
        p = config.root / n
        if p.is_file() and _is_content_path(config, p):
            out.append(p)
    return sorted(out)


class Reporter:
    def __init__(self):
        self.errors = 0

    def error(self, path, msg):
        print(f"ERROR   {path}: {msg}")
        self.errors += 1

    def warn(self, path, msg):
        print(f"warning {path}: {msg}")

    def finish(self, ok_msg):
        if self.errors:
            print(f"\nFAILED with {self.errors} error(s).")
            sys.exit(1)
        print(ok_msg)
