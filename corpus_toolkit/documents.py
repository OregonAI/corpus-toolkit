"""Writing a corpus document: frontmatter in the platform's order, validated BEFORE it
touches disk.

Nine corpora assembled frontmatter nine ways -- five f-string templates in ERF alone, a
`DOC.format` template in counties, `yaml.safe_dump` with three different widths elsewhere --
and found out from CI afterwards what was missing. ERF's own comment records the failure
class this module exists for: the three platform-required fields "were added by the
multi-corpus work after this template was written, so docs generated here silently lacked
them" (ingest_ors.py). A writer that owns the shape cannot forget a field it validates.

What the writer owns:
  * KEY ORDER: the bundled schema's property order, which is also the template skeleton's
    (`_meta/templates/document.md`). Keys the schema does not know follow, in the order
    given. Two documents from two corpora therefore read the same way.
  * VERIFICATION FIELDS: `last_verified` and `verified_by` are written as `""` when the
    caller gives none. They are a HUMAN act (`docs/verification-loop.md`; `corpus-verify` is
    their one writer) and an ingester never fills them.
  * CORPUS FIELDS: `corpus` and `jurisdiction` default from `_meta/corpus.yml`;
    `schema_version` defaults to 1.
  * VALIDATION: the bundled schema plus this corpus's declared doc_types, id/filename
    agreement, the disclaimer marker, and doc_type/content-root placement -- the same
    per-file checks `corpus-validate-frontmatter` runs -- with every finding reported at
    once as `DocumentError`. Nothing is written when any finding exists.
  * ONE YAML FORMAT: `safe_dump(sort_keys=False, allow_unicode=True, width=100)`; dates as
    quoted ISO strings, so a re-parse gives back exactly what was written.

What it does not own: the body. Conversion, cleanup and section layout are the corpus's
(ADR-0016). It requires only that the body carry the disclaimer marker, as CI does.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import yaml

from corpus_toolkit.repo import parse_frontmatter
from corpus_toolkit.validate.frontmatter import bundled_schema, schema_with_extensions

YAML_WIDTH = 100


class DocumentError(ValueError):
    """The document would not pass CI. `findings` lists every reason; nothing was written."""

    def __init__(self, path, findings: list[str]):
        self.path = Path(path) if path is not None else None
        self.findings = list(findings)
        where = f"{self.path}: " if self.path is not None else ""
        super().__init__(where + "; ".join(self.findings))


def canonical_order() -> list[str]:
    """The bundled schema's top-level property order -- the platform's, not any corpus's."""
    return list(bundled_schema()["properties"])


def _iso(value: Any) -> Any:
    """Dates become ISO strings, recursively. `safe_dump` would emit a bare `2026-09-04`,
    which YAML re-reads as a date object; the schema types these fields as strings."""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat() if value.time() == _dt.time() else value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _iso(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_iso(v) for v in value]
    return value


def ordered_frontmatter(frontmatter: Mapping[str, Any], config=None) -> dict[str, Any]:
    """The frontmatter as it will be written: platform defaults applied, canonical order."""
    fm = {k: _iso(v) for k, v in frontmatter.items()}
    fm.setdefault("schema_version", 1)
    if config is not None:
        fm.setdefault("corpus", config.id)
        fm.setdefault("jurisdiction", config.jurisdiction)
    fm.setdefault("last_verified", "")
    fm.setdefault("verified_by", "")
    order = canonical_order()
    known = {k: fm[k] for k in order if k in fm}
    known.update({k: v for k, v in fm.items() if k not in known})
    return known


def render_document(frontmatter: Mapping[str, Any], body: str, config=None) -> str:
    """The exact text `write_document` writes: `---`, ordered YAML, `---`, blank line, body."""
    head = yaml.safe_dump(ordered_frontmatter(frontmatter, config), sort_keys=False,
                          allow_unicode=True, width=YAML_WIDTH, default_flow_style=False)
    body = body.lstrip("\n").rstrip("\n") + "\n"
    return f"---\n{head}---\n\n{body}"


def findings(frontmatter: Mapping[str, Any], body: str, path: Path | None = None,
             config=None) -> list[str]:
    """Every reason this document would fail `corpus-validate-frontmatter`, or []."""
    fm = ordered_frontmatter(frontmatter, config)
    schema = schema_with_extensions(bundled_schema(), config) if config is not None \
        else bundled_schema()
    out = []
    for err in sorted(jsonschema.Draft202012Validator(schema).iter_errors(fm), key=str):
        where = "/".join(str(x) for x in err.path) or "(root)"
        out.append(f"schema: {where}: {err.message}")
    if path is not None and fm.get("id") != Path(path).stem:
        out.append(f"id '{fm.get('id')}' != filename stem '{Path(path).stem}'")
    if config is not None:
        marker = getattr(config, "disclaimer_marker", None)
        if marker and marker not in body:
            out.append(f"missing '{marker}' disclaimer marker in body")
        if path is not None:
            try:
                parts = Path(path).resolve().relative_to(Path(config.root).resolve()).parts
            except ValueError:
                parts = ()
            if parts:
                expected_here = config.doc_type_for(parts)
                doc_type = fm.get("doc_type")
                if expected_here is not None:
                    if expected_here != doc_type:
                        out.append(f"doc_type '{doc_type}' does not belong under "
                                   f"'{parts[0]}/' (expected doc_type '{expected_here}')")
                else:
                    er = config.expected_root_for(doc_type)
                    if er is not None:
                        out.append(f"doc_type '{doc_type}' belongs under a '{er.path}/' "
                                   f"content root, not here")
    return out


def write_document(config, path: Path, frontmatter: Mapping[str, Any], body: str,
                   *, validate: bool = True) -> Path:
    """Validate, then write `path`. Raises `DocumentError` (nothing written) on any finding.

    Returns the path. The written file re-parses (`corpus_toolkit.repo.parse_frontmatter`)
    to exactly the frontmatter that was validated, which the writer checks before returning
    so a YAML-quoting surprise cannot reach the repository silently.
    """
    path = Path(path)
    if validate:
        problems = findings(frontmatter, body, path, config)
        if problems:
            raise DocumentError(path, problems)
    text = render_document(frontmatter, body, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    back, _ = parse_frontmatter(path)
    expected = ordered_frontmatter(frontmatter, config)
    if back != expected:
        path.unlink()
        raise DocumentError(path, ["the written frontmatter did not re-parse to what was "
                                   "validated; the file was removed"])
    return path
