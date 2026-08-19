"""Generic corpus-repo primitives: frontmatter parsing, content-file
discovery, snapshot hashing, git-diff scoping. Ported from
oregon-policy-repo/src/repo_lib.py with all Oregon-specific constants
(CONTENT_DIRS, DIR_DOC_TYPE, snapshot slicing) replaced by config-driven
equivalents — see corpus_toolkit/config.py and docs/reference-architecture.md."""
from __future__ import annotations

import datetime
import hashlib
import json
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


class WatchedPathMissing(ValueError):
    """A source declared a `watch` path the fetched document does not contain.

    Its own category because the alternative is silence: two documents that both lack a
    watched path hash equal and read as "unchanged", so the corpus would report stability
    exactly when upstream removed the field it was watching. "Could not check" is never
    "is not there" (CONTEXT.md).
    """


class WatchedDocumentUnreadable(WatchedPathMissing):
    """The body a `watch` source returned could not be read as json at all.

    A SUBCLASS, so nothing that already handles the parent changes behaviour, but its own
    class because it is a DIFFERENT FINDING with a different remedy: a 200 carrying an error
    page, or a block, sends the operator upstream; a missing path sends them to the `watch`
    list. Reporting one as the other is what convention 5 forbids, and every aggregate in
    the drift run did exactly that until this existed to distinguish them.
    """


def validate_watch(watch, where: str = ""):
    """Check a `watch` list and return it with each path trimmed. Raises ValueError.

    ONE GRAMMAR, ONE PARSER, in the module that owns the grammar. `config._validated_watch`
    checks a manifest at load and this runs again at the hash, and while they were two
    separate implementations they disagreed: the door rejected `a[].b[]` and the choke point
    did not, so a direct caller got a bare ValueError out of `_select_watched`, which the
    drift run's `except Exception` files under "a fact about our access, not about
    upstream". Every check on the VALUE lives here; `config._validated_watch` adds the source
    id and the one rule a hash cannot express -- at a manifest, `watch:` with no value under
    it is an authoring accident, where a caller passing None means "hash the raw bytes".

    `where` prefixes the message when the caller knows which source this came from.
    """
    at = f"{where}: " if where else ""
    if isinstance(watch, str):
        raise ValueError(
            f"{at}`watch` is a string, not a list. Write [{watch!r}] — a bare string is "
            f"iterated CHARACTER BY CHARACTER, so each character would become its own "
            f"watched path and be reported missing from the document.")
    if not isinstance(watch, (list, tuple)):
        # Its own message: `watch: 5` and `watch: {a: 1}` were both told about character-by-
        # character iteration, a rationale about a type they are not (convention 5).
        raise ValueError(
            f"{at}`watch` must be a list of paths, got {type(watch).__name__} ({watch!r}).")
    if not watch:
        # An empty list digests to sha256("{}") -- a CONSTANT for every document, so the
        # source reports `unchanged` forever without looking at anything. Reachable from
        # `watch=[p for p in paths if p]` or `watch=cfg.get("watch", [])`.
        raise ValueError(
            f"{at}`watch` is an empty list. Every document would digest to the same value, "
            f"so the source would report `unchanged` forever without looking at anything. "
            f"Pass watch=None to hash the raw bytes instead.")
    out = []
    for i, path in enumerate(watch):
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"{at}watch[{i}] is {path!r}; each watched path must be a "
                             f"non-empty string.")
        # Trimmed, and the trimmed value is what gets USED. `- " rowsUpdatedAt "` survived
        # because strip() was tested for emptiness and then thrown away, so the lookup ran
        # on the padded string and reported the key missing from the document.
        # Rebuilt SEGMENT BY SEGMENT, splitting each into its key and an optional `[]`
        # suffix and trimming the key. Trimming segment EDGES was not enough: whitespace
        # before `[]` stayed interior, so `columns [].name` kept a key of `'columns '` and
        # was reported from the crawl as a path upstream does not have -- while
        # `columns[] .name`, the same typo one space to the right, was normalised and
        # worked. Two spellings of one mistake with opposite outcomes is the asymmetry this
        # is for, and edge-trimming moved it rather than ending it.
        segments = []
        for seg in path.strip().split("."):
            seg = seg.strip()
            project = seg.endswith("[]")
            key = (seg[:-2] if project else seg).strip()
            if not key:
                # `[]`, `a.[]`, `[].name`, `a. .b`: a segment with no key. Passed the door
                # empty and was then reported as a path upstream does not have -- the
                # miscategorisation this validator exists to prevent, and the one hole left
                # in a check that already rejects `.name` and `columns[].`.
                raise ValueError(
                    f"{at}watch[{i}] {path!r}: the segment {seg!r} has no key — `[]` "
                    f"projects over a named array, as in `columns[].name`. A watch list "
                    f"cannot address a document that is itself an array.")
            if "[" in key or "]" in key:
                # `columns[ ].name` was taken as a literal key named `columns[ ]` and then
                # reported missing; `columns[]name` is a dot short of a projection. A
                # bracket in a real json key is vanishingly rare, a mistyped projection is
                # not, so refuse rather than look one up.
                raise ValueError(
                    f"{at}watch[{i}] {path!r}: the segment {seg!r} is not a key or a "
                    f"projection. Write `[]` exactly, immediately after the array's key, as "
                    f"in `columns[].name`. Left as written this is looked up as a literal "
                    f"key and reported as a path the document does not contain, which reads "
                    f"as upstream changing shape rather than as the typo it is.")
            segments.append(key + ("[]" if project else ""))
        path = ".".join(segments)
        if path.count("[]") > 1:
            raise ValueError(
                f"{at}watch[{i}] {path!r} projects with `[]` more than once. Nested "
                f"projections flatten, so documents differing only in how values are "
                f"distributed across the outer array would digest identically and their "
                f"drift could never be reported. Declare one path per projected level.")
        out.append(path)
    return out


def _select_watched(doc, path: str):
    """Values at `path`, as a list. Raises WatchedPathMissing if the path reaches nothing.

    A DELIBERATELY SMALL GRAMMAR: dot-separated keys, with `[]` projecting over an array --
    `rowsUpdatedAt`, `columns[].name`, `columns[].cachedContents`. That covers what the
    manifests actually watch. A full JSONPath dependency buys expressiveness nobody has
    asked for and a parser nobody in this repo can review.

    AT MOST ONE `[]` PER PATH. A second projection flattens, so `{"a":[{"b":[1,2]},{"b":[3]}]}`
    and `{"a":[{"b":[1]},{"b":[2,3]}]}` digest EQUAL and drift between them can never be
    reported. Refusing is the narrow fix; a nesting-preserving encoder is a general one
    nobody has asked for.
    """
    if path.count("[]") > 1:
        raise ValueError(
            f"watched path {path!r} projects with `[]` more than once. Nested projections "
            f"flatten, so documents that differ only in how values are distributed across "
            f"the outer array would digest identically and their drift could never be "
            f"reported. Declare one path per projected level instead.")
    current = [doc]
    for segment in path.split("."):
        project = segment.endswith("[]")
        key = segment[:-2] if project else segment
        nxt = []
        for node in current:
            if not isinstance(node, dict) or key not in node:
                raise WatchedPathMissing(
                    f"watched path {path!r} is not present in this document (missing at "
                    f"{key!r}). Either upstream changed shape -- which is worth knowing, "
                    f"and is why this is an error rather than an empty value -- or the "
                    f"path is wrong. A missing path hashed as empty would make two "
                    f"documents that both lack it read as unchanged.")
            value = node[key]
            if project:
                if not isinstance(value, list):
                    raise WatchedPathMissing(
                        f"watched path {path!r} projects over {key!r} with `[]`, but that "
                        f"key holds {type(value).__name__}, not a list")
                nxt.extend(value)
            else:
                nxt.append(value)
        if project and not nxt:
            # AN EMPTY ARRAY IS NOT A CHECKED PATH. Falling through with `[]` meant no leaf
            # key was ever examined, so a typo'd leaf validated against any document whose
            # array happened to be empty -- and worse, if upstream starts returning
            # `"columns": []` the digest flips ONCE and is then stable forever while the
            # watched schema is gone. Silent stability is the failure this class exists for.
            raise WatchedPathMissing(
                f"watched path {path!r} projects over {key!r}, but that array is empty in "
                f"this document, so nothing under it was checked. Two documents with an "
                f"empty {key!r} would digest equal no matter what the watched leaves say.")
        current = nxt
    return current


def _watched_digest(raw: bytes, watch) -> str:
    """sha256 over ONLY the declared paths, canonicalised.

    Canonical because upstream re-serialising its JSON -- different key order, different
    indentation -- is not a content change, and without this the mechanism would trade one
    false positive for another.
    """
    # THE CHOKE POINT, not only the door -- and the SAME checks as the door, because two
    # implementations of one grammar disagreed and the gap read as an upstream change.
    # `config._validated_watch` runs before the crawl, but that is one caller; a watch list
    # also reaches here from a corpus's own scripts and from anything that assembles a
    # source dict another way. corpus-toolkit#111 is the same lesson.
    watch = validate_watch(watch)
    try:
        # STRICT. `errors="replace"` mapped every invalid byte to U+FFFD before parsing, so
        # two distinct non-UTF-8 spellings of a watched value digested the same. JSON is
        # defined as UTF-8 (RFC 8259), so a body that is not is a real upstream condition
        # worth reporting -- not something to silently smooth over on the way to a hash.
        # `utf-8-sig`, not `utf-8`: a leading BOM is routine from IIS/.NET-backed
        # government endpoints, which is the population this toolkit crawls, and json.loads
        # on BYTES would have accepted it (CPython sniffs utf-8-sig). Rejecting it made a
        # source that hashed fine before adoption permanently uncomparable and blamed the
        # response. This keeps the strict property the comment below argues for -- real
        # mojibake still fails -- and drops only the byte order mark.
        doc = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise WatchedDocumentUnreadable(
            f"a source declaring `watch` returned bytes that are not valid utf-8 ({e}). "
            f"json is utf-8 by definition, so this is a fact about the response, not "
            f"something to normalise away.")
    except json.JSONDecodeError as e:
        # NARROW ON PURPOSE. A bare `except Exception` here reported a NameError in this
        # very function as "upstream did not return parseable json" -- a check describing a
        # condition other than the one that occurred, which is the thing this codebase files
        # bugs about. A programming error must surface as itself.
        raise WatchedDocumentUnreadable(
            f"a source declaring `watch` did not return parseable json ({e}). An error "
            f"page served with a 200 looks exactly like this; it must not read as a hash.")
    if not isinstance(doc, dict):
        # UNREADABLE, not path-missing: the remedy is the source's `url`, not its `watch`
        # list, and reporting it as a missing path sent the operator to compare a schema
        # that was never involved.
        #
        # Two different findings, so two different remedies. An array is Socrata's
        # `/resource/{id}.json` -- the sibling of the endpoint this feature is written for
        # -- and the fix is to point `url` at the metadata document. A scalar is not that:
        # a 200 carrying a bare error string has no rows to point away from, and offering
        # the array advice would describe a condition that did not occur.
        kind = {list: "array", type(None): "null"}.get(type(doc), type(doc).__name__)
        remedy = ("point `url` at the dataset's metadata document instead of its rows"
                  if isinstance(doc, list) else
                  "check what this url actually serves — a 200 carrying a bare value is "
                  "usually an error response, not a document")
        raise WatchedDocumentUnreadable(
            f"a source declaring `watch` returned a json {kind}, not an object. A watch "
            f"path addresses keys, so this document cannot be watched — {remedy}.")
    selected = {path: _select_watched(doc, path) for path in watch}
    # No `default=`: everything here came out of `json.loads`, so a fallback encoder could
    # only mask a defect -- and it masked it by COLLIDING, mapping distinct values onto one
    # string. Anything unserializable must raise as itself.
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(raw: bytes, fmt: str,
                 volatile_patterns: Sequence[re.Pattern[bytes]] = (),
                 watch: Sequence[str] | None = None) -> str:
    """Content hash of a freshly-fetched source: sha256 of the whitespace-normalized
    extracted text (pdftotext for PDFs, tag-stripping for HTML/XML). Falls back to
    the raw-byte hash when extraction yields <200 chars (e.g. image-only scans).

    A json source with no `watch` still gets the raw-byte hash it always got, INCLUDING in a
    corpus that declares `volatile_patterns` -- those are declared corpus-wide and passed for
    every source, so refusing the combination here would break sources that opted into
    nothing on account of a pattern written for an unrelated html group. A pattern that
    matches nothing anywhere is already named in the drift report, per run, which is the
    level that can actually tell the difference.

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
    if watch is not None:
        # `is not None`, NOT truthiness. `watch=[]`, `()`, `""` and `0` all fell through to
        # the whole-document hash, so a caller writing `watch=cfg.get("watch", [])` got
        # unwatched hashing while believing the source was watch-scoped -- the silent
        # opposite of what it asked for. Every invalid value now reaches the guard below.
        #
        # A JSON source hashes ONLY what it declared it watches (corpus-toolkit#72), so a
        # vendor counter that moves on its own is inert by construction rather than needing
        # to be enumerated as it appears. Deliberately BEFORE the format branch: the digest
        # is over selected values, so the under-200-character raw-byte fallback below must
        # not apply -- inheriting it would make this work for large metadata documents and
        # quietly not work for small ones.
        return _watched_digest(raw, watch)
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
