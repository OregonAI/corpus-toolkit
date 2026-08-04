#!/usr/bin/env python3
"""Move every corpus-toolkit pin in a corpus repo, and report when they disagree.

  python3 bump_pins.py --repo ../oregon-budget --to v1.23.0          # rewrite
  python3 bump_pins.py --repo ../oregon-budget --to v1.23.0 --dry-run
  python3 bump_pins.py --repo ../oregon-budget --check               # report drift only

WHY THIS EXISTS. Every toolkit tag obliges a manual edit in every corpus, and each corpus
pins the toolkit in at least TWO places per workflow call — the workflow ref and the code
ref:

    uses: OregonAI/corpus-toolkit/.github/workflows/validate-frontmatter.yml@v1.6.0
    with:
      toolkit-ref: v1.6.0

They are separate knobs on purpose (`uses:` selects the workflow FILE, `toolkit-ref:`
selects the CODE it installs) and they drift silently, because nothing compares them.
Repos with a job that installs the toolkit itself carry a third. Measured across the org
on 2026-07-28: 14 of 70 merged PRs (20%) existed only to carry a platform change into a
corpus (corpus-toolkit#9).

DRIFT IS THE REAL COST, not the typing. Measured 2026-08-04, oregon-legislature's ci.yml
runs `verify-provenance` at v1.21.0 while `validate-frontmatter` and `check-links` in the
SAME FILE run at v1.19.0 — a partial bump someone made to get one fix. Nothing failed;
nothing could. `--check` is that detector, and it is the half worth running in CI.

WHAT IS DELIBERATELY NOT TOUCHED. Only `OregonAI/corpus-toolkit` refs. `actions/checkout@v4`
and friends are pinned to major tags on a different cadence by a different owner, and a
regex sloppy enough to catch them would rewrite `@v4` to `@v1.23.0` on a good day.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

# Every shape a toolkit pin takes. Each captures the version in group 'v' so one
# substitution routine handles all of them.
PATTERNS = (
    # uses: OregonAI/corpus-toolkit/.github/workflows/<name>.yml@v1.2.3
    re.compile(r"(?P<pre>OregonAI/corpus-toolkit/[^@\s]+@)(?P<v>v\d+\.\d+\.\d+)"),
    # toolkit-ref: v1.2.3
    re.compile(r"(?P<pre>toolkit-ref:\s*)(?P<v>v\d+\.\d+\.\d+)"),
    # a checkout of the toolkit repo: `repository: OregonAI/corpus-toolkit` then `ref: v…`
    # Matched on the ref line only when the preceding lines name the toolkit — handled in
    # `_toolkit_checkout_refs` rather than here, because `ref:` alone is far too generic.
    # requirements.txt: corpus-toolkit[...] @ git+https://github.com/OregonAI/corpus-toolkit@v1.2.3
    re.compile(r"(?P<pre>github\.com/OregonAI/corpus-toolkit@)(?P<v>v\d+\.\d+\.\d+)"),
)

SCAN_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml",
              "requirements.txt", "requirements-*.txt")


def _toolkit_checkout_refs(text: str):
    """Spans of `ref: vX.Y.Z` lines that belong to a corpus-toolkit checkout.

    `ref:` on its own says nothing — actions/checkout uses it for the corpus's own code
    too. Only a ref within a few lines after `repository: OregonAI/corpus-toolkit` is ours.
    """
    lines = text.splitlines(keepends=True)
    out, offset, armed = [], 0, 0
    for line in lines:
        if "repository:" in line and "OregonAI/corpus-toolkit" in line:
            armed = 4
        elif armed:
            m = re.search(r"(?P<pre>ref:\s*)(?P<v>v\d+\.\d+\.\d+)", line)
            if m:
                out.append((offset + m.start("v"), offset + m.end("v")))
                armed = 0
            else:
                armed -= 1
        offset += len(line)
    return out


def find_pins(text: str) -> list[tuple[int, int, str]]:
    """(start, end, version) for every toolkit pin, sorted, non-overlapping."""
    spans: list[tuple[int, int, str]] = []
    for pat in PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start("v"), m.end("v"), m.group("v")))
    for start, end in _toolkit_checkout_refs(text):
        spans.append((start, end, text[start:end]))
    spans.sort()
    deduped: list[tuple[int, int, str]] = []
    for s in spans:
        if not deduped or s[0] >= deduped[-1][1]:
            deduped.append(s)
    return deduped


def rewrite(text: str, to: str) -> tuple[str, int]:
    pins = find_pins(text)
    if not pins:
        return text, 0
    out, last, n = [], 0, 0
    for start, end, old in pins:
        out.append(text[last:start])
        out.append(to)
        n += old != to
        last = end
    out.append(text[last:])
    return "".join(out), n


def scan(repo: pathlib.Path):
    """{path: [versions]} for every file carrying a toolkit pin."""
    found: dict[pathlib.Path, list[str]] = {}
    for pattern in SCAN_GLOBS:
        for path in sorted(repo.glob(pattern)):
            if not path.is_file():
                continue
            pins = find_pins(path.read_text(encoding="utf-8"))
            if pins:
                found[path] = [v for _, _, v in pins]
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, type=pathlib.Path)
    ap.add_argument("--to", help="target tag, e.g. v1.23.0")
    ap.add_argument("--check", action="store_true",
                    help="report pins and exit 1 if they disagree; writes nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.check and not args.to:
        ap.error("--to is required unless --check")
    if args.to and not re.fullmatch(r"v\d+\.\d+\.\d+", args.to):
        ap.error(f"--to must look like v1.2.3, got {args.to!r}")

    repo = args.repo.resolve()
    found = scan(repo)
    if not found:
        print(f"{repo.name}: no corpus-toolkit pins found")
        return 0

    versions = collections.Counter(v for vs in found.values() for v in vs)
    total = sum(versions.values())
    for path, vs in found.items():
        print(f"  {path.relative_to(repo)}: {', '.join(sorted(set(vs)))}"
              f" ({len(vs)} pin{'s' if len(vs) != 1 else ''})")
    print(f"{repo.name}: {total} pin(s) across {len(found)} file(s); "
          f"versions {', '.join(f'{v}×{n}' for v, n in versions.most_common())}")

    if args.check:
        if len(versions) > 1:
            print(f"\nDRIFT: {len(versions)} different toolkit versions pinned in one "
                  f"repo. `uses:` selects the workflow file and `toolkit-ref:` selects "
                  f"the code it installs — a partial bump leaves jobs running against "
                  f"different toolkit versions, and nothing fails.", file=sys.stderr)
            return 1
        return 0

    changed = 0
    for path in found:
        text = path.read_text(encoding="utf-8")
        new, n = rewrite(text, args.to)
        if n and not args.dry_run:
            path.write_text(new, encoding="utf-8")
        changed += n
    verb = "would move" if args.dry_run else "moved"
    print(f"\n{verb} {changed} pin(s) to {args.to}"
          f"{' (dry run, nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
