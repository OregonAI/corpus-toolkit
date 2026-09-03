"""The corpora manifest (ADR-0008) and the lists the platform generates from it (ADR-0014).

    corpus-manifest --json               # the whole manifest, validated
    corpus-manifest --canary-matrix      # repos the release gate validates before `v1` moves
    corpus-manifest --pin-matrix         # repos propagate-pin opens a requirements bump in
    corpus-manifest --names [--status live|retired|all]
    corpus-manifest --ci-track-state /path/to/a/corpus/checkout

Nine files across four repos used to name "which corpora exist", and at least one was
wrong in both directions — a propagation matrix targeting a repository that did not exist
and an archived one, while omitting the largest corpus and the whole consumer tier
(corpus-toolkit#83, ADR-0004). The tooling lists now generate from `schemas/corpora.yml`;
the runtime lists (`corpus-gateway/src/registry.py`, `corpus-chat/src/corpora.py`) stay
deliberately duplicated and are only CHECKED against it.

`--ci-track-state` is the release gate's held-corpus detector. A corpus is on the CI track
when its workflows call the reusable workflows at a bare major tag (`@v1`); it is HELD when
it pins an exact tag (`@v1.31.1`) instead, which is how a corpus opts out of a release it
cannot yet take. The canary still runs a held corpus and reports it, but a held corpus does
not block `v1` from moving (ADR-0014). Read from the corpus's own workflow files rather
than from a manifest field, so there is nothing to keep in sync.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from importlib import resources

import yaml

TIERS = frozenset({"corpus", "consumer", "platform"})
VISIBILITIES = frozenset({"public", "private"})
STATUSES = frozenset({"live", "retired"})
FIELDS = ("name", "tier", "visibility", "status", "ci_track", "toolkit_pin")

# `uses: OregonAI/corpus-toolkit/.github/workflows/<file>.yml@<ref>`
_USES = re.compile(r"uses:\s*OregonAI/corpus-toolkit/\.github/workflows/[^@\s]+@(?P<ref>\S+)")
_MAJOR = re.compile(r"^v\d+$")
_EXACT = re.compile(r"^v\d+\.\d+\.\d+$")


class ManifestError(ValueError):
    """The manifest does not describe an org this tooling can act on."""


def manifest_text() -> str:
    return (resources.files("corpus_toolkit").joinpath("schemas/corpora.yml")
            .read_text(encoding="utf-8"))


def validate(repos: list[dict]) -> None:
    """Every problem at once, so a bad edit is fixed in one round."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(repos):
        where = f"repos[{i}]" + (f" ({r.get('name')})" if isinstance(r, dict) and r.get("name") else "")
        if not isinstance(r, dict):
            problems.append(f"{where}: not a mapping")
            continue
        missing = [f for f in FIELDS if f not in r]
        extra = sorted(set(r) - set(FIELDS))
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
        if extra:
            problems.append(f"{where}: unknown field(s) {', '.join(extra)}")
        name = r.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"{where}: name must be a non-empty string")
        elif name in seen:
            problems.append(f"{where}: duplicate name")
        else:
            seen.add(name)
        if r.get("tier") not in TIERS:
            problems.append(f"{where}: tier must be one of {sorted(TIERS)}")
        if r.get("visibility") not in VISIBILITIES:
            problems.append(f"{where}: visibility must be one of {sorted(VISIBILITIES)}")
        if r.get("status") not in STATUSES:
            problems.append(f"{where}: status must be one of {sorted(STATUSES)}")
        for flag in ("ci_track", "toolkit_pin"):
            if not isinstance(r.get(flag), bool):
                problems.append(f"{where}: {flag} must be true or false")
        if r.get("visibility") == "private" and r.get("ci_track") is True:
            problems.append(f"{where}: a private repo cannot be on the CI track — the public "
                            f"release gate cannot clone it, so its canary leg would report "
                            f"'could not check' as 'passed'")
        if r.get("status") == "retired" and (r.get("ci_track") or r.get("toolkit_pin")):
            problems.append(f"{where}: a retired repo is neither canaried nor bumped")
    if problems:
        raise ManifestError("corpora.yml:\n  " + "\n  ".join(problems))


def load_manifest(text: str | None = None) -> list[dict]:
    data = yaml.safe_load(manifest_text() if text is None else text)
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise ManifestError("corpora.yml: top level must be a mapping with a `repos` list")
    validate(data["repos"])
    return data["repos"]


def live_corpora(repos: list[dict]) -> list[str]:
    return [r["name"] for r in repos if r["tier"] == "corpus" and r["status"] == "live"]


def canary_targets(repos: list[dict]) -> list[str]:
    """Live, public, CI-track repos of the CORPUS tier.

    The template is on the CI track too (it calls the reusable workflows and is swept with
    the corpora), but it is not a corpus: its `_meta/corpus.yml` still carries
    `{{CORPUS_NAME}}` placeholders, so full validation of the template as it sits in git
    is not a meaningful run. The release gate proves the template by INSTANTIATING it
    (contract_smoke.py), which is the check that matches what it is for.
    """
    return [r["name"] for r in repos
            if r["status"] == "live" and r["visibility"] == "public" and r["ci_track"]
            and r["tier"] == "corpus"]


def pin_targets(repos: list[dict]) -> list[str]:
    return [r["name"] for r in repos if r["status"] == "live" and r["toolkit_pin"]]


def names(repos: list[dict], status: str = "all") -> list[str]:
    return [r["name"] for r in repos if status == "all" or r["status"] == status]


def ci_track_state(repo: pathlib.Path) -> dict:
    """How a checked-out corpus pins the reusable workflows: floating, held, mixed or none.

    Returns {"state": ..., "floating": [refs], "exact": [refs], "other": [refs]}.
    `mixed` — some calls at `@v1`, some at an exact tag — is reported as its own state
    rather than collapsed into either, because it is the partial bump `bump_pins.py`'s
    `--check` was written to catch, one shape over.
    """
    floating, exact, other = [], [], []
    for path in sorted((repo / ".github" / "workflows").glob("*.yml")) + \
            sorted((repo / ".github" / "workflows").glob("*.yaml")):
        for m in _USES.finditer(path.read_text(encoding="utf-8")):
            ref = m.group("ref")
            (floating if _MAJOR.match(ref) else exact if _EXACT.match(ref) else other).append(ref)
    if not (floating or exact or other):
        state = "none"
    elif exact and floating:
        state = "mixed"
    elif exact:
        state = "held"
    elif floating:
        state = "floating"
    else:
        state = "other"
    return {"state": state, "floating": sorted(set(floating)), "exact": sorted(set(exact)),
            "other": sorted(set(other))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", action="store_true", help="print the validated manifest")
    g.add_argument("--canary-matrix", action="store_true",
                   help="JSON list: live, public, ci_track repos (release-gate canary)")
    g.add_argument("--pin-matrix", action="store_true",
                   help="JSON list: live repos that pin the toolkit (propagate-pin)")
    g.add_argument("--names", action="store_true", help="JSON list of repo names")
    g.add_argument("--ci-track-state", type=pathlib.Path, metavar="CORPUS",
                   help="report how a checked-out corpus pins the reusable workflows")
    ap.add_argument("--status", choices=["live", "retired", "all"], default="all",
                    help="with --names: which entries (default all)")
    args = ap.parse_args(argv)

    if args.ci_track_state:
        print(json.dumps(ci_track_state(args.ci_track_state)))
        return 0
    try:
        repos = load_manifest()
    except ManifestError as e:
        print(e, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(repos, indent=2))
    elif args.canary_matrix:
        print(json.dumps(canary_targets(repos)))
    elif args.pin_matrix:
        print(json.dumps(pin_targets(repos)))
    elif args.names:
        print(json.dumps(names(repos, args.status)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
