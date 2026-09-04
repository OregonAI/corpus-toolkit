"""The one writer of a source's `sha256` baseline in the source manifest.

THE MANIFEST IS CURATED DATA a human reviews in a PR. Two writers existed for this one field
-- `corpus-detect-changes` (line-level edit, quoted value) and each corpus's own ingester
(`yaml.safe_dump`, unquoted value, whole-file rewrite) -- and oregon-collective-bargaining
had to carry a shim purely because they disagreed about quoting (its `_manifest_baseline.py`:
"ONE FILE, TWO WRITERS, AND THEY DISAGREE"). This module is that one writer. The drift
detector calls it after a run; an ingester calls it through
`corpus_toolkit.sources.snapshots.record_snapshot` when a source is (re)ingested, because a
baseline is what the mirror HOLDS and ingest is what moves it (ADR-0016).

The edit is line-level, on BYTES, then verified twice -- by re-parsing, so a value cannot
land in the wrong entry, and line by line, so nothing else in the file (a comment, a quoting
style, a CRLF ending) can move. A file that fails either check is left untouched and the
caller is told which source and why. Regexes over YAML are fragile, so nothing they produce
is written until it has passed both checks.
"""
from __future__ import annotations

import copy
import difflib
import re
from pathlib import Path

import yaml

from corpus_toolkit import config as config_mod

_ID_RE = re.compile(r"^(?P<lead>[ \t]*(?:-[ \t]+)?)id:[ \t]*"
                    r"(?P<value>'[^']*'|\"[^\"]*\"|[^#\n]*?)[ \t]*(?:#.*)?$")
_SHA_RE = re.compile(r"^(?P<lead>[ \t]*(?:-[ \t]+)?)sha256:(?P<sp>[ \t]*)"
                     r"(?P<value>'[^']*'|\"[^\"]*\"|[^#\n]*?)"
                     r"(?P<gap>[ \t]*)(?P<comment>#[^\n]*)?(?P<eol>\r?\n?)$")


class BaselineRefused(RuntimeError):
    """The baseline could not be written without risking the wrong entry or the wrong file.
    Nothing was written."""


class UndeclaredSource(BaselineRefused):
    """The id is declared in no source-manifest group, so it has no baseline to move."""


def _scalar(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def plan_sha_edits(lines: list[str], wanted: set[str]) -> dict[str, list]:
    """{source id: [id line index, sha256 line index or None, key column]}.

    Only ids in `wanted`, and only their FIRST occurrence — an id that appears twice is
    dropped by the caller before we get here, because guessing which entry a hash belongs
    to is how a manifest acquires a baseline that is wrong for a source nobody re-examines.
    """
    plan: dict[str, list] = {}
    cur = None
    # `sha256:` lines seen since the current entry began, as (index, lead length). An entry
    # may write `sha256:` ABOVE `id:`, and scanning forward from the id line alone never
    # claimed those -- `rewrite_sha256` then concluded the entry had none and INSERTED a
    # second one, which both verification guards accept: PyYAML resolves duplicate keys
    # last-wins so the re-parse reads back the inserted value, and the line diff sees one
    # added line carrying a wanted value. The run reported success and left a stale key in a
    # curated file (corpus-toolkit#119).
    pending: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            cur = None                      # a top-level key ends the entry
            pending = []
        elif stripped.startswith("- "):
            # A `- ` INSIDE the entry is not a new entry. This reset was unconditional,
            # which was safe only while no source key held a block sequence -- `watch:`
            # (corpus-toolkit#72) is the first, and with it written above `sha256:` the sha
            # line was never associated with its id: a second `sha256:` was inserted after
            # `id:`, the re-parse check saw the old trailing value win, and the whole group
            # file was refused, taking every other source in it down too. That is the
            # adoption path MIGRATION.md prescribes, broken by the feature it adopts.
            #
            # Compared against the entry's own key column, so a sibling `- id:` (marker to
            # the LEFT of that column) still ends it -- writing a's hash into b's line is
            # the wrong-entry write this function refuses by name.
            marker = len(line) - len(line.lstrip())
            if cur is None or marker < plan[cur][2]:
                cur = None
                # A NEW ENTRY STARTS HERE, so anything seen above belongs to the previous
                # one. Keeping it would let a `sha256:` from the entry before be claimed by
                # this one -- the wrong-entry write, from the other direction.
                pending = []
        m = _ID_RE.match(line)
        if m:
            sid = _scalar(m.group("value"))
            cur = sid if sid in wanted and sid not in plan else None
            if cur is not None:
                col = len(m.group("lead"))
                plan[cur] = [i, None, col]
                # AT THE ENTRY'S OWN KEY COLUMN, exactly as the forward scan requires.
                # Without the column test this claims the first `sha256:` above `id:` at any
                # depth, so an entry whose `attachments:` list carries per-file digests above
                # its own id gets the source's hash written into the attachment.
                for idx, lead in pending:
                    if lead == col:
                        plan[cur][1] = idx
                        break
            continue
        m = _SHA_RE.match(line)
        # AT THE ENTRY'S OWN KEY COLUMN, exactly. Relaxing the `- ` reset above so a nested
        # block sequence no longer ends the entry also let the first `sha256:` INSIDE that
        # sequence be claimed as the entry's own -- an `attachments:` list with per-file
        # digests had the source's hash written into the attachment, the re-parse check
        # failed, and the group file was refused with nothing written. The entry's own keys
        # are at `plan[cur][2]`; anything deeper belongs to something else.
        if m:
            if (cur is not None and plan[cur][1] is None
                    and len(m.group("lead")) == plan[cur][2]):
                plan[cur][1] = i
            elif cur is None:
                # Above an `id:` we have not reached yet. Recorded with its column so the
                # id line can claim it only if it is that entry's own key.
                pending.append((i, len(m.group("lead"))))
    return plan


def rewrite_sha256(text: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    """Set each id's `sha256` in place. Returns (new text, ids that could not be located)."""
    lines = text.splitlines(keepends=True)
    plan = plan_sha_edits(lines, set(updates))
    # An inserted key takes the file's OWN line ending, not this platform's: a CRLF
    # manifest that grows one LF line is a mixed-ending file, which every later diff shows.
    nl = "\r\n" if "\r\n" in text else "\n"
    inserts: dict[int, list[str]] = {}
    for sid, (id_i, sha_i, col) in plan.items():
        value = updates[sid]
        if sha_i is not None:
            m = _SHA_RE.match(lines[sha_i])
            lines[sha_i] = (f"{m.group('lead')}sha256:{m.group('sp') or ' '}\"{value}\""
                            f"{m.group('gap')}{m.group('comment') or ''}"
                            f"{m.group('eol') or nl}")
        else:
            # No `sha256:` key at all: insert one immediately after `id:`, at the same
            # indentation. Appending at the end of the entry instead would land inside
            # whatever nested block happened to come last.
            if not lines[id_i].endswith("\n"):
                lines[id_i] += nl
            inserts.setdefault(id_i, []).append(f"{' ' * col}sha256: \"{value}\"{nl}")
    if inserts:
        rebuilt: list[str] = []
        for i, line in enumerate(lines):
            rebuilt.append(line)
            rebuilt.extend(inserts.get(i, ()))
        lines = rebuilt
    return "".join(lines), sorted(set(updates) - set(plan))


def rewrite_problem(before: str, after: str, updates: dict[str, str],
                    unlocated: list[str]) -> str | None:
    """Why this rewrite must not be written, or None if it is sound.

    TWO CHECKS, because they catch different mistakes. The YAML comparison catches a value
    written into the wrong place — a nested block, a neighbouring entry — by insisting the
    reparsed document equals the original with exactly these `sha256` values changed. The
    LINE comparison catches everything a parse cannot see: a reflowed scalar, a lost
    comment, a translated line ending. Only `sha256:` lines carrying one of the new values
    may differ, byte for byte, and every other line must survive untouched.
    """
    if unlocated:
        return (f"could not locate {', '.join(unlocated)} in the file, so the value has "
                f"nowhere to go.")
    expected = copy.deepcopy(yaml.safe_load(before) or {})
    for s in expected.get("sources") or []:
        if isinstance(s, dict) and str(s.get("id", "")) in updates:
            s["sha256"] = updates[str(s["id"])]
    try:
        actual = yaml.safe_load(after)
    except yaml.YAMLError as e:
        return f"the rewritten file did not parse as YAML ({e})."
    if actual != expected:
        return ("the rewritten file did not re-parse to the original with only these "
                "sha256 values changed.")
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    wanted = set(updates.values())
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for line in old_lines[i1:i2]:
            if not _SHA_RE.match(line):
                return (f"a line that is not a `sha256:` line would have changed: "
                        f"{line.strip()[:60]!r}.")
        for line in new_lines[j1:j2]:
            m = _SHA_RE.match(line)
            if not m or _scalar(m.group("value")) not in wanted:
                return (f"a line that is not one of the new `sha256:` values would have "
                        f"been written: {line.strip()[:60]!r}.")
    return None


def record_baseline(config, source_id: str, sha256: str) -> Path | None:
    """Set ONE source's baseline in the manifest group file that declares it.

    Returns the file written, or None when the recorded value already equals `sha256`
    (nothing to do, nothing touched). Raises `UndeclaredSource` when no group declares the
    id -- a snapshot that is not a manifest source has no baseline to move -- and
    `BaselineRefused` when the id is declared more than once (which entry?) or the rewrite
    fails verification. In every refusal NOTHING has been written.

    `sha256` must be the detector's hash -- `corpus_toolkit.repo.content_hash(raw, fmt,
    config.volatile_patterns)` -- not the document's `source_sha256`; the two agree only for
    image-only PDFs. `snapshots.record_snapshot` computes the right one.
    """
    files = config_mod.load_source_manifest_group_files(config)
    hits: list[tuple[Path, dict]] = []
    for path, group in files:
        for s in (group.get("sources") or []):
            if isinstance(s, dict) and str(s.get("id", "")) == source_id:
                hits.append((path, s))
    if not hits:
        raise UndeclaredSource(
            f"{source_id!r} is declared in no source-manifest group under "
            f"{config.source_manifest_path}; declare the source before recording a baseline.")
    if len(hits) > 1:
        where = sorted({str(p.name) for p, _ in hits})
        raise BaselineRefused(
            f"{source_id!r} is declared {len(hits)} times ({', '.join(where)}) -- cannot tell "
            f"which entry the hash belongs to. Give the entries distinct ids. Nothing written.")
    path, entry = hits[0]
    if str(entry.get("sha256") or "").strip() == sha256:
        return None
    # Bytes in, bytes out. `read_text`/`write_text` translate line endings, so a CRLF
    # manifest would come back LF THROUGHOUT -- a whole-file rewrite, which is the one
    # thing the line-level editor exists to avoid.
    text = path.read_bytes().decode("utf-8")
    new_text, unlocated = rewrite_sha256(text, {source_id: sha256})
    problem = rewrite_problem(text, new_text, {source_id: sha256}, unlocated)
    if problem:
        raise BaselineRefused(f"{path}: {problem} Nothing was written to this file.")
    path.write_bytes(new_text.encode("utf-8"))
    return path
