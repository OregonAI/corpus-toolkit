"""The gate covers the template's CMD argv and its requirements extras (corpus-toolkit#116).

corpus-toolkit#100 made the release gate run the template's Dockerfile `RUN` commands. Two
more pieces of consumed surface in the same file were still uncovered, and both fail the same
way: unit tests green, `entrypoints` green, the gate green, every corpus broken.

THE CMD IS HOW THE CONTAINER ACTUALLY STARTS. The gate asserted `corpus-mcp-serve --help`,
which argparse answers with exit 0 regardless of which options exist. Rename
`--public-hostname` to `--public-host`, or make `--config` positional, and: the unit suite
stays green (test_mount_path.py builds the app through `sdk.http_kwargs` and never touches
the parser), the entrypoints job stays green (it asserts `hasattr(module, "main")`), `--help`
still exits 0, the #100 build-command step still passes — and every corpus container
crash-loops on `unrecognized arguments`. That CMD is identical across all seven live corpora.

THE EXTRAS ARE NAMES A CORPUS DEPENDS ON. The template's requirements.txt is
`corpus-toolkit[mcp,semantic] @ git+…`. Delete or rename the `semantic` extra and pip emits
only a warning for the unknown one: the image builds, the gate is green, and every corpus
loses numpy — so `semantic.available()` returns False and the corpus serves keyword-only
WHILE REPORTING HEALTHY. That is the federal-reference incident the extra's own comment in
pyproject.toml records.
"""
import importlib.util
import pathlib
import sys

import pytest

_GATE = pathlib.Path(__file__).resolve().parent.parent / ".github" / "scripts" / "contract_smoke.py"


def _gate_module():
    spec = importlib.util.spec_from_file_location("contract_smoke_116", _GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["contract_smoke_116"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _gate_module()

TEMPLATE_CMD = '''\
FROM python:3.12-slim
WORKDIR /repo
CMD ["corpus-mcp-serve", "--config", "_meta/corpus.yml", "--http", \\
     "--host", "0.0.0.0", "--port", "8000", \\
     "--path", "/oregon-audits/mcp", \\
     "--public-hostname", "oregonai.morficflux.com"]
'''


def test_the_parser_is_reachable_without_starting_a_server():
    """THE TOOLKIT CHANGE THIS ISSUE NEEDS. The parser was built inside `main()`, so the only
    way to ask "would this argv be accepted?" was to run the server."""
    from corpus_toolkit.mcp.server import build_arg_parser

    args = build_arg_parser().parse_args(["--config", "x.yml", "--http", "--port", "8000"])

    assert args.config == "x.yml" and args.http is True and args.port == 8000


def test_main_uses_the_same_parser_it_exposes():
    """Two parsers would drift, and the drift would be invisible: the gate would validate an
    argv that `main` no longer accepts. Same species as the extract-don't-copy rule
    corpus-toolkit#100 established for the RUN commands."""
    import inspect
    from corpus_toolkit.mcp import server

    assert "build_arg_parser()" in inspect.getsource(server.main), (
        "main() builds its own parser, so the gate would validate a different one")


def test_the_cmd_argv_is_extracted_from_the_dockerfile(tmp_path):
    """EXTRACT, DO NOT COPY. A hardcoded duplicate in CI drifts from the Dockerfile and then
    asserts nothing — the same species of bug this closes."""
    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_CMD)

    argv = gate.dockerfile_cmd_argv(d)

    assert argv[0] == "corpus-mcp-serve"
    assert "--public-hostname" in argv and "--path" in argv


def test_the_real_template_cmd_is_accepted_by_the_real_parser():
    """THE POINT. The template's own CMD, through the toolkit's own parser."""
    import os
    from corpus_toolkit.mcp.server import build_arg_parser

    candidates = [os.environ.get("CORPUS_TEMPLATE"),
                  pathlib.Path(__file__).resolve().parent.parent.parent / "corpus-template",
                  pathlib.Path.home() / "corpus-template"]
    real = next((pathlib.Path(c) / "Dockerfile" for c in candidates
                 if c and (pathlib.Path(c) / "Dockerfile").is_file()), None)
    if real is None:
        pytest.skip("no corpus-template checkout found (set CORPUS_TEMPLATE)")

    argv = gate.dockerfile_cmd_argv(real)

    assert argv[0] == "corpus-mcp-serve"
    build_arg_parser().parse_args(argv[1:])       # raises SystemExit if any flag is unknown


def test_a_flag_the_parser_does_not_have_fails(tmp_path):
    """The failure this exists to catch, and the one `--help` cannot see.

    ARGPARSE ACCEPTS UNAMBIGUOUS ABBREVIATIONS, which is worth knowing about what this check
    can and cannot catch: `--public-host` parses fine as `--public-hostname`, so shortening a
    flag is not caught here — and does not need to be, because it still works at runtime for
    the same reason. What is caught is a flag the parser genuinely does not have, which is
    the crash-loop case: the toolkit renames an option and the template's CMD keeps the old
    spelling."""
    from corpus_toolkit.mcp.server import build_arg_parser

    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_CMD.replace("--public-hostname", "--public-host-name"))

    argv = gate.dockerfile_cmd_argv(d)

    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(argv[1:])


def test_the_cmd_is_checked_against_the_parser_the_server_runs(tmp_path):
    """The direction the issue describes: the TOOLKIT renames an option and the template's
    CMD keeps the old spelling. Simulated by parsing the real CMD against a parser missing
    that option — which is what the toolkit would ship after such a rename."""
    import argparse
    from corpus_toolkit.mcp.server import build_arg_parser

    d = tmp_path / "Dockerfile"
    d.write_text(TEMPLATE_CMD)
    argv = gate.dockerfile_cmd_argv(d)

    renamed = argparse.ArgumentParser()
    for action in build_arg_parser()._actions:
        opts = ["--frontend-host" if o == "--public-hostname" else o
                for o in action.option_strings]
        if not opts or "-h" in opts:
            continue
        kw = {"help": action.help}
        if action.nargs == 0:
            kw["action"] = "store_true"
        renamed.add_argument(*opts, **kw)

    with pytest.raises(SystemExit):
        renamed.parse_args(argv[1:])


def test_a_cmd_shape_the_extractor_does_not_understand_is_refused(tmp_path):
    """NARROW BEATS GENERAL, AND SILENCE IS THE ENEMY. Shell-form `CMD` is legal Docker and
    is not the JSON array this parses; skipping it silently is how a gate ends up asserting
    less than it appears to (corpus-toolkit#100)."""
    d = tmp_path / "Dockerfile"
    d.write_text('FROM python:3.12-slim\nCMD corpus-mcp-serve --config _meta/corpus.yml\n')

    with pytest.raises(Exception) as e:
        gate.dockerfile_cmd_argv(d)

    assert "shell" in str(e.value).lower() or "json" in str(e.value).lower()


def test_a_dockerfile_with_no_cmd_fails_rather_than_passing_empty(tmp_path):
    """An extractor that finds nothing and reports success is the defect being fixed."""
    d = tmp_path / "Dockerfile"
    d.write_text("FROM python:3.12-slim\nWORKDIR /repo\n")

    with pytest.raises(Exception) as e:
        gate.dockerfile_cmd_argv(d)

    assert "no cmd" in str(e.value).lower() or "found no" in str(e.value).lower()


def test_declared_extras_are_checked_against_pyproject(tmp_path):
    """`pip install -r requirements.txt` is classified container-only and skipped, so the
    extras — names a corpus depends on — were never checked. pip only WARNS on an unknown
    extra, so deleting `semantic` builds green and every corpus silently loses numpy."""
    req = tmp_path / "requirements.txt"
    req.write_text("corpus-toolkit[mcp,semantic] @ git+https://example.invalid/x@v1.0.0\n")

    assert gate.requirements_extras(req) == {"mcp", "semantic"}


def test_an_extra_no_longer_declared_is_refused(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "1"\n'
                         '[project.optional-dependencies]\nmcp = []\ntest = []\n')
    req = tmp_path / "requirements.txt"
    req.write_text("corpus-toolkit[mcp,semantic] @ git+https://example.invalid/x@v1.0.0\n")

    with pytest.raises(Exception) as e:
        gate.check_requirements_extras(req, pyproject)

    assert "semantic" in str(e.value)


def test_the_real_template_extras_are_all_declared():
    """BASELINE. They agree today and nothing checked that they do."""
    import os
    candidates = [os.environ.get("CORPUS_TEMPLATE"),
                  pathlib.Path(__file__).resolve().parent.parent.parent / "corpus-template",
                  pathlib.Path.home() / "corpus-template"]
    root = next((pathlib.Path(c) for c in candidates
                 if c and (pathlib.Path(c) / "requirements.txt").is_file()), None)
    if root is None:
        pytest.skip("no corpus-template checkout found (set CORPUS_TEMPLATE)")

    gate.check_requirements_extras(
        root / "requirements.txt",
        pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml")
