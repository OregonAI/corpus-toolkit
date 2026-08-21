"""Tests for drift reporting — that a failed report is LOUD.

corpus-toolkit#53: `_open_issue()` called `subprocess.run` bare and discarded the
returncode. oregon-collective-bargaining's weekly drift run detected 618 changed sources,
attempted 618 issue creations, created ZERO — the repo had no `source-change` label, so
every `gh issue create --label source-change` exited non-zero — and printed
`618 changed, 58 fetch failure(s)`, which reads as "618 were filed".

The bug was never in the detection. It was that detecting and reporting were counted by
the same number, so a reporter doing nothing was indistinguishable from one doing its job.
These tests pin the distinction: the return value must track whether an issue exists, and
the summary must say what was REPORTED, not only what drifted.
"""
import io
import json
import shutil
import sys
import tempfile
import textwrap
import unittest

import pytest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from corpus_toolkit.repo import content_hash
from corpus_toolkit.sources import changes


def _completed(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class OpenIssueReturnsOutcomeTest(unittest.TestCase):
    """_open_issue must report whether an issue actually exists afterwards."""

    def test_returns_false_and_explains_when_gh_create_fails(self):
        # `gh issue list` finds nothing open, then `gh issue create` fails — the exact
        # shape of the 618-failure case, where the label did not exist.
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run", side_effect=[
                 _completed(0, "0"),
                 _completed(1, "", "could not add label: 'source-change' not found"),
             ]):
            err = io.StringIO()
            with redirect_stderr(err):
                ok = changes._open_issue("doc-1", "https://x/1", "aaa", "bbb")
        self.assertFalse(ok, "a failed `gh issue create` must not report success")
        self.assertIn("FAILED to open issue for doc-1", err.getvalue())
        self.assertIn("source-change", err.getvalue(),
                      "the operator needs gh's own reason, not a generic message")

    def test_returns_true_on_success(self):
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run", side_effect=[
                 _completed(0, "0"), _completed(0, "https://github.com/o/r/issues/1"),
             ]):
            self.assertTrue(changes._open_issue("doc-1", "https://x/1", "a", "b"))

    def test_already_open_counts_as_reported(self):
        # An issue that is already open IS reporting — it must not read as a failure and
        # trip the "every creation failed" alarm.
        with mock.patch.object(changes.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(changes.subprocess, "run",
                               side_effect=[_completed(0, "1")]):
            out = io.StringIO()
            with redirect_stdout(out):
                ok = changes._open_issue("doc-1", "https://x/1", "a", "b")
        self.assertTrue(ok)
        self.assertIn("already open", out.getvalue())

    def test_no_gh_binary_is_not_silent(self):
        with mock.patch.object(changes.shutil, "which", return_value=None):
            err = io.StringIO()
            with redirect_stderr(err):
                ok = changes._open_issue("doc-1", "https://x/1", "a", "b")
        self.assertFalse(ok)
        self.assertIn("gh", err.getvalue())


class EnsureLabelTest(unittest.TestCase):
    """The label is a hard dependency of `--label`, and nothing else creates it."""

    def test_creates_label_with_force(self):
        with mock.patch.object(changes.subprocess, "run",
                               return_value=_completed(0)) as run:
            self.assertTrue(changes._ensure_label())
        args = run.call_args[0][0]
        self.assertEqual(args[:4], ["gh", "label", "create", changes.ISSUE_LABEL])
        self.assertIn("--force", args,
                      "--force makes this idempotent; without it a second run errors")

    def test_warns_but_does_not_raise_when_label_creation_fails(self):
        with mock.patch.object(changes.subprocess, "run",
                               return_value=_completed(1, "", "denied")):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertFalse(changes._ensure_label())
        self.assertIn("could not ensure", err.getvalue())


class CapTest(unittest.TestCase):
    def test_cap_is_small_enough_to_surface_a_broken_baseline(self):
        # The failure this guards against is a manifest of 680 sources with `sha256: ''`,
        # where every source drifts every run. The cap only helps if it is far below that.
        self.assertLessEqual(changes.MAX_ISSUES_PER_RUN, 50)
        self.assertGreaterEqual(changes.MAX_ISSUES_PER_RUN, 5)


CORPUS_YML = """\
schema_version: 1
corpus:
  id: test-corpus
  name: Test Corpus
  jurisdiction: oregon
  archetype: document
content_roots:
  - path: "documents"
    doc_type: rule
source_manifest_path: _meta/sources
"""


def _body(i: int) -> bytes:
    return b"<html><body><p>" + f"Rule text for source {i}. ".encode() * 20 + b"</p></body></html>"


class _DriftRun(unittest.TestCase):
    """Drives main() over a real manifest on disk with only the network faked."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "_meta" / "sources").mkdir(parents=True)
        (self.root / "documents").mkdir()
        (self.root / "_meta" / "corpus.yml").write_text(CORPUS_YML)
        self.bodies = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def group(self, name: str, n: int, *, baseline: str | None, start: int = 0,
              file_stem: str | None = None):
        """Write a group of n sources. baseline=None means `sha256: \'\'` (unseeded),
        "current" means the hash they will actually fetch, anything else is a literal.

        `file_stem` writes the group to a differently-named file and declares `group:`
        inside it, which is what a real manifest does when the file name and the group
        name disagree. That is the only way the manifest's iteration order (file name)
        can differ from the group name, so it is the only way to observe a tiebreak that
        sorts on the name."""
        lines = ["sources:"] if file_stem is None else [f"group: {name}", "sources:"]
        for i in range(start, start + n):
            url = f"https://example.gov/{name}/{i}"
            self.bodies[url] = _body(i)
            if baseline is None:
                sha = ""
            elif baseline == "current":
                sha = content_hash(_body(i), "html")
            else:
                sha = baseline
            lines.append(textwrap.dedent(f"""\
                  - id: "{name}-{i}"
                    url: "{url}"
                    format: html
                    sha256: "{sha}"
            """).rstrip())
        stem = file_stem or name
        (self.root / "_meta" / "sources" / f"{stem}.yml").write_text("\n".join(lines) + "\n")

    def _fetch(self, url):
        # RAISES rather than returning the exception. Returning it let a "failed" fetch
        # reach `len(raw)` and be counted as a normalizable source before dying, so a test
        # about blocked fetches was silently exercising a shape `fetch()` cannot produce.
        body = self.bodies[url]
        if isinstance(body, Exception):
            raise body
        return body

    def run_cli(self, *argv):
        args = ["corpus-detect-changes", "--config",
                str(self.root / "_meta" / "corpus.yml"), *argv]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             mock.patch.object(changes, "_ensure_label", return_value=True), \
             mock.patch.object(changes, "_open_issue", return_value=True) as open_issue, \
             redirect_stdout(out), redirect_stderr(err):
            self.open_issue = open_issue
            try:
                changes.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()


class GroupBreakdownTest(_DriftRun):
    """corpus-toolkit#67 item 1: the one line that identifies a bulk false positive.

    ERF\'s capped run was 484/484 in `oar` and 52/52 in a DEQ group — two template-level
    faults — while three other groups carried the five genuine changes. The totals line
    could not distinguish that from 544 real revisions, and nobody could without opening
    every issue.
    """

    def test_breakdown_appears_on_an_uncapped_run_too(self):
        self.group("oar", 4, baseline="stale")
        self.group("oam", 3, baseline="current")
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("oar 4/4", out)
        self.assertIn("oam 0/3", out)

    def test_group_names_are_the_ones_the_group_flag_accepts(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 2, baseline="stale")
        _, out, _ = self.run_cli("--group", "oar")
        self.assertIn("oar 2/2", out)
        self.assertNotIn("oam", out, "an out-of-scope group is not reported as 0 drift")

    def test_unseeded_sources_are_marked_in_the_breakdown(self):
        # The two shapes that both read as 100% drift: a stale/altered baseline (#66) and
        # no baseline at all (#68). One character of the breakdown separates them.
        self.group("oar", 3, baseline="stale")
        self.group("counties", 2, baseline=None)
        _, out, _ = self.run_cli()
        self.assertIn("counties 2/2", out)
        self.assertIn("unseeded", out)


class CappedRunIsNotACleanRunTest(_DriftRun):
    """corpus-toolkit#67 item 3: the delivery channel was the broken part.

    Both observed occurrences — ERF (519 dropped) and oregon-counties (3,366 dropped) —
    concluded `success` with the truncation notice on stderr near the end of a
    multi-thousand-line log. A correct diagnosis and an incorrect one produced the same
    outcome, because in neither case did anyone read it.
    """

    def test_a_capped_run_exits_non_zero(self):
        self.group("oar", changes.MAX_ISSUES_PER_RUN + 5, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1, "a truncated run must be distinguishable from a clean one")
        self.assertIn("STOPPED after", err)

    def test_an_uncapped_run_with_drift_still_exits_zero(self):
        # Unchanged behaviour: a changed source is a signal, not an error.
        self.group("oar", 3, baseline="stale")
        code, _, _ = self.run_cli("--open-issues")
        self.assertEqual(code, 0)

    def test_capped_message_does_not_assert_an_empty_baseline_when_there_is_none(self):
        # ERF had ZERO empty baselines and was told to go check the manifest for them.
        self.group("oar", changes.MAX_ISSUES_PER_RUN + 5, baseline="stale")
        _, _, err = self.run_cli("--open-issues")
        self.assertIn("0 of", err)
        self.assertIn("not the cause", err.lower())
        self.assertNotIn("usually means the manifest baseline is empty", err)

    def test_capped_message_reports_the_measured_unseeded_count_when_there_is_one(self):
        n = changes.MAX_ISSUES_PER_RUN + 5
        self.group("counties", n, baseline=None)
        _, _, err = self.run_cli("--open-issues")
        self.assertIn(f"{n} of {n}", err)
        self.assertIn("--record-baseline", err)


class BudgetIsSpentSmallestGroupFirstTest(_DriftRun):
    """corpus-toolkit#69: the cap decides WHICH issues are filed, and it used to decide by
    manifest iteration order alone.

    ERF run 31022774644: 544 changed, 25 opened, 519 dropped. The 52-source DEQ group came
    first alphabetically and consumed the whole budget, so the five genuine changes in three
    small agency groups — and the 484-source `oar` template change that was 89% of the drift
    — got no ticket at all. Nothing about the budget was allocated; it was simply spent by
    whoever the loop reached first.
    """

    def _filed(self):
        return [c.args[0] for c in self.open_issue.call_args_list]

    def test_a_noisy_group_no_longer_shuts_out_the_small_ones(self):
        # Alphabetically first AND far over the cap on its own: the ERF shape.
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("wrd", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        filed = self._filed()
        self.assertEqual(len(filed), changes.MAX_ISSUES_PER_RUN,
                         "the cap is not weakened — it still bounds the run")
        self.assertEqual({"oam-0", "oam-1"}, {s for s in filed if s.startswith("oam-")},
                         "every source in the smallest drifting group must be filed")
        self.assertEqual({"wrd-0", "wrd-1", "wrd-2"},
                         {s for s in filed if s.startswith("wrd-")},
                         "a small genuine finding must not be starved by a bulk one")
        self.assertEqual(code, 1, "a capped run is still not a clean run")


    def test_equal_sized_groups_are_ordered_by_name_not_by_file_position(self):
        # Two groups drifting equally, written to files whose names sort the OPPOSITE way
        # to the group names — the manifest's own iteration order. Without a tiebreak on
        # something belonging to the group, the run files whichever file the loader
        # happened to open first, and moving a group between files silently changes which
        # sources get reported.
        n = changes.MAX_ISSUES_PER_RUN - 12   # 13 each: the second group cannot fit whole
        self.group("zebra", n, baseline="stale", file_stem="a-first")
        self.group("alpha", n, baseline="stale", file_stem="b-second")
        self.run_cli("--open-issues")
        filed = self._filed()
        self.assertEqual(len(filed), changes.MAX_ISSUES_PER_RUN)
        self.assertEqual(len([s for s in filed if s.startswith("alpha-")]), n,
                         "the tie must be broken by the group name, so `alpha` files whole")
        self.assertEqual([s for s in filed if s.startswith("zebra-")],
                         [f"zebra-{i}" for i in range(changes.MAX_ISSUES_PER_RUN - n)],
                         "within a group the manifest's own order must survive the sort")

    def test_a_re_run_over_the_same_drift_files_the_same_set(self):
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("wrd", 3, baseline="stale")
        self.run_cli("--open-issues")
        first = self._filed()
        self.run_cli("--open-issues")
        self.assertEqual(first, self._filed(),
                         "an unstable order means a re-run reports a different set and "
                         "nobody can tell why")


    def test_a_capped_run_says_the_budget_was_allocated_and_how(self):
        # A group at 484/484 in the breakdown with no issue against it now means "the
        # budget went to smaller groups first", not "reporting failed" (corpus-toolkit#53
        # is the reason that distinction has to be printed rather than inferred).
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("oar", 30, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1)
        self.assertIn("smallest-group-first", err.lower())
        self.assertIn("oam (2 of 2)", err)
        self.assertIn("oar (23 of 30)", err)
        self.assertIn("deq (0 of 40)", err)
        self.assertIn("not reported at all", err)

    def test_the_starved_group_line_does_not_fire_when_every_group_got_a_ticket(self):
        # Capped, but the budget reached all three groups (deq takes the remaining 20 of
        # its 40). A line that says "not reported at all" on a run where every drifting
        # group WAS reported would send an operator looking for a group that is not there.
        self.group("deq", 40, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.group("wrd", 3, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 1)
        self.assertIn("deq (20 of 40)", err, "the run is capped and the line is printed")
        self.assertNotIn("not reported at all", err)

    def test_an_uncapped_run_does_not_print_the_allocation_line(self):
        # Nothing was allocated: every drifting source was filed. Printing the policy
        # anyway would train an operator to skim past it on the run where it matters.
        self.group("oam", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(code, 0)
        self.assertNotIn("smallest-group-first", (err + out).lower())


class UnseededManifestIsNotDriftTest(_DriftRun):
    """corpus-toolkit#68: a run with no baseline cannot detect drift, and must say so."""

    def test_every_run_reports_the_measured_unseeded_count(self):
        self.group("oar", 2, baseline="current")
        self.group("counties", 3, baseline=None)
        _, out, _ = self.run_cli()
        self.assertIn("3 with no recorded baseline", out)

    def test_wholly_unseeded_run_files_nothing_and_names_the_seeding_mode(self):
        self.group("counties", 4, baseline=None)
        code, out, err = self.run_cli("--open-issues")
        self.open_issue.assert_not_called()
        self.assertEqual(code, 1, "detection is inert here; the run must not report success")
        self.assertIn("--record-baseline", err)

    def test_a_partly_seeded_manifest_still_files(self):
        # The refusal is for the wholly-unseeded case only — one unseeded source among
        # real ones must not switch reporting off for the corpus.
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli("--open-issues")
        self.assertEqual(self.open_issue.call_count, 3)
        self.assertEqual(code, 0)


class NothingCheckedIsNotCleanTest(_DriftRun):
    def test_an_empty_group_scope_does_not_exit_zero(self):
        # `--group` takes a free-text name and nothing validates it against the manifest,
        # so a typo checked nothing and reported "0 changed ... of 0 checked", exit 0 —
        # could-not-check served as not-there, inside the run that exists to prevent it.
        self.group("oar", 2, baseline="current")
        code, out, err = self.run_cli("--group", "nosuchgroup", "--open-issues")
        self.assertEqual(code, 1)
        self.assertIn("NOTHING WAS CHECKED", err)
        self.assertIn("nosuchgroup", err)


class InertRunOutputsTest(_DriftRun):
    def test_inert_run_does_not_set_changed_true_for_the_workflow(self):
        # `changed=true` fires whatever the calling workflow does next. On an inert run
        # every source "changed" against an empty baseline, which is not a finding.
        self.group("counties", 3, baseline=None)
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        text = gh.read_text()
        self.assertIn("changed=false", text)
        self.assertIn("unseeded=3", text)

    def test_a_real_drift_run_still_sets_changed_true(self):
        self.group("oar", 1, baseline="stale")
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        self.assertIn("changed=true", gh.read_text())




class WatchPathReportingTest(_DriftRun):
    """A declared `watch` path that is not in the document (corpus-toolkit#72).

    The first version routed this into `failed` and printed it to stdout, which made it:
    counted as a "fetch failure" in the totals line, listed under "a fact about our access,
    not about upstream" — the precise opposite of what it is — folded into the >20% SYSTEMIC
    threshold, and invisible in CI (no annotation, exit 0). A watched field disappearing
    upstream is one of the most actionable things this tool can find, and it was the
    quietest.
    """

    def totals_line(self, out: str) -> str:
        """The `N changed, …` line ALONE.

        `assertIn("1 not compared", out)` was satisfied by the group breakdown's
        `gone 0/1 [1 not compared]`, so three tests naming the totals line in their
        docstrings passed with that clause deleted from it entirely."""
        return next(l for l in out.splitlines() if l.startswith(tuple("0123456789"))
                    and " changed, " in l)

    def json_group(self, name: str, docs: dict, *, watch: list, baseline="current",
                   declare_format=True):
        """A group of json sources with a `watch` list. `docs` maps id -> dict body.

        `declare_format=False` omits `format:`, which is what a real Socrata entry looks
        like — and `_format_for` maps an unrecognised `.json` extension to `"html"`."""
        lines = ["sources:"]
        for sid, doc in docs.items():
            url = f"https://example.gov/{name}/{sid}.json"
            raw = json.dumps(doc).encode()
            self.bodies[url] = raw
            try:
                sha = content_hash(raw, "json", watch=watch) if baseline == "current" else baseline
            except Exception:
                sha = "unknowable"
            watch_yaml = "\n".join(f"      - {w}" for w in watch)
            fmt = '\n                    format: json' if declare_format else ""
            lines.append(textwrap.dedent(f"""\
                  - id: "{sid}"
                    url: "{url}"{fmt}
                    sha256: "{sha}"
                    watch:
                """).rstrip() + "\n" + watch_yaml)
        (self.root / "_meta" / "sources" / f"{name}.yml").write_text("\n".join(lines) + "\n")

    def test_a_missing_watched_path_is_not_counted_as_a_fetch_failure(self):
        """The bytes ARRIVED. Calling that a fetch failure names a condition other than the
        one that occurred, and points the operator at the network instead of at upstream's
        schema."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}, "b": {"rowsUpdatedAt": 2}},
                        watch=["rowsUpdatedAt"])
        self.json_group("gone", {"c": {"viewCount": 9}}, watch=["rowsUpdatedAt"])

        code, out, err = self.run_cli()

        self.assertIn("0 fetch failure(s)", out,
                      "a watched-path miss was counted as a failed fetch")
        self.assertNotIn("a fact about our access", out + err,
                         "a document that arrived was listed under an access problem")
        # And the totals line must not say `of 3 checked` when one of the three was not
        # compared to anything — that is could-not-check reported as checked, on the one
        # line an operator reads.
        self.assertIn("1 not compared", self.totals_line(out),
                      f"the totals line counted an uncompared source as checked:\n{out}")

    def test_a_missing_watched_path_is_visible_where_ci_looks(self):
        """It printed to stdout and exited 0, so a weekly run reported success while one
        source had silently stopped being checked at all — corpus-toolkit#67's failure mode,
        rebuilt inside its own successor."""
        import os
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        self.json_group("gone", {"c": {"viewCount": 9}}, watch=["rowsUpdatedAt"])

        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, out, err = self.run_cli()

        self.assertIn("WATCH PATH MISSING", err, "reported on stdout, where CI does not look")
        self.assertIn("::warning", out + err, "no annotation, so nothing in the run summary")
        self.assertNotEqual(code, 0,
                            "a source that could not be checked at all exited 0")

    def test_watch_failures_do_not_trip_the_systemic_access_alarm(self):
        """>20% of fetches failing means the crawler cannot reach upstream. Watched-path
        misses are the opposite finding — every fetch succeeded — and mixing them makes the
        one alarm that says "stop, our access is broken" fire when access is fine."""
        self.json_group("gone", {"c": {"viewCount": 9}, "d": {"viewCount": 8}},
                        watch=["rowsUpdatedAt"])
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])

        code, out, err = self.run_cli()

        self.assertNotIn("SYSTEMIC", out + err,
                         "2 of 3 watched-path misses read as an access outage")

    def test_a_watch_that_is_a_bare_string_is_refused_before_anything_is_fetched(self):
        """`watch: rowsUpdatedAt` (scalar) is iterated CHARACTER BY CHARACTER, so the run
        reported `watched path 'r' is not present` — an authoring typo dressed up as
        "upstream changed shape", the most misleading thing this feature could say.

        Sibling of `_validated_volatile_patterns` and `_validated_index_headings`, and
        refused at the same moment for the same reason: after a 3,447-source crawl is the
        wrong time to learn a key was mistyped."""
        (self.root / "_meta" / "sources" / "ds.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "a"
                url: "https://example.gov/ds/a"
                format: json
                sha256: ""
                watch: rowsUpdatedAt
            """))
        fetched = []

        args = ["corpus-detect-changes", "--config", str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", lambda url: fetched.append(url) or b"{}"), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception) as e:
                changes.main()

        self.assertIn("watch", str(e.exception).lower())
        self.assertIn("a", str(e.exception), "the operator needs to know WHICH source")
        self.assertEqual(fetched, [], "the crawl started before the manifest was checked")

    def test_seeding_does_not_call_an_uncompared_source_a_failed_fetch_either(self):
        """The same mislabelling one level down. `_record_baselines` knows only "not in
        `fetched`", and printed that as `skipped (fetch failed)` — so an operator seeding
        baselines was told the network was the problem for a document that arrived intact.

        Skipping it is right: a hash it could not compute must never be written. Naming the
        reason wrong is not."""
        self.json_group("gone", {"c": {"viewCount": 9}}, watch=["rowsUpdatedAt"],
                        baseline="")

        code, out, err = self.run_cli("--record-baseline")

        self.assertNotIn("1 skipped (fetch failed)", out,
                         "a document that arrived was reported as a failed fetch")
        self.assertIn("not compared", out)

    def test_a_watched_source_is_not_counted_in_the_volatile_pattern_denominator(self):
        """A `watch` source never reaches the html/xml path, so no pattern touched it — but
        its bytes were added to `normalizable_bytes` anyway, which is the DENOMINATOR of the
        >10% breadth warning.

        That warning is the only thing standing between a corpus and a pattern that deletes
        content before hashing (corpus-toolkit#66). Padding the denominator with bytes no
        pattern processed switches it off silently: the wider the JSON body, the safer a
        dangerous pattern looks."""
        self.group("html", 1, baseline="current")
        # `format:` omitted, as a real Socrata entry has it — `_format_for` then calls it
        # html, the accounting block fires, and `content_hash` still takes the watch branch.
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1, "pad": "x" * 12000}},
                        watch=["rowsUpdatedAt"], declare_format=False)
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + "volatile_patterns:\n  - 'Rule text for source [0-9]+[.] '\n")

        code, out, err = self.run_cli()

        # Measured: the pattern strips 480 of the 1,210 bytes it actually processed — 39.7%,
        # far over VOLATILE_BREADTH_WARN. Padded with the JSON body's 12 KB it reported
        # `3.83% of 12544` and downgraded itself to a NOTE.
        self.assertIn("of 1 source(s)", out + err,
                      "a watched json source was counted as an HTML/XML source")
        self.assertIn("A pattern this wide deletes CONTENT", out + err,
                      "the breadth warning was switched off by bytes no pattern processed")

    def test_a_body_that_is_not_json_is_not_reported_as_a_missing_watch_path(self):
        """A 200-with-an-error-page and a watched field disappearing are DIFFERENT findings
        with different remedies, and every aggregate called both the second one.

        The per-source line had it right; the totals line, the summary and the CI annotation
        all said `a declared 'watch' path is absent from the fetched document` and sent the
        operator to check their path list. Naming a condition other than the one that
        occurred is what `_watched_digest`'s own comment says this codebase files bugs
        about — here it is one layer up, at the site an operator actually reads."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        self.bodies["https://example.gov/ds/a.json"] = b"<html>503 Service Unavailable</html>"

        code, out, err = self.run_cli()

        blame = out + err
        self.assertNotIn("watched path missing", blame.lower(),
                         "an error page served with a 200 was blamed on the watch list")
        self.assertIn("not parseable", blame.lower())
        self.assertNotEqual(code, 0)

    def test_the_group_breakdown_marks_a_group_that_compared_nothing(self):
        """`socrata 0/2` is byte-identical whether both sources were compared and found
        stable or neither was compared at all. The group line is the one that makes a bulk
        fault self-evident (corpus-toolkit#67), and it already carries `[N unseeded]` for
        exactly this class of caveat — the adjacent unchecked site."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        self.json_group("socrata", {"c": {"viewCount": 9}, "d": {"viewCount": 8}},
                        watch=["rowsUpdatedAt"])

        code, out, err = self.run_cli()

        self.assertIn("socrata 0/2 [2 not compared]", out,
                      f"a group where nothing was compared reads as stable:\n{out}")
        self.assertIn("ds 0/1,", out + ",", "an unaffected group grew a spurious marker")

    def test_a_watch_key_with_no_value_is_refused_rather_than_silently_ignored(self):
        """`watch:` with nothing under it — a mis-indented list, or one deleted a line at a
        time — parses to None, and the source reverted to hashing the whole document. The
        run then emitted exactly the `viewCount` false positives #72 exists to remove, from
        a manifest that VISIBLY DECLARES `watch`, with nothing said anywhere.

        One character away, `watch: []` is a hard load error. The same authoring accident
        must not get opposite treatment, and the silent branch is the wrong one to keep."""
        (self.root / "_meta" / "sources" / "ds.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "a"
                url: "https://example.gov/ds/a"
                format: json
                sha256: ""
                watch:
            """))
        self.bodies["https://example.gov/ds/a"] = json.dumps({"rowsUpdatedAt": 1}).encode()
        self.bodies["https://example.gov/ds/a.json"] = self.bodies["https://example.gov/ds/a"]

        args = ["corpus-detect-changes", "--config", str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", lambda url: self.bodies[url]), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception) as e:
                changes.main()

        self.assertIn("watch", str(e.exception).lower())


    def test_the_group_breakdown_marks_a_group_whose_fetches_all_failed_too(self):
        """THE ADJACENT SITE, and it predates `watch` entirely. A fetch failure skips the
        comparison exactly as a watched-path miss does, and the group line has always
        rendered `oar 0/2` for it — indistinguishable from a group that was fully compared
        and found stable.

        This is the shape corpus-toolkit#67 added the group line to expose: ERF's run had a
        DEQ group at 52/52 from a broken fetch. Fixing the marker for the new condition and
        not the old one would leave the line honest only about the case nobody has hit yet.
        """
        self.group("ok", 1, baseline="current")
        self.group("blocked", 2, baseline="current")
        for i in (0, 1):
            self.bodies[f"https://example.gov/blocked/{i}"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli()

        self.assertIn("blocked 0/2 [2 not compared]", out,
                      f"a group where every fetch failed reads as stable:\n{out}")


    def test_seeding_does_not_claim_a_reason_it_did_not_check(self):
        """`_record_baselines` knows only "no hash was computed" — its own comment says so
        and says the caller "must not claim one of the two reasons". The caller then listed
        two, and `WatchedDocumentUnreadable` is neither, so an error page served with a 200
        was reported as a failed fetch or a missing path while the run's own stderr said
        "not parseable json" three lines up. The same mislabelling one revision later."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"],
                        baseline="")
        self.bodies["https://example.gov/ds/a.json"] = b"<html>503</html>"

        code, out, err = self.run_cli("--record-baseline")

        self.assertNotIn("fetch failed, or a declared `watch` path was absent", out,
                         "the tally named two reasons and the actual one was a third")
        self.assertIn("1 skipped (not compared)", out)

    def test_a_volatile_pattern_measured_against_no_sources_still_reports(self):
        """`content_hash` now permits `volatile_patterns` + json on the grounds that "a
        pattern that matches nothing anywhere is already named in the drift report, per
        run". Excluding watch sources from `n_normalizable` can take that denominator to
        zero, and the report then `break`s out and prints NOTHING — so the justification the
        removal of the refusal rests on stops holding exactly when a corpus has no
        non-watch HTML sources left.

        Zero sources measured is itself the finding: the pattern is configured, and there
        was nothing for it to do."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"],
                        declare_format=False)
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + "volatile_patterns:\n  - 'sid=[0-9]+'\n")

        code, out, err = self.run_cli()

        self.assertIn("sid=", out + err,
                      "a configured pattern measured against zero sources said nothing")

    def test_a_bad_watch_in_an_out_of_scope_group_does_not_abort_the_run(self):
        """`--group` is "the per-cadence cron's knob". Validating every group up front made
        one group's typo abort every OTHER group's cron with an uncaught traceback, having
        printed nothing and fetched nothing.

        Fail-before-the-first-request is worth keeping; failing on a group this run was
        told not to look at is not."""
        self.group("oar", 1, baseline="current")
        (self.root / "_meta" / "sources" / "socrata.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "ds-1"
                url: "https://example.gov/socrata/1.json"
                sha256: ""
                watch: rowsUpdatedAt
            """))

        code, out, err = self.run_cli("--group", "oar")

        self.assertEqual(code, 0, f"an out-of-scope group's typo aborted the run:\n{err}")
        self.assertIn("oar 0/1", out)


    def test_not_compared_means_the_same_thing_on_every_line(self):
        """Three adjacent lines carried three definitions: the totals line counted watch
        misses only, the group breakdown counted fetch failures too, and the baseline tally
        used a third set. An operator read `1 not compared` and then counted 2 on the next
        line — and the only way to tell which was wrong was to read the source."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}, "b": {"viewCount": 9}},
                        watch=["rowsUpdatedAt"])
        self.group("html", 2, baseline="current")
        self.bodies["https://example.gov/html/1"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli()

        n_totals = int(self.totals_line(out).split(" not compared")[0].split(", ")[-1])
        n_groups = sum(int(part.split("[")[1].split(" ")[0])
                       for part in out.splitlines()
                       if part.startswith("drift by group")
                       for part in part.split("], ") if "not compared" in part)
        self.assertEqual(n_totals, n_groups,
                         f"the totals line and the group line disagree:\n{out}")
        self.assertEqual(n_totals, 2, "1 failed fetch + 1 watch miss = 2 not compared")

    def test_the_zero_source_note_does_not_blame_config_for_a_block(self):
        """`if not n_normalizable` fires whenever no HTML/XML source was successfully
        FETCHED — so a corpus whose HTML sources all 403'd was told its pattern is
        "configured and untested", which points at the manifest when the finding is that
        the crawler is being blocked. In scope and unreachable is not the same as never in
        scope."""
        self.group("html", 1, baseline="current")
        self.bodies["https://example.gov/html/0"] = OSError("HTTP Error 403")
        (self.root / "_meta" / "corpus.yml").write_text(
            CORPUS_YML + "volatile_patterns:\n  - 'sid=[0-9]+'\n")

        code, out, err = self.run_cli()

        self.assertIn("sid=", out + err, "the pattern was not mentioned at all")
        self.assertNotIn("configured and untested", out + err,
                         "a blocked fetch was reported as a configuration problem")
        self.assertIn("could not be fetched", (out + err).lower())


    def test_watch_on_a_source_declared_html_is_refused_at_the_door(self):
        """A `watch:` block pasted onto a `format: html` entry — routine in a mixed-format
        manifest — sailed through validation, and `content_hash` takes the watch branch
        BEFORE the format branch, so `json.loads` met HTML and the run reported
        `WATCH BODY UNREADABLE ... a fact about the response — an error page served with a
        200, or a block`. The exact opposite of what happened, forever, from a manifest that
        says on its face what the format is.

        The check is an ALLOWLIST — `json` or `geojson`, declared or from the url's
        extension — for the same reason the feature itself is one: enumerating the formats
        that are not json makes every format nobody thought of a silent acceptance."""
        (self.root / "_meta" / "sources" / "rules.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "rule-a"
                url: "https://example.gov/rules/a"
                format: html
                sha256: ""
                watch:
                  - rowsUpdatedAt
            """))
        self.bodies["https://example.gov/rules/a"] = _body(1)

        args = ["corpus-detect-changes", "--config", str(self.root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception) as e:
                changes.main()

        self.assertIn("rule-a", str(e.exception))
        self.assertIn("html", str(e.exception))
        self.assertNotIn("UNREADABLE", out.getvalue() + err.getvalue())

    def test_a_duplicated_id_still_counts_as_not_compared_everywhere(self):
        """The one combination where "ONE DEFINITION, used on every line that says it" was
        false: `_record_baselines` skips a duplicated id BEFORE the not-compared counter, so
        a source that was both duplicated and never compared was counted by the totals line
        and the group line and not by the tally."""
        (self.root / "_meta" / "sources" / "dup.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "d"
                url: "https://example.gov/dup/1"
                format: html
                sha256: ""
              - id: "d"
                url: "https://example.gov/dup/2"
                format: html
                sha256: ""
            """))
        for i in (1, 2):
            self.bodies[f"https://example.gov/dup/{i}"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("2 not compared", self.totals_line(out))
        self.assertIn("[2 not compared]", out)
        self.assertIn("2 skipped (not compared)", out,
                      f"the tally disagreed with the two lines above it:\n{out}")


    def test_watch_on_an_extension_derived_non_json_format_is_refused_too(self):
        """The refusal keyed on an EXPLICIT `format:`, but `_format_for` derives
        `pdf/xls/xlsx/docx/xml` from the url extension just as declaratively — only the
        UNRECOGNISED extension falls back to html, and that fallback is the one case the
        rationale needs to protect (a Socrata `.json` url with no `format:`).

        So `watch:` on a `.xml` or `.pdf` url with no `format:` sailed through and produced
        exactly what the refusal exists to stop: `WATCH BODY UNREADABLE`, blaming the
        response for a fact about the declaration, on every run, forever.

        `.html` and `.csv` are here because the FIRST fix missed them: `_format_for` returns
        its `html` fallback for every extension it does not recognise, so exempting that
        fallback exempted real HTML pages along with the Socrata `.json` urls it was meant
        to protect. `.xml` and `.pdf` were refused and `.html` was not, with no `format:`
        declared in either case."""
        for ext in ("xml", "pdf", "html", "csv", "aspx"):
            with self.subTest(ext=ext):
                (self.root / "_meta" / "sources" / f"{ext}.yml").write_text(textwrap.dedent(f"""\
                    sources:
                      - id: "{ext}src"
                        url: "https://example.gov/{ext}/doc.{ext}"
                        sha256: ""
                        watch:
                          - rowsUpdatedAt
                    """))
                args = ["corpus-detect-changes", "--config",
                        str(self.root / "_meta" / "corpus.yml")]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", args), \
                     mock.patch.object(changes, "fetch", self._fetch), \
                     redirect_stdout(out), redirect_stderr(err):
                    with self.assertRaises(Exception) as e:
                        changes.main()
                self.assertIn(f"{ext}src", str(e.exception))
                (self.root / "_meta" / "sources" / f"{ext}.yml").unlink()

    def test_a_json_format_spelled_differently_is_not_refused(self):
        """`format: JSON` works identically today, and `geojson` is json too. The refusal
        must name the formats a watch list genuinely cannot read, not everything that is not
        the literal string `json`."""
        self.json_group("ds", {"a": {"rowsUpdatedAt": 1}}, watch=["rowsUpdatedAt"])
        text = (self.root / "_meta" / "sources" / "ds.yml").read_text()
        (self.root / "_meta" / "sources" / "ds.yml").write_text(
            text.replace("format: json", "format: JSON"))

        code, out, err = self.run_cli()

        self.assertEqual(code, 0, f"a json source was refused for its spelling:\n{err}")

    def test_a_duplicate_id_where_one_entry_fetched_still_agrees(self):
        """`fetched` is keyed `(group, id)`, so a SUCCESSFUL sibling populated the key and
        the failing entry looked fetched to the tally. Copying an entry and editing the url
        while forgetting the id is exactly how duplicates arise, so this is the common
        shape, not an exotic one."""
        (self.root / "_meta" / "sources" / "dup.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "d"
                url: "https://example.gov/dup/1"
                format: html
                sha256: ""
              - id: "d"
                url: "https://example.gov/dup/2"
                format: html
                sha256: ""
            """))
        self.bodies["https://example.gov/dup/1"] = _body(7)
        self.bodies["https://example.gov/dup/2"] = OSError("HTTP Error 403")

        code, out, err = self.run_cli("--record-baseline")

        self.assertIn("1 not compared", self.totals_line(out))
        self.assertIn("[1 not compared]", out)
        self.assertIn("1 skipped (not compared)", out,
                      f"the tally disagreed with the two lines above it:\n{out}")


if __name__ == "__main__":
    unittest.main()
