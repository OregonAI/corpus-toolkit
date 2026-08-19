"""The declared shape of an object-shaped tool response: response convention 1, open.

WHY THIS FILE EXISTS. `docs/mcp-interface-contract.md` convention 1 requires `corpus`,
`archetype` and `authoritative_source` on every object-shaped response. They were in every
response body and invisible to field-level validation, because `-> dict[str, Any]` emits
`{"additionalProperties": true}` — a real output schema with no `properties` at all. A
conformance harness, a validating client or a release gate could assert nothing about the
convention beyond string-matching a body (corpus-toolkit#15).

WHY IT IS NOT A TypedDict, AND WHY THAT IS NOT A MATTER OF TASTE. v1.24.0 declared one and
took all four live corpora down in a single deploy (corpus-toolkit#61). A TypedDict return
makes the SDK build a pydantic model and push every response through it, and the generated
model is CLOSED:

  1. `authoritative_source: str` rejects None, which is the documented value for a corpus
     that declares no source. `total=False` makes a key optional, not nullable. So
     `corpus_overview`, `resolve_citation` and unknown-id `get_document` became hard
     ValidationErrors on every corpus at once.
  2. Keys the model does not declare are DROPPED on the way out. `get_document` returned
     its three envelope fields and no document body — the payload deleted at serialization
     time, silently, with the call still reporting success.

THE DISTINCTION THIS MODEL TURNS ON: closedness, not declaration. It is worth being precise
about what actually changed at v1.24.0, because the incident is usually remembered as "a
declaration got into the response path" and that is not what happened. Measured on 1.28.1
and 2.0.0, `func_metadata._create_dict_model`: a `-> dict[str, Any]` annotation ALREADY
builds `RootModel[dict[str, Any]]`, and `convert_result` already does
`output_model.model_validate(result).model_dump(mode="json", by_alias=True)`. A pydantic
model has been serializing every object response since long before #58. What #58 changed
was that model's `extra` behaviour, from pass-everything to keep-only-what-I-declared.

So this model declares the three fields and sets `extra="allow"`, which is pydantic's
pass-through: undeclared keys are validated as-is, stored on the instance, and emitted by
`model_dump`. Measured against the RootModel the toolkit ships today, SAME KEYS AND SAME
VALUES on both majors for keys that shadow `BaseModel` methods and attributes (`json`,
`copy`, `schema`, `dict`, `construct`, `validate`, `model_dump`, `model_config`,
`model_fields`, `model_extra`, `model_fields_set`, `model_json_schema`,
`model_computed_fields`, `__dict__`, `__fields__`), leading-underscore and dunder keys, the
empty-string key, non-ASCII keys, non-JSON values (date, Path, tuple), falsy values, and
deep nesting. The declaration describes the envelope; it does not own the payload.

NOT byte-identical, and the difference is key ORDER. `model_dump` emits declared fields
first and extras after, so a response that merged the envelope LAST comes out with it
first: `resolve_citation` returns `{**tool_keys, **self._envelope()}` and its structured
content goes from `['citation','matches','unresolved','corpus',…]` to
`['corpus','archetype','authoritative_source','citation',…]`. Measured, identical on 1.28.1
and 2.0.0. It is inert — JSON objects are unordered by definition, `json.loads` on either
gives the same mapping, and the content blocks are built from the raw return value and do
not move at all — but "byte-identical" is a stronger claim than the measurement supports
and this file is the wrong place to overclaim. Said plainly because the round-trip tests
compare mappings, so nothing in the suite would ever have told you.

WHY THE FIELDS ARE REQUIRED AND `authoritative_source` HAS NO DEFAULT. Nullable-and-required
is the only shape that keeps convention 1's own distinction — `null` means "this corpus
declares no front door", an ABSENT key means "nobody answered the question", and CONTEXT.md's
rule that outranks the vocabulary is that those two must never collapse. A default of None
would silently manufacture the first answer for a tool that gave neither, which is the one
case worth catching.

The cost is real and is stated here rather than discovered: a response that violates
convention 1 is now a hard ValidationError at serialization instead of a quietly
non-conforming answer. Every built-in path assembles the envelope through
`CorpusFramework._envelope()`, so the toolkit's own tools cannot hit it — the exposure is a
corpus supplying `plugins.retrieval_module` whose record overrides one of the three with a
non-string (`get_document` merges the backend record over the envelope). That was already a
convention violation; it now announces itself. See MIGRATION.md.

WHY THE RETURN ANNOTATION AND NOT A POST-HOC SCHEMA PATCH. Setting `output_schema` on the
registered tool afterwards was the other candidate on #15, and it is worse in two ways
neither of which is about SDK internals being private. Measured on both majors: `@tool()`
takes no output-schema argument — the annotation is the only supported declaration — and
`fn_metadata.output_schema` and `fn_metadata.output_model` are used together, the schema
advertised on the wire and the model doing the validating. Patching one and not the other
ships a schema that no longer describes what the server enforces: it would advertise the
three fields as required while the serializer accepted a response without them, so the
advertisement would be a claim nothing checks. That is the failure this issue exists to
stop, one layer up.

WHY ONE SHARED MODEL AND NO PER-TOOL SUBCLASSES. Every extra declared field is another
chance to type something more narrowly than a backend actually emits, and that is the
v1.24.0 failure mode exactly. The convention is a floor shared by six tools; describing
per-tool shapes buys a nicer schema title and takes on that risk six times over. If a tool's
own shape is ever worth declaring, subclass this and keep `extra="allow"`.

NOT IN `_sdk`. That module is for things that DIFFER between the majors, and this does not:
1.28.1 and 2.0.0 emit the identical schema and identical serialized output for this model.
Putting version-independent code there would blur the one boundary the file exists to hold.

WHAT PINS ALL OF THIS: `tests/test_output_schemas.py` (the schema names the fields; a
response missing one is rejected; the null survives; the body survives) and
`tests/test_result_marshalling.py` (every registered tool's real answer, whole-payload,
both halves, both majors). A schema assertion alone cannot see the dropped-payload failure —
that is how v1.24.0 shipped green.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


# THE DOCSTRING BELOW GOES ON THE WIRE. Pydantic copies a model's `__doc__` into the JSON
# Schema as `description`, and the SDK serves that schema on every `tools/list` for six
# tools — so this file's rationale lives above, in the MODULE docstring, where no client
# pays for it. Measured: the first draft's design note added ~1 KiB per tool to the
# inventory response. Keep the class docstring to what a client reading the schema needs.
#
# USED AS AN ANNOTATION, NEVER AS A CONSTRUCTOR. The tools in server.py keep returning
# `fw.<tool>()`'s plain dict; the annotation only tells the SDK what to advertise and
# validate against. Handing the tools model INSTANCES would move `_sdk.call_tool`
# (convert_result=False) and the release gate off the toolkit's own answer and onto the
# SDK's marshalling, which is the separation those two exist for.
#
# NAMED `ResponseEnvelope`, NOT `ObjectResponse`. tests/test_result_marshalling.py's
# adversarial check re-applies daff198's `ObjectResponse` TypedDict verbatim to prove the
# gate still catches v1.24.0; one name with two live meanings in this file's history would
# be its own trap.


class ResponseEnvelope(BaseModel):
    """The three fields every object-shaped corpus response carries (response
    convention 1). `authoritative_source` is null when the corpus declares none.
    Additional, tool-specific properties are expected and are not constrained here."""

    # THE ONE LINE THAT MATTERS. Without it this model is v1.24.0.
    model_config = ConfigDict(extra="allow")

    corpus: str
    archetype: str
    # Nullable AND required: `null` is a documented answer, an absent key is not an answer.
    authoritative_source: str | None
