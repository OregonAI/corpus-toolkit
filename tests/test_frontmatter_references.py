#!/usr/bin/env python3
"""corpus-validate-frontmatter's corpus-wide checks: `joins[].document_id` resolution
(corpus-toolkit#3) and the `corpus.authoritative_source` config check
(corpus-toolkit#6).

Both are gates, so both are written the way a gate has to be tested: the assertion is
that the command EXITS NON-ZERO with the right message on the wrong input, not that it
exits zero on the right one. A guard that only ever sees valid input is
indistinguishable from a guard that cannot fire, and this codebase has shipped nine of
those.
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus_toolkit.validate import frontmatter as fm_mod        # noqa: E402

CORPUS_YML = """\
corpus:
  id: budget
  name: Budget
  jurisdiction: oregon
  archetype: hybrid
{extra}  schema_version: 1
  contract_version: 1
content_roots:
  - path: documents
    doc_type: dataset_doc
"""

DOC = """\
---
schema_version: 1
corpus: budget
jurisdiction: oregon
id: {id}
title: {title}
doc_type: dataset_doc
citation: {title}
issuing_body: Department of Administrative Services
source_url: https://example.org/{id}
source_format: json
status: current
content_mode: summary
last_verified: ""
verified_by: ""
maintainer: "@OregonAI/maintainers"
{joins}---

## At a glance

NON-AUTHORITATIVE curated copy. Verify at source.
"""


class ValidateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="corpus-fmv-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        (self.root / "_meta").mkdir(parents=True)
        (self.root / "documents").mkdir(parents=True)

    def write_corpus(self, *, authoritative_source=None):
        extra = (f"  authoritative_source: {authoritative_source}\n"
                 if authoritative_source is not None else "")
        (self.root / "_meta" / "corpus.yml").write_text(CORPUS_YML.format(extra=extra))

    def write_doc(self, doc_id, title, joins=""):
        (self.root / "documents" / f"{doc_id}.md").write_text(
            DOC.format(id=doc_id, title=title, joins=joins))

    def validate(self, *extra_argv):
        """Run the CLI in-process. Returns (exit_code, combined output)."""
        argv = sys.argv
        sys.argv = ["corpus-validate-frontmatter", "--config",
                    str(self.root / "_meta" / "corpus.yml"), *extra_argv]
        buf = io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                fm_mod.main()
        except SystemExit as e:
            code = e.code or 0
        finally:
            sys.argv = argv
        return code, buf.getvalue()


class TestJoinReferentialIntegrity(ValidateTestCase):
    """corpus-toolkit#3 — the field was shape-validated and nothing read it."""

    JOINS = ("joins:\n"
             "  - document_id: {target}\n"
             "    dataset: expenditures\n"
             "    key: fund-100\n")

    def test_a_dangling_document_id_fails_the_gate(self):
        self.write_corpus(authoritative_source="https://sos.oregon.gov/archives/")
        self.write_doc("appropriation-100", "Appropriation 100",
                       self.JOINS.format(target="does-not-exist"))

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a dangling join:\n{out}")
        self.assertIn("joins[0].document_id", out)
        self.assertIn("'does-not-exist' does not resolve", out)

    def test_a_resolvable_document_id_passes(self):
        self.write_corpus(authoritative_source="https://sos.oregon.gov/archives/")
        self.write_doc("spending-100", "Spending 100")
        self.write_doc("appropriation-100", "Appropriation 100",
                       self.JOINS.format(target="spending-100"))

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertNotIn("joins[", out)

    def test_the_check_links_entry_point_covers_joins_too(self):
        """--check-relationships is what the check-links reusable workflow runs. A join
        is a reference; leaving it out of that path would mean the gate exists in a
        command no corpus's CI actually invokes."""
        self.write_corpus(authoritative_source="https://sos.oregon.gov/archives/")
        self.write_doc("appropriation-100", "Appropriation 100",
                       self.JOINS.format(target="does-not-exist"))

        code, out = self.validate("--check-relationships")

        self.assertEqual(code, 1, f"--check-relationships passed on a dangling join:\n{out}")
        self.assertIn("does not resolve", out)

    def test_a_join_target_outside_the_changed_set_still_resolves(self):
        """The resolution universe must stay corpus-wide even when validation is scoped.
        Otherwise a one-file PR fails on joins that are perfectly valid — the same
        mistake _all_content_ids was written to prevent for relationships."""
        self.write_corpus(authoritative_source="https://sos.oregon.gov/archives/")
        self.write_doc("spending-100", "Spending 100")
        joined = self.root / "documents" / "appropriation-100.md"
        joined.write_text(DOC.format(id="appropriation-100", title="Appropriation 100",
                                     joins=self.JOINS.format(target="spending-100")))

        universe = fm_mod._resolution_universe(
            fm_mod.config_mod.load(self.root / "_meta" / "corpus.yml"), {})
        config = fm_mod.config_mod.load(self.root / "_meta" / "corpus.yml")

        self.assertEqual(fm_mod._join_findings([joined], universe, config), [])


class TestAuthoritativeSourceConfigCheck(ValidateTestCase):
    """corpus-toolkit#6, part 3."""

    def test_a_missing_authoritative_source_fails_the_gate(self):
        """corpus-toolkit#11 — was a warning while the live corpora had not adopted the
        key; every one of them now declares one, so the omission is an error and a new
        corpus cannot ship without a front door."""
        self.write_corpus()
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a missing authoritative_source:\n{out}")
        self.assertIn("corpus.authoritative_source is not set", out)

    def test_the_message_says_what_to_write_and_that_one_url_is_enough(self):
        """corpus-toolkit#70. The wording IS the fix here — this message is where most
        corpora meet the field — so it is asserted rather than left to review, and the
        assertion covers ACTIONABILITY rather than vocabulary. Pinning the phrase "front
        door" alone passes on a message that names the concept and leaves the reader with
        nothing to do: the four things that have to survive a rewrite are the key to set,
        an instruction to set it, the fact that one entry point suffices for a corpus
        spanning several publishers, and the pointer to where per-document precision
        actually comes from. The superseded text — "set it to the URL where the official
        text lives" — was actionable and asserted the one thing that is not true."""
        self.write_corpus()
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, out)
        for fragment, guards in (("corpus.authoritative_source is not set", "which key"),
                                 ("Set it to", "an instruction to act"),
                                 ("front door", "what the value means"),
                                 ("need not cover every publisher", "one URL is enough"),
                                 ("get_document", "where precision comes from"),
                                 ("source_url", "and the field it comes from")):
            self.assertIn(fragment, out,
                          f"the unset error no longer carries {fragment!r} ({guards}), "
                          f"so a corpus reading it cannot act on it:\n{out}")

    def test_a_non_url_authoritative_source_is_an_error(self):
        """Convention 1 says the field IS a URL, so a caller will try to follow it —
        a plausible-looking non-URL is worse than the omission."""
        self.write_corpus(authoritative_source='"Oregon Secretary of State"')
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a non-URL authoritative_source:\n{out}")
        self.assertIn("must be a URL", out)

    def test_the_templates_unedited_placeholder_fails_the_gate(self):
        """corpus-toolkit#11. The literal value corpus-template ships. It PARSES as a URL,
        so an omission-only check waves it through and every MCP response then tells an
        agent to verify at a host RFC 2606 guarantees can never exist — the failure this
        gate exists to prevent, merely relocated."""
        self.write_corpus(
            authoritative_source='"https://REPLACE-ME.invalid/where-the-official-text-lives"')
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on the template placeholder:\n{out}")
        self.assertIn("RFC 2606", out)
        self.assertIn("Set it to", out)

    def test_every_rfc2606_reserved_name_fails_the_gate(self):
        """The rule is the reserved NAMES, not a `REPLACE-ME` string match: a corpus that
        edits the path and leaves the host is still shipping a dead pointer, and each of
        these is a host no corpus's official text can ever live under."""
        for url in ("https://REPLACE-ME.invalid/where-the-official-text-lives",
                    "https://sos.oregon.example/archives",
                    "http://records.test/schedules",
                    "https://localhost/official",
                    "https://corpus.localhost:8080/official",
                    "https://example.com/official",
                    "https://www.example.net/official",
                    "https://example.org/budget"):
            with self.subTest(url=url):
                self.write_corpus(authoritative_source=f'"{url}"')
                self.write_doc("spending-100", "Spending 100")

                code, out = self.validate()

                self.assertEqual(code, 1, f"gate passed on {url}:\n{out}")
                self.assertIn("RFC 2606", out)

    def test_a_url_shaped_value_that_cannot_be_parsed_is_a_finding(self):
        """`urlsplit` RAISES on a malformed authority (`https://[oops` — "Invalid IPv6
        URL"), and this value reaches it having passed the `https://` prefix check. An
        exception out of a validator is a traceback naming neither the file nor the key —
        the failure mode config._validated_corpus_string exists to end."""
        self.write_corpus(authoritative_source='"https://[oops"')
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on an unparseable URL:\n{out}")
        self.assertIn("corpus.authoritative_source", out)
        self.assertIn("cannot be parsed", out)

    def test_a_url_naming_no_host_is_a_finding(self):
        """`https:///schedules` starts with `https://`, so the is-it-a-URL branch passes
        it, and a check that only asks about the host's NAME has no name to ask about. It
        must not fall through clean: convention 1 promises a caller somewhere to go."""
        self.write_corpus(authoritative_source='"https:///schedules"')
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a URL naming no host:\n{out}")
        self.assertIn("names no host", out)

    def test_replace_me_left_in_a_host_fails_even_off_a_reserved_name(self):
        """The RFC 2606 rule is the general one, but it has an edge: an author who swaps
        `.invalid` for a real TLD, or drops it, leaves `REPLACE-ME` sitting in a host the
        reserved-name rule no longer covers. The template's own marker is worth naming."""
        for url in ("https://REPLACE-ME.oregon.gov/where-the-official-text-lives",
                    "http://REPLACE-ME/where-the-official-text-lives"):
            with self.subTest(url=url):
                self.write_corpus(authoritative_source=f'"{url}"')
                self.write_doc("spending-100", "Spending 100")

                code, out = self.validate()

                self.assertEqual(code, 1, f"gate passed on {url}:\n{out}")
                self.assertIn("REPLACE-ME", out)

    def test_a_real_host_whose_path_says_example_is_not_a_placeholder(self):
        """The check reads the HOST, not the URL text. A substring match would reject
        `https://sos.oregon.gov/archives/example-schedules` — a real front door."""
        self.write_corpus(
            authoritative_source='"https://sos.oregon.gov/archives/example-schedules"')
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertNotIn("RFC 2606", out)

    # ---- the template must still validate itself (corpus-toolkit#11) ----
    #
    # `corpus-template` ships the `.invalid` placeholder deliberately: a bare
    # `{{AUTHORITATIVE_SOURCE_URL}}` is not a URL, and since v1.10.0 that is an error, so
    # the template could not pass its own CI. Making the placeholder an error too would
    # break it the same way — and a template that does not validate is a template every
    # corpus starts life failing. What separates the two states is that the template is
    # not a corpus yet: its `corpus.id` is still the unfilled `{{CORPUS_ID}}` and it holds
    # no documents. Filling the id is step one of the replication guide, and adding the
    # first document is what makes a repo a corpus; either one turns these back into
    # errors.

    TEMPLATE_SOURCE = '"https://REPLACE-ME.invalid/where-the-official-text-lives"'

    def write_template(self, *, authoritative_source=TEMPLATE_SOURCE):
        """corpus.yml as corpus-template ships it: unfilled id, placeholder front door."""
        extra = (f"  authoritative_source: {authoritative_source}\n"
                 if authoritative_source is not None else "")
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML.format(extra=extra).replace("id: budget", 'id: "{{CORPUS_ID}}"'))

    def test_the_uninstantiated_template_still_validates(self):
        self.write_template()

        code, out = self.validate()

        self.assertEqual(code, 0, f"the template fails its own CI:\n{out}")
        self.assertIn("warning", out)
        self.assertIn("{{CORPUS_ID}}", out)
        self.assertIn("RFC 2606", out)

    def test_the_template_warning_says_when_it_becomes_an_error(self):
        """A warning a reader cannot act on is how the template ships broken to a corpus."""
        self.write_template()

        code, out = self.validate()

        for fragment, guards in (("corpus.id", "which key is unfilled"),
                                 ("no documents", "the other half of the condition"),
                                 ("error", "what happens next")):
            self.assertIn(fragment, out,
                          f"the template warning no longer carries {fragment!r} "
                          f"({guards}):\n{out}")

    def test_a_template_that_holds_a_document_is_a_corpus_and_fails(self):
        """The exemption cannot become the way to keep a placeholder: a repo that forked
        the template, added documents and never edited corpus.yml is a corpus shipping a
        dead front door, which is exactly what #11 is about."""
        self.write_template()
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a corpus with documents:\n{out}")
        self.assertIn("RFC 2606", out)

    def test_a_named_corpus_with_no_documents_still_fails(self):
        """The other half: an empty repo that has filled in its id is a corpus being set
        up, not the template, and it must not ship a placeholder front door."""
        self.write_corpus(authoritative_source=self.TEMPLATE_SOURCE)

        code, out = self.validate()

        self.assertEqual(code, 1, f"gate passed on a named corpus:\n{out}")
        self.assertIn("RFC 2606", out)

    def test_an_unfilled_id_is_reported_even_with_a_real_front_door(self):
        """The state is never silent — it is what suspends two errors."""
        self.write_template(authoritative_source='"https://sos.oregon.gov/archives/"')

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertIn("{{CORPUS_ID}}", out)

    def test_a_real_url_is_silent(self):
        self.write_corpus(authoritative_source="https://sos.oregon.gov/archives/")
        self.write_doc("spending-100", "Spending 100")

        code, out = self.validate()

        self.assertEqual(code, 0, out)
        self.assertNotIn("authoritative_source", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
