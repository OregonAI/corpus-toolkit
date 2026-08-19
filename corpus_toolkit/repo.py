"""Generic corpus-repo primitives: frontmatter parsing, content-file
discovery, snapshot hashing, git-diff scoping. Ported from
oregon-policy-repo/src/repo_lib.py with all Oregon-specific constants
(CONTENT_DIRS, DIR_DOC_TYPE, snapshot slicing) replaced by config-driven
equivalents — see corpus_toolkit/config.py and docs/reference-architecture.md."""
from __future__ import annotations

import datetime
import hashlib
import multiprocessing as mp
import os
import re
import subprocess
import sys
from collections.abc import Sequence
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
#
# THIS LIST STAYS EMPTY, deliberately (corpus-toolkit#66). A generic toolkit cannot
# enumerate the volatile tokens of every site it will ever crawl, and shipping even the
# site-agnostic candidates (Cloudflare's `data-cfemail`, `/cdn-cgi/l/email-protection`)
# would silently rehash every existing source carrying that markup across every corpus —
# a wave of phantom drift arriving in a version bump, which is the one way this platform
# has already broken corpora. A corpus declares the tokens ITS sources embed, under
# `volatile_patterns:` in `_meta/corpus.yml`, and re-seeds its baselines in the same PR.
VOLATILE_PATTERNS: list[bytes] = []


def normalize_volatile(data: bytes,
                       patterns: Sequence[re.Pattern[bytes]] = ()) -> bytes:
    """Strip volatile byte patterns before hashing.

    `patterns` is the CORPUS's list — `config.volatile_patterns`, compiled once at load —
    and is PASSED IN rather than read from a module global. A global every caller has to
    remember to populate first reproduces exactly the silent no-op #66 is about: the hash
    would depend on whether some earlier code path happened to install the patterns, and
    forgetting it looks identical to a corpus that declared none.
    """
    for pat in list(VOLATILE_PATTERNS) + list(patterns):
        data = re.sub(pat, b"", data)
    return data


def content_hash(raw: bytes, fmt: str,
                 volatile_patterns: Sequence[re.Pattern[bytes]] = ()) -> str:
    """Content hash of a freshly-fetched source: sha256 of the whitespace-normalized
    extracted text (pdftotext for PDFs, tag-stripping for HTML/XML). Falls back to
    the raw-byte hash when extraction yields <200 chars (e.g. image-only scans).

    `volatile_patterns` applies to the HTML/XML path only, which is where per-fetch and
    per-release tokens live; PDFs go through text extraction and are largely immune, and
    the binary formats have no text layer to normalize. A caller that passes none gets
    byte-identical hashes to every release before v1.26.0 — the property that keeps this
    change from re-hashing the platform (corpus-toolkit#66).

    NOT the same hash as `hash_snapshot()`, which reads committed `.txt` and is deliberately
    never re-derived from the source at verification time. Frontmatter `source_sha256` is
    therefore NOT a valid seed for a manifest baseline: the two agree only for image-only
    scans, where both fall back to raw bytes (measured on oregon-kpm, corpus-toolkit#68).
    """
    if fmt == "pdf":
        proc = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw,
                              capture_output=True, check=False)
        text = proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""
    elif fmt in ("html", "xml"):
        from corpus_toolkit.html_to_text import html_to_text
        text = html_to_text(normalize_volatile(raw, volatile_patterns))
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


class UnresolvableRef(RuntimeError):
    """A caller-supplied git ref that git cannot resolve. Never downgraded to 'no
    changes' — see changed_content_files()."""


def changed_content_files(config: CorpusConfig, base_ref: str | None = None):
    """Content files added/modified relative to base_ref (default: merge-base with
    origin/main, else HEAD~1). Includes uncommitted working-tree changes. Returns a
    sorted list of existing paths — deletions are dropped (nothing to verify)."""
    def _git(*args):
        return subprocess.run(["git", "-C", str(config.root), *args],
                              capture_output=True, text=True)

    explicit_base = base_ref is not None
    # Git's all-zero SHA is not a broken ref — it is its documented sentinel for "this
    # push CREATED the ref". GitHub sends it as `github.event.before` on the first push
    # to a new branch, which both shipped workflows pass straight to --changed. Treating
    # it as unresolvable would fail CI on every new branch, so it means "no base commit
    # exists", and the default base (merge-base with origin/main) is the right answer:
    # validate what the branch adds. Distinct from a ref that was NAMED and is missing,
    # which stays a hard error below.
    if explicit_base and base_ref.strip() and set(base_ref.strip()) == {"0"}:
        base_ref, explicit_base = None, False

    if base_ref is None:
        base_ref = "HEAD~1"
        mb = _git("merge-base", "origin/main", "HEAD")
        if mb.returncode == 0 and mb.stdout.strip():
            base_ref = mb.stdout.strip()

    # A base ref the CALLER named explicitly and git cannot resolve is a hard error.
    # Swallowing it made "the ref does not exist" indistinguishable from "nothing
    # changed": --changed origin/main on a fork PR, a shallow checkout, or a renamed
    # default branch returned [], both validators printed "No changed content files"
    # and CI went GREEN having checked nothing. That is a guardrail switching itself off.
    if explicit_base:
        probe = _git("rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
        if probe.returncode != 0:
            raise UnresolvableRef(
                f"base ref {base_ref!r} could not be resolved by git. Refusing to "
                f"validate zero files and report success — that is how a guardrail "
                f"silently switches itself off. (Shallow clone, fork PR, or a remote "
                f"that is not 'origin' are the usual causes.)")

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


# Below this many files, forking costs more than it saves. Above it, the per-file work
# (schema validation, snapshot hashing) dominates process setup.
#
# A DUPLICATED CONSTANT UNTIL NOW: `validate/frontmatter.py` and `validate/provenance.py`
# each carried their own `len(paths) < 50` and their own `chunksize=64`, so tuning either
# meant finding both (corpus-toolkit#76).
PARALLEL_THRESHOLD = 50
CHUNKSIZE = 64


def map_documents(paths, fn, *, jobs: int | None = None, setup=None, setup_args=()):
    """Run `fn` over every path, forking when that is worth it.

    `setup` is the per-process initializer — the toolkit's validators hand their worker
    state (a compiled JSON schema, a config, a snapshot-slicing plugin) to module globals
    that `fn` then reads, because those objects are expensive to build and awkward to
    pickle per call. It runs ONCE in-process on the sequential path and once per worker on
    the parallel one.

    That handoff is the part of these tools hardest to get right and easiest to copy
    wrong, and it was written out twice before this existed.

    RESULT ORDER IS NOT GUARANTEED. The parallel path is `imap_unordered`, so results
    arrive as workers finish; the sequential path is naturally in order. Both callers
    aggregate rather than index, so the difference has never mattered — but making it
    uniform either way would be a behaviour change rather than a refactor, so the existing
    asymmetry is preserved and named instead.

    `fn` and `setup` must be importable at module scope: `fork` is used, but the pool still
    pickles the callables.
    """
    paths = list(paths)
    # `is not None`, NOT a falsy check: both validators previously did `max(1, args.jobs)`,
    # so `-j 0` meant "do not fork". A falsy check reads 0 as "unset" and forks across every
    # CPU instead — the exact opposite, and silently, in the constrained environments where
    # someone would have disabled it on purpose.
    jobs = max(1, jobs if jobs is not None else (os.cpu_count() or 1))
    if jobs == 1 or len(paths) < PARALLEL_THRESHOLD:
        if setup is not None:
            setup(*setup_args)
        return [fn(p) for p in paths]
    # `fork` specifically, not the default start method: the validators rely on inheriting
    # an already-loaded config rather than re-reading it per worker.
    ctx = mp.get_context("fork")
    with ctx.Pool(jobs, initializer=setup, initargs=setup_args) as pool:
        return list(pool.imap_unordered(fn, paths, chunksize=CHUNKSIZE))


def check_generated(path: Path, rendered: str, *, normalize=None,
                    hint: str = "") -> tuple[bool, str]:
    """Is the committed artifact at `path` still what regenerating would produce?

    Returns `(current, message)` and NEVER RAISES — a checker that throws is a checker
    whose caller learns nothing, the same rule `FileBackend.index_status` follows. That
    means catching BOTH families `read_text` produces: `OSError` for the file, and
    `UnicodeDecodeError` for its bytes. The second is a `ValueError`, not an `OSError`, so
    an `except OSError` alone lets a single stray latin-1 byte in a committed artifact
    escape as a traceback — which is how the first version of this got it wrong.

    `normalize` decides what counts as a difference: `index.py` drops the `generated` key
    because it is metadata rather than derived from the documents, `status.py` strips
    date-shaped lines because a regeneration date is not staleness. Without it every gate
    would fail daily and be switched off within a week.

    FOUR STATES, KEPT APART. Missing, unparseable, stale, and current are four different
    answers, and telling someone to regenerate a corrupt file is right while telling them
    it is merely stale is not. status.py's predecessor collapsed all of them into "does the
    file contain this heading?" — a gate that could not fail, which reads as coverage.
    """
    normalize = normalize or (lambda s: s)
    name = path.name
    if not path.is_file():
        return False, f"{name} is missing{hint}"
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return False, f"{name} could not be read ({type(e).__name__}: {e}){hint}"
    try:
        a, b = normalize(current), normalize(rendered)
    except Exception as e:                       # noqa: BLE001 — any parse failure, one answer
        return False, (f"{name} is not in the expected format "
                       f"({type(e).__name__}: {e}){hint}")
    if a != b:
        return False, f"{name} is STALE{hint}"
    return True, f"{name} is current"


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
