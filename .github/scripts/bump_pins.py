#!/usr/bin/env python3
"""Move the corpus-toolkit SERVING pin in a corpus repo, and report when pins disagree.

  python3 bump_pins.py --repo ../oregon-budget --to v1.33.0          # rewrite
  python3 bump_pins.py --repo ../oregon-budget --to v1.33.0 --dry-run
  python3 bump_pins.py --repo ../oregon-budget --check               # report drift only

WHAT A PIN IS NOW (ADR-0014). A corpus names the toolkit in two tracks:

  CI track      uses: OregonAI/corpus-toolkit/.github/workflows/<name>.yml@v1
                Floats on the major tag the release gate advances after its canary has
                validated every live corpus on the candidate. NEVER edited by this script.
                A corpus that pins an exact tag here has deliberately HELD itself back; the
                gate reports that and this script leaves it alone.
  serving track corpus-toolkit[...] @ git+https://github.com/OregonAI/corpus-toolkit@v1.33.0
                in requirements*.txt — what the Dockerfile installs into the served image.
                Exact, so `deployed.txt`'s commit builds the same image twice. THIS is what
                the script moves, one line per file.

WHY IT USED TO MOVE MORE. Before ADR-0014 every corpus carried the tag in three places per
workflow call — `uses:@tag`, `toolkit-ref:`, and a `.toolkit` checkout `ref:` — plus
requirements, 9–15 sites per repo; 154 of the 616 PRs merged across the org in seven weeks
(2026-09-02) existed to move them. `toolkit-ref` now defaults to the workflow's own commit
(`github.job_workflow_sha`) and the corpus's own jobs install from requirements.txt, so the
workflow-file shapes have no version in them to move.

WHAT IS DELIBERATELY NOT TOUCHED. Only `github.com/OregonAI/corpus-toolkit@vX.Y.Z` in
requirements files. `actions/checkout@v4` and friends were never in scope, and workflow
files are out of scope by design now.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

# The one shape a serving pin takes. Group 'v' is the version.
PATTERNS = (
    # requirements.txt: corpus-toolkit[...] @ git+https://github.com/OregonAI/corpus-toolkit@v1.2.3
    re.compile(r"(?P<pre>github\.com/OregonAI/corpus-toolkit(?:\.git)?@)(?P<v>v\d+\.\d+\.\d+)"),
)

SCAN_GLOBS = ("requirements.txt", "requirements-*.txt")


def find_pins(text: str) -> list[tuple[int, int, str]]:
    """(start, end, version) for every serving pin, sorted, non-overlapping."""
    spans: list[tuple[int, int, str]] = []
    for pat in PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start("v"), m.end("v"), m.group("v")))
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
    """{path: [versions]} for every requirements file carrying a serving pin."""
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
    ap.add_argument("--to", help="target tag, e.g. v1.33.0")
    ap.add_argument("--check", action="store_true",
                    help="report pins and exit 1 if they disagree; writes nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.check and not args.to:
        ap.error("--to is required unless --check")
    if args.to and not re.fullmatch(r"v\d+\.\d+\.\d+", args.to):
        ap.error(f"--to must look like v1.2.3, got {args.to!r} (the floating major tag is "
                 f"never a serving pin)")

    repo = args.repo.resolve()
    found = scan(repo)
    if not found:
        print(f"{repo.name}: no corpus-toolkit serving pin found in requirements*.txt")
        return 0

    versions = collections.Counter(v for vs in found.values() for v in vs)
    total = sum(versions.values())
    for path, vs in found.items():
        print(f"  {path.relative_to(repo)}: {', '.join(sorted(set(vs)))}"
              f" ({len(vs)} pin{'s' if len(vs) != 1 else ''})")
    print(f"{repo.name}: {total} serving pin(s) across {len(found)} file(s); "
          f"versions {', '.join(f'{v}×{n}' for v, n in versions.most_common())}")

    if args.check:
        if len(versions) > 1:
            print(f"\nDRIFT: {len(versions)} different toolkit versions pinned across this "
                  f"repo's requirements files — the image would install whichever file its "
                  f"Dockerfile happens to read.", file=sys.stderr)
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
    print(f"\n{verb} {changed} serving pin(s) to {args.to}"
          f"{' (dry run, nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
