"""Loading a sibling corpus's compact index (`_meta/corpus-index.json`, see
corpus_toolkit/index.py) for cross-corpus citation resolution.

Hard rule: an unreachable sibling degrades resolution, it never breaks the
server. `load_sibling_index` swallows every failure and returns None. On a
failed fetch it prefers a STALE cache over nothing — a day-old title is far
more useful to a caller than silence — and marks the payload so the caller can
say so.

stdlib urllib only (the toolkit has no HTTP dependency, and shouldn't gain one
for a once-a-day GET of a small JSON file)."""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TTL_SECONDS = 86_400          # a sibling corpus changes on a commit cadence, not a live one
FETCH_TIMEOUT_SECONDS = 10
USER_AGENT = ("corpus-toolkit/1.1 (+https://github.com/OregonAI/corpus-toolkit) "
              "sibling-index-fetch")

# Keys the loader adds to the returned payload. Underscore-prefixed so they can
# never collide with the index's own fields.
SOURCE_KEY = "_source"       # "local" | "fetch" | "cache" | "cache-stale"
STALE_KEY = "_stale"         # present and True only for "cache-stale"


def _safe_name(sibling_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sibling_id or "sibling")


def _valid(payload) -> bool:
    """Minimal shape check. A malformed index is UNAVAILABLE, not authoritative —
    trusting it would let a truncated download turn 'exists' into 'absent'."""
    return isinstance(payload, dict) and isinstance(payload.get("documents"), dict)


def _read_json(path: Path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if _valid(payload) else None


def _mark(payload: dict, source: str) -> dict:
    out = dict(payload)
    out[SOURCE_KEY] = source
    if source == "cache-stale":
        out[STALE_KEY] = True
    return out


def _fetch(url: str) -> dict | None:
    # Ask for gzip explicitly and decompress by hand: urllib does NEITHER by default.
    # A corpus index is mostly repeated JSON keys and paths, so it compresses about
    # 5x — the executive-regulatory-frameworks index is 7.22 MiB raw and 1.36 MiB
    # gzipped, and GitHub serves it compressed. Without this header every cache miss
    # pulls the full uncompressed file.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        if getattr(resp, "status", 200) != 200:
            return None
        raw = resp.read()
        # Content-Encoding is advisory; a server may ignore the request header, so
        # branch on what actually came back rather than on what was asked for.
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8"))
    return payload if _valid(payload) else None


def _write_cache(cache_path: Path, payload: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_path)           # atomic: a concurrent reader never sees a partial file
    except OSError:
        pass                              # a read-only cache dir must not break resolution


def load_sibling_index(sibling, cache_dir: Path,
                       ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict | None:
    """Return the sibling's parsed index, or None if it is unavailable.

    `sibling` is a corpus_toolkit.config.Sibling (anything with `.id`,
    `.index_path`, `.index_url` works). Resolution order:

      1. `index_path` — a LOCAL file, wins over the URL when both are set.
      2. a cache entry under `cache_dir` younger than `ttl_seconds`.
      3. a fresh GET of `index_url` (cached on success).
      4. the cache at ANY age, if the fetch failed — returned with
         `_stale: True` so the caller can qualify what it reports.

    Never raises. The returned dict carries `_source` (and `_stale` when
    serving an expired cache); both are loader metadata, not index fields."""
    try:
        cache_path = Path(cache_dir) / f"{_safe_name(getattr(sibling, 'id', ''))}.json"

        local = getattr(sibling, "index_path", None)
        if local:
            payload = _read_json(Path(local))
            if payload is not None:
                return _mark(payload, "local")
            # a configured-but-unreadable local path falls through to the URL

        url = getattr(sibling, "index_url", None)
        if not url:
            return None

        if cache_path.is_file():
            age = time.time() - cache_path.stat().st_mtime
            if age < ttl_seconds:
                payload = _read_json(cache_path)
                if payload is not None:
                    return _mark(payload, "cache")

        try:
            payload = _fetch(url)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            payload = None
        if payload is not None:
            _write_cache(cache_path, payload)
            return _mark(payload, "fetch")

        stale = _read_json(cache_path) if cache_path.is_file() else None
        if stale is not None:
            return _mark(stale, "cache-stale")
        return None
    except Exception:                     # noqa: BLE001 — resolution degrades, never breaks
        return None


def lookup(index: dict, doc_id: str) -> dict | None:
    """One document out of a loaded index: {"title", "doc_type", "path"}, or
    None. Tolerates a longer row than the 3 fields written today."""
    row = (index.get("documents") or {}).get(doc_id)
    if isinstance(row, (list, tuple)) and len(row) >= 3:
        return {"title": row[0], "doc_type": row[1], "path": row[2]}
    if isinstance(row, dict):             # forward-compat: a richer row shape
        return {"title": row.get("title", ""), "doc_type": row.get("doc_type", ""),
                "path": row.get("path", "")}
    return None


def document_url(sibling, path: str) -> str | None:
    """`web_base` + repo-relative path, or None when the sibling declares no
    web_base (an id + corpus is still a useful answer; a wrong URL is not)."""
    base = (getattr(sibling, "web_base", "") or "").strip()
    if not base or not path:
        return None
    return base.rstrip("/") + "/" + str(path).lstrip("/")
