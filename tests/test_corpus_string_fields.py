"""`corpus.*` string fields are CHECKED at load, and the check is a load-time one.

corpus-toolkit#89. Two failure shapes shared one cause — the loader coerced these fields
without ever asking what they were.

`authoritative_source` was stripped, so a non-string died inside a string method with no
field name attached: `AttributeError: 'list' object has no attribute 'strip'`, which names
neither the file nor the key. Worse, the URL validator that *does* carry a good message
("must be a URL, got ...") could never produce it, because `load()` runs first and dies
before the validator is reached.

`id`, `name` and `jurisdiction` had the opposite problem: no coercion, no check, so a
non-string was simply accepted. `id: 90210` loaded as an int and `id: no` as boolean False,
because YAML 1.1 reads unquoted `no`/`off`/`yes`/`true` as booleans.

That second half became the serious one when corpus-toolkit#103 declared `ResponseEnvelope`
with `corpus: str`. `config.id` lands in that slot on all six object-shaped tools, so a
single unquoted `id: no` stopped being "an odd value in the payload" and became a
ValidationError on every tool call — at runtime, on a config the loader had called good.

The policy is the one `_validated_archetype` already states in its own docstring: a bad
value must fail at LOAD, not surface as a server that starts clean.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus_toolkit import config as config_mod

# Every value here is RAW YAML, written into the file verbatim. That is the point: these
# tests are about what the YAML parser hands the loader, so the unquoted-boolean cases have
# to survive as unquoted booleans rather than being normalised by a Python-side quoting
# helper on the way in.
GOOD = {"id": "test-corpus", "name": "Test Corpus",
        "jurisdiction": "oregon", "archetype": "document"}


def _load(tmp_path: pathlib.Path, **overrides):
    """Load a corpus.yml built from GOOD plus `overrides`, each a raw YAML scalar.

    Built line by line at a fixed indent rather than by dedenting a template: an earlier
    version of this helper concatenated indented blocks, and textwrap's common-prefix rule
    made two different tests emit malformed YAML — which raises, and so reads as a red test
    for entirely the wrong reason.
    """
    fields = {**GOOD, **overrides}
    lines = "".join(f"  {k}: {v}\n" for k, v in fields.items() if v is not None)
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(
        "corpus:\n" + lines
        + "content_roots:\n  - path: documents\n    doc_type: policy\n")
    return config_mod.load(tmp_path / "_meta" / "corpus.yml")


def test_the_baseline_corpus_still_loads(tmp_path):
    """The guard that stops every assertion below from passing for the wrong reason — a
    malformed fixture raises too, and would look like a working check."""
    cfg = _load(tmp_path)

    assert cfg.id == "test-corpus"
    assert cfg.name == "Test Corpus"
    assert cfg.jurisdiction == "oregon"
    assert cfg.authoritative_source is None      # undeclared stays None, not ""


@pytest.mark.parametrize("value, kind", [
    ("[https://a.invalid, https://b.invalid]", "list"),
    ("12345", "int"),
    ("{a: b}", "dict"),
])
def test_a_non_string_authoritative_source_names_the_field(tmp_path, value, kind):
    """The reported symptom. `AttributeError: 'list' object has no attribute 'strip'` names
    neither the file nor the key, so a corpus author gets a stack trace into the toolkit
    instead of "which line of my config is wrong"."""
    with pytest.raises(ValueError) as e:
        _load(tmp_path, authoritative_source=value)

    assert "corpus.authoritative_source" in str(e.value)
    assert kind in str(e.value)


def test_a_string_authoritative_source_still_reaches_the_url_validator(tmp_path):
    """`authoritative_source` has always had a validator with a good message, and it could
    never fire for a non-string because `load()` died first. The load check NARROWS the
    input; it does not replace the judgement downstream of it.

    THIS CALLS THE VALIDATOR. An earlier version asserted only that the string survived
    `load()`, which holds with or without the fix and left the issue's "that message is
    reachable" criterion with no guard anywhere in the suite — so a future change that
    pre-empts the validator at load time, precisely the regression this issue closes, would
    ship green."""
    from corpus_toolkit.validate.frontmatter import _check_config

    cfg = _load(tmp_path, authoritative_source="not-a-url")
    assert cfg.authoritative_source == "not-a-url"      # load accepts it...

    errors = []
    class _Reporter:
        def error(self, rel, msg): errors.append(msg)
        def warn(self, rel, msg): pass
    _check_config(cfg, _Reporter())                     # ...and the validator judges it

    assert any("must be a URL" in m and "not-a-url" in m for m in errors), errors


@pytest.mark.parametrize("field", ["id", "name", "jurisdiction"])
@pytest.mark.parametrize("value", ["90210", "no", "off", "yes", "true", "[a, b]"])
def test_a_non_string_id_name_or_jurisdiction_fails_at_load(tmp_path, field, value):
    """These had NO check, so a non-string was accepted in silence."""
    with pytest.raises(ValueError) as e:
        _load(tmp_path, **{field: value})

    assert f"corpus.{field}" in str(e.value)


@pytest.mark.parametrize("spelling", ["no", "off", "yes", "true"])
def test_the_yaml_boolean_trap_is_named_in_the_error(tmp_path, spelling):
    """"must be a string" is unhelpful when the author typed something that looks exactly
    like one. The fix is quoting, so the error says so."""
    with pytest.raises(ValueError) as e:
        _load(tmp_path, id=spelling)

    assert "quote" in str(e.value).lower()


def test_a_quoted_yaml_boolean_is_a_perfectly_good_id(tmp_path):
    """The other side of the trap: quoting is the advice, so it has to actually work."""
    cfg = _load(tmp_path, id='"no"')

    assert cfg.id == "no"


def test_name_still_defaults_to_id_when_absent(tmp_path):
    """Pre-existing behaviour the new validation must not disturb."""
    cfg = _load(tmp_path, name=None)

    assert cfg.name == "test-corpus"


@pytest.mark.parametrize("id_yaml, expected", [
    ("test-corpus", "test-corpus"),   # ordinary
    ('"no"', "no"),                   # quoted boolean spelling
    ("~", ""),                        # explicit YAML null
    (None, ""),                       # key absent entirely
])
def test_anything_load_accepts_can_fill_the_response_envelope(tmp_path, id_yaml, expected):
    """The property that ties corpus-toolkit#89 to #103, and the reason the loader is the
    right place to enforce this rather than the response model.

    `ResponseEnvelope.corpus` is declared `str`, and `config.id` fills it on all six
    object-shaped tools. A config the loader ACCEPTS must never be able to produce a
    response the serializer rejects — otherwise the failure surfaces at runtime, per tool
    call, on a corpus that started clean.

    PARAMETRIZED OVER THE BOUNDARIES, not the happy path. An earlier version of this test
    loaded only the all-strings fixture and asserted `corpus == "test-corpus"`, which holds
    with or without the fix — a guard for the headline invariant that could not fail, which
    is the same defect the sweep in caf59b9 had. `id: ~` is the case that matters: it
    returned None before this commit, and None in that slot was a live ValidationError on
    every tool call."""
    from corpus_toolkit.mcp.responses import ResponseEnvelope
    cfg = _load(tmp_path, id=id_yaml)

    envelope = ResponseEnvelope.model_validate(
        {"corpus": cfg.id, "archetype": cfg.archetype,
         "authoritative_source": cfg.authoritative_source})

    assert envelope.corpus == expected
    assert isinstance(envelope.corpus, str)


@pytest.mark.parametrize("block, kind", [
    ("corpus:\n", "NoneType"),
    ("corpus: oregon\n", "str"),
    ("corpus:\n  - id: x\n", "list"),
])
def test_a_corpus_block_that_is_not_a_mapping_names_itself(tmp_path, block, kind):
    """The sibling instance of the reported symptom, three lines above the fix for it.

    `raw.get("corpus", {})` was unchecked, so every field read below it raised the exact
    error this issue was filed about — `AttributeError: 'NoneType' object has no attribute
    'get'` — and an empty or mis-indented `corpus:` block is a far more common authoring
    state than `id: 90210`.

    Found in review. #102/#104/#105 are the same story: a site cleared by hand turns out to
    have a sibling instance, so it is worth closing here rather than discovering later."""
    (tmp_path / "_meta").mkdir(exist_ok=True)
    (tmp_path / "_meta" / "corpus.yml").write_text(
        block + "content_roots:\n  - path: documents\n    doc_type: policy\n")

    with pytest.raises(ValueError) as e:
        config_mod.load(tmp_path / "_meta" / "corpus.yml")

    assert "corpus:" in str(e.value)
    assert kind in str(e.value)
