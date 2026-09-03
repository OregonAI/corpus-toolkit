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

ADR 0015: `corpus-detect-changes` files no issues any more, so every test of the
issue-filing/label/cap/budget machinery (`OpenIssueReturnsOutcomeTest`, `EnsureLabelTest`,
`CapTest`, `CappedRunIsNotACleanRunTest`, `BudgetIsSpentSmallestGroupFirstTest`,
`GroupFindingIssueTest`, the `inert`-run refusal, and the `_completed()` helper they alone
used) was retired rather than adapted — there is nothing left in `changes.py` for them to
call. What replaces the tickets is `DRIFT.md`/`drift-state.json`, asserted on directly below.
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
        # Ids whose `gh issue create` will fail. Default empty: filings succeed. The #53
        # shape is a filing that is ATTEMPTED and does not exist afterwards, and no test
        # could express it while the mock returned True unconditionally.
        self.failing_ids: set[str] = set()
        # Same idea for the group findings of ADR 0010: a finding that was ATTEMPTED and
        # does not exist afterwards is a different outcome from one that was never due.
        self.failing_findings: set[str] = set()
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
        # Since ADR 0015 the run files nothing, so there is nothing to fake but the network.
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             redirect_stdout(out), redirect_stderr(err):
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


class ChangedSourcesTsvIsPublicSurfaceTest(_DriftRun):
    """`changed-sources.tsv` is read by corpus repos, and no test read it — so the four
    columns and their order were a claim in a comment. #69 reshaped the record behind this
    writer (a group name rides along now), which is exactly the change that would rewrite
    the file by accident."""

    def test_four_columns_id_url_old_new_in_manifest_order(self):
        self.group("aaa", 3, baseline="stale")
        self.group("zzz", 1, baseline="stale")
        self.run_cli()
        rows = [ln.split("\t") for ln in
                (self.root / "changed-sources.tsv").read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["aaa-0", "aaa-1", "aaa-2", "zzz-0"],
                         "manifest order")
        self.assertTrue(all(len(r) == 4 for r in rows), rows)
        self.assertEqual(rows[0][1], "https://example.gov/aaa/0")
        self.assertEqual(rows[0][2], "stale")
        self.assertEqual(rows[0][3], content_hash(_body(0), "html"))


class SourceOutcomesArtifactTest(_DriftRun):
    """corpus-toolkit#160: `changed-sources.tsv` lists only what changed, so a source that
    was fetched and held still, a source whose fetch failed, and a source never in this
    run's scope are byte-identical in that file — none of them appears. `source-outcomes.json`
    is the companion artifact that records, per source, WHAT HAPPENED, plus the run-level
    facts (scope, per-group breakdown, totals) that otherwise exist only on stdout.

    THE RED PROOF the issue demands: a run where every fetch failed must be distinguishable,
    by reading the artifact alone, from a run where nothing changed. Both write an EMPTY
    `changed-sources.tsv` today — that collapse is the bug this file exists to fix.
    """

    def _outcomes(self):
        return json.loads((self.root / "source-outcomes.json").read_text())

    def test_a_wholly_failed_run_is_distinguishable_from_a_no_drift_run(self):
        # Scenario A: every fetch in the group fails.
        self.group("oar", 3, baseline="stale")
        for i in range(3):
            self.bodies[f"https://example.gov/oar/{i}"] = OSError("HTTP Error 403")
        self.run_cli()
        failed_tsv = (self.root / "changed-sources.tsv").read_bytes()
        failed_report = self._outcomes()

        # Scenario B: a fresh corpus where every fetch succeeds and nothing changed.
        other_root = Path(tempfile.mkdtemp())
        (other_root / "_meta" / "sources").mkdir(parents=True)
        (other_root / "documents").mkdir()
        (other_root / "_meta" / "corpus.yml").write_text(CORPUS_YML)
        for i in range(3):
            url = f"https://example.gov/oar/{i}"
            self.bodies[url] = _body(i)
        (other_root / "_meta" / "sources" / "oar.yml").write_text(
            "\n".join(["sources:"] + [textwrap.dedent(f"""\
                  - id: "oar-{i}"
                    url: "https://example.gov/oar/{i}"
                    format: html
                    sha256: "{content_hash(_body(i), 'html')}"
            """).rstrip() for i in range(3)]) + "\n")
        args = ["corpus-detect-changes", "--config", str(other_root / "_meta" / "corpus.yml")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(changes, "fetch", self._fetch), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                changes.main()
            except SystemExit:
                pass
        clean_tsv = (other_root / "changed-sources.tsv").read_bytes()
        clean_report = json.loads((other_root / "source-outcomes.json").read_text())
        shutil.rmtree(other_root, ignore_errors=True)

        # The existing artifact collapses the two runs today — proving the bug is real,
        # not merely asserting the fix.
        self.assertEqual(failed_tsv, b"", "sanity: the failed run's tsv is empty")
        self.assertEqual(clean_tsv, b"", "sanity: the clean run's tsv is empty")
        self.assertEqual(failed_tsv, clean_tsv,
                         "sanity: changed-sources.tsv alone cannot tell these apart")

        # The companion artifact must not repeat that collapse.
        self.assertNotEqual(failed_report, clean_report,
                            "a wholly-failed run and a no-drift run produced "
                            "indistinguishable source-outcomes.json artifacts")
        self.assertEqual(failed_report["totals"]["fetch_failed"], 3)
        self.assertEqual(failed_report["totals"]["unchanged"], 0)
        self.assertEqual(clean_report["totals"]["fetch_failed"], 0)
        self.assertEqual(clean_report["totals"]["unchanged"], 3)


class ChangedSourcesTsvByteIdenticalGuaranteeTest(_DriftRun):
    """The out-of-scope acceptance criterion, proven rather than reasoned about: for the
    same inputs, `changed-sources.tsv` is byte-identical to what it was BEFORE this
    artifact existed. `tests/fixtures/changed-sources-golden.tsv` was captured by running
    this exact scenario against the pre-#160 code, on this branch, before a single line of
    `source-outcomes.json` support was written — a snapshot, not a value re-derived from
    the current implementation, so a change to the writer would actually be caught here.

    REGENERATING IT IS NOT AN ORDINARY EDIT. Its worth is entirely that no current code
    produced it; hand-editing it to match a new writer converts the proof into a
    restatement of whatever the writer now does. If the tsv format legitimately changes,
    that is a breaking change to public surface (AGENTS.md), and the honest move is a NEW
    fixture captured the same way — check the commit that last changed the format out into
    a scratch directory, run this scenario against it, and commit the bytes it wrote
    alongside this one, keeping the old file as the record of the old contract."""

    def test_tsv_bytes_are_unchanged_by_the_new_artifact(self):
        self.group("aaa", 3, baseline="stale")
        self.group("zzz", 1, baseline="stale")
        self.group("bbb", 2, baseline="current")
        self.group("ccc", 2, baseline=None)
        self.group("ddd", 1, baseline="stale")
        self.bodies["https://example.gov/ddd/0"] = Exception("boom")
        self.run_cli()
        golden = (Path(__file__).parent / "fixtures" / "changed-sources-golden.tsv").read_bytes()
        actual = (self.root / "changed-sources.tsv").read_bytes()
        self.assertEqual(actual, golden)


class SourceOutcomesVocabularyTest(_DriftRun):
    """The outcome vocabulary itself: each branch the fetch loop already takes must land
    on its own, distinct outcome string — collapsing any two recreates the bug one level
    in (the issue's own words)."""

    def _by_id(self, report, sid):
        return next(s for s in report["sources"] if s["id"] == sid)

    def test_no_baseline_is_reported_as_such_not_changed_nor_unchanged(self):
        # Fetch succeeds; the manifest recorded no sha256 at all.
        self.group("counties", 1, baseline=None)
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        entry = self._by_id(report, "counties-0")
        self.assertEqual(entry["outcome"], "no_baseline")
        self.assertFalse(entry["had_baseline"])
        # And it must NOT also appear as changed or unchanged.
        self.assertNotIn(entry["outcome"], ("changed", "unchanged"))

    def test_unreadable_json_and_watch_path_missing_are_distinct_outcomes(self):
        # A watch-declared json source, hand-written so both watch exceptions are reachable.
        (self.root / "_meta" / "sources" / "ds.yml").write_text(textwrap.dedent("""\
            sources:
              - id: "ds-bad-json"
                url: "https://example.gov/ds/bad"
                format: json
                watch: ["rowsUpdatedAt"]
                sha256: "stale"
              - id: "ds-missing-path"
                url: "https://example.gov/ds/missing"
                format: json
                watch: ["rowsUpdatedAt"]
                sha256: "stale"
        """))
        self.bodies["https://example.gov/ds/bad"] = b"<html>not json</html>"
        self.bodies["https://example.gov/ds/missing"] = json.dumps({"other": 1}).encode()
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(self._by_id(report, "ds-bad-json")["outcome"], "unreadable_json")
        self.assertEqual(self._by_id(report, "ds-missing-path")["outcome"],
                         "watch_path_missing")

    def test_fetch_failed_changed_and_unchanged_are_reported(self):
        self.group("oar", 1, baseline="stale")     # will change
        self.group("oam", 1, baseline="current")    # will not change
        self.group("deq", 1, baseline="stale")
        self.bodies["https://example.gov/deq/0"] = OSError("HTTP Error 403")
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(self._by_id(report, "oar-0")["outcome"], "changed")
        self.assertEqual(self._by_id(report, "oam-0")["outcome"], "unchanged")
        self.assertEqual(self._by_id(report, "deq-0")["outcome"], "fetch_failed")


class SourceOutcomesRunLevelFactsTest(_DriftRun):
    """The run-level facts the issue says exist only on stdout today: in-scope groups, the
    per-group breakdown, and totals. `source-outcomes.json` must carry all three, counted
    ONE way -- and the printed pair must be recoverable from them exactly, since the log
    counts an unseeded source as changed and the artifact does not."""

    def test_groups_in_scope_and_breakdown_match_what_a_full_run_touches(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 1, baseline="current")
        _, out, _ = self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(sorted(report["groups_in_scope"]), ["oam", "oar"])
        self.assertEqual(report["groups"]["oar"]["changed"], 2)
        self.assertEqual(report["groups"]["oar"]["checked"], 2)
        self.assertEqual(report["groups"]["oam"]["changed"], 0)
        self.assertIn("oar 2/2", out, "the artifact's own numbers must match the log's")

    def test_totals_are_readable_without_parsing_the_log(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 1, baseline="current")
        self.group("counties", 1, baseline=None)
        self.run_cli()
        report = json.loads((self.root / "source-outcomes.json").read_text())
        t = report["totals"]
        self.assertEqual(t["total"], 4)
        self.assertEqual(t["changed"], 2)
        self.assertEqual(t["unchanged"], 1)
        self.assertEqual(t["no_baseline"], 1)
        self.assertEqual(t["fetch_failed"], 0)

    def test_the_printed_pair_is_recoverable_from_the_artifact_exactly(self):
        """The log and the artifact count `changed` differently ON PURPOSE, and the
        difference is exactly the unseeded sources: `_print_group_breakdown` counts one as
        changed (`new != old` with `old == ""`), the artifact calls it `no_baseline`.

        The first version of this artifact shipped the log's dict by reference and so held
        both readings of the word `changed` at once — 2 by one path and 4 by the other, in
        one file. Deriving the artifact's own numbers fixes that and creates this
        obligation: the printed pair must still be reconstructible, or a fact the criterion
        asked for has been lost rather than corrected.
        """
        self.group("oar", 2, baseline="stale")
        self.group("counties", 2, baseline=None)
        _, out, _ = self.run_cli()

        report = json.loads((self.root / "source-outcomes.json").read_text())

        # THE HALF THAT CATCHES THE CONFLATION. Reconstructing the printed pair cannot: the
        # log adds the two together, so a `groups` dict that folded `no_baseline` INTO
        # `changed` reconstructs it perfectly and reads as correct. Only asking the artifact
        # to hold them apart does, which is the whole point of counting them separately.
        self.assertEqual(report["groups"]["counties"]["no_baseline"], 2)
        self.assertEqual(report["groups"]["counties"]["changed"], 0,
                         "an unseeded source was never compared, so it did not change")

        for group, stats in report["groups"].items():
            printed = stats["changed"] + stats["no_baseline"]
            self.assertIn(f"{group} {printed}/{stats['checked']}", out,
                          "changed + no_baseline must reconstruct the printed pair")

    def test_every_in_scope_source_is_counted_exactly_once(self):
        """`total` comes from the in-scope count and the six outcomes come from the
        per-source records; nothing asserted they agree, so a future `continue` that
        appended no outcome would quietly drop a source from an artifact whose entire
        purpose is that no source is silently missing."""
        self.group("oar", 2, baseline="stale")
        self.group("oam", 1, baseline="current")
        self.group("counties", 1, baseline=None)
        self.group("deq", 1, baseline="stale")
        self.bodies["https://example.gov/deq/0"] = OSError("HTTP Error 403")
        self.run_cli()

        t = json.loads((self.root / "source-outcomes.json").read_text())["totals"]
        self.assertEqual(sum(t[o] for o in changes.OUTCOMES), t["total"])

    def test_a_source_that_appended_no_outcome_raises_rather_than_vanishing(self):
        """The guard behind `test_every_in_scope_source_is_counted_exactly_once`, driven at
        the builder directly because the fetch loop currently has no branch that skips an
        append -- which is exactly the state this must survive someone changing."""
        with self.assertRaises(RuntimeError) as e:
            changes._source_outcomes_report(
                [changes.SourceOutcome("oar", "oar-0", "https://example.gov/oar/0",
                                       "unchanged", True)],
                n_total=2, group_filter=None)
        self.assertIn("appended no outcome", str(e.exception))

    def test_an_outcome_outside_the_vocabulary_raises_rather_than_inventing_a_key(self):
        """`totals[o.outcome] = totals.get(o.outcome, 0) + 1` would have invented a key for
        a typo, leaving the real outcome reading a confident zero."""
        with self.assertRaises(RuntimeError) as e:
            changes._source_outcomes_report(
                [changes.SourceOutcome("oar", "oar-0", "https://example.gov/oar/0",
                                       "chagned", True)],
                n_total=1, group_filter=None)
        self.assertIn("unknown outcome", str(e.exception))

    def test_group_filter_narrows_scope_and_the_excluded_group_is_simply_absent(self):
        self.group("oar", 2, baseline="stale")
        self.group("oam", 2, baseline="stale")
        self.run_cli("--group", "oar")
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(report["group_filter"], ["oar"])
        self.assertEqual(report["groups_in_scope"], ["oar"])
        self.assertNotIn("oam", report["groups"])
        self.assertFalse(any(s["group"] == "oam" for s in report["sources"]),
                         "a group filtered out by --group must not appear as any outcome, "
                         "including as if it were unchanged")

    def test_a_wholly_failed_run_still_writes_the_artifact(self):
        # The run this ticket exists for: every fetch fails, and the artifact must still
        # exist and describe the run rather than being skipped as "nothing to report".
        self.group("oar", 3, baseline="stale")
        for i in range(3):
            self.bodies[f"https://example.gov/oar/{i}"] = OSError("HTTP Error 403")
        self.run_cli()
        self.assertTrue((self.root / "source-outcomes.json").exists())
        report = json.loads((self.root / "source-outcomes.json").read_text())
        self.assertEqual(report["totals"]["fetch_failed"], 3)
        self.assertEqual(report["totals"]["total"], 3)


class DriftStateArtifactTest(_DriftRun):
    """ADR 0015: `drift-state.json` is the rolling state `DRIFT.md` renders from, written
    on every run that reaches the fetch loop, beside `source-outcomes.json`."""

    def test_a_clean_run_writes_one_record_per_source_and_no_red_reasons(self):
        self.group("oar", 2, baseline="current")
        self.group("oam", 1, baseline="current")
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        state = json.loads((self.root / "drift-state.json").read_text())
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(sorted(r["id"] for r in state["sources"]),
                         ["oam-0", "oar-0", "oar-1"])
        self.assertEqual(state["last_run"]["red_reasons"], [])


class AnUnseededSourceIsNotAChangedSourceTest(_DriftRun):
    """corpus-toolkit#145 / ADR 0015: the per-source half of ADR 0010's rule, now expressed
    as auto-seeding rather than a refusal.

    "An uncompared source is not a changed source" was enforced for the group drift finding
    and not for the individual tickets. A source with `sha256: ''` compares unequal to
    everything, so it used to file `Source changed: <id>` with an EMPTY previous hash every
    run — could-not-check reported as a finding. Since ADR 0015 there is no ticket to file
    at all: the run that first fetches an unseeded source records what it fetched, says so
    on stderr and in DRIFT.md, and compares from the next run on.
    """

    def test_the_tsv_still_carries_the_unseeded_source_with_an_empty_old_column(self):
        """The ticket is what went away, not the record. `changed-sources.tsv` is read by
        corpus repos and its four columns are public surface (corpus-toolkit#53), and an
        empty `old` column is self-describing where a ticket body was not."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 1, baseline="stale")
        self.run_cli()
        rows = [r.split("\t") for r in
                (self.root / "changed-sources.tsv").read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["counties-0", "oar-0"])
        self.assertEqual(rows[0][2], "", "the unseeded row lost its empty `old` column")

    def test_seeding_is_not_itself_the_unchecked_condition(self):
        """Every run seeds automatically since ADR 0015; `--record-baseline` (bare) is a
        synonym for that default. A run that seeds must not be red for having done so."""
        self.group("counties", 2, baseline=None)
        code, out, err = self.run_cli("--record-baseline")
        self.assertEqual(code, 0, f"the seeding run reported failure:\n{err}")

    def test_an_unseeded_source_does_not_set_changed_true_for_the_workflow(self):
        """`changed=true` fires whatever the calling workflow does next. An unseeded
        source is not drift, so it must not fire it."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="current")
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        self.assertIn("changed=false", gh.read_text())
        self.assertIn("unseeded=1", gh.read_text())

    def test_a_real_drift_run_still_sets_changed_true(self):
        self.group("oar", 1, baseline="stale")
        gh = self.root / "gh-output"
        self.run_cli("--github-output", str(gh))
        self.assertIn("changed=true", gh.read_text())

    def test_a_wholly_unseeded_manifest_seeds_itself_and_exits_clean(self):
        """ADR 0015: a manifest nobody has ever seeded is no longer a refusal. The run
        that first fetches these sources records what it fetched, names them on stderr and
        in DRIFT.md, and exits 0 — the corpus-toolkit#68 shape (oregon-counties, 3,447
        sources, all `sha256: ''`) is now self-healing on its first run rather than
        permanently red."""
        self.group("counties", 4, baseline=None)
        path = self.root / "_meta" / "sources" / "counties.yml"
        gh = self.root / "gh-output"
        code, out, err = self.run_cli("--github-output", str(gh))
        self.assertEqual(code, 0, f"a seeding run reported failure:\n{err}")
        manifest_text = path.read_text()
        for i in range(4):
            self.assertIn(content_hash(_body(i), "html"), manifest_text,
                          f"counties-{i}'s fetched hash was never written to the manifest")
        self.assertIn("4 source(s) had no recorded baseline and were SEEDED this run", err)
        gh_text = gh.read_text()
        self.assertIn("changed=false", gh_text)
        self.assertIn("unseeded=4", gh_text)
        self.assertIn("seeded=4", gh_text)
        self.assertIn("## Seeded this run (4)", (self.root / "DRIFT.md").read_text())

    def test_a_partly_seeded_manifest_names_the_seeded_source_and_still_compares_the_rest(self):
        """One unseeded source among genuinely drifted ones must not switch reporting off
        for the corpus — the seeded source is named, and the drifted ones still compare."""
        self.group("counties", 1, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertIn("1 source(s) had no recorded baseline and were SEEDED this run", err)
        self.assertIn("counties-0", err)
        self.assertIn("2 changed", out, "the genuinely drifted sources still compared")

    def test_a_second_run_after_seeding_reports_zero_changed_and_no_seeded_line(self):
        """Once seeded, a source compares like any other — no more SEEDED line, and
        nothing left to name."""
        self.group("counties", 2, baseline=None)
        self.run_cli()
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertIn("0 changed", out)
        self.assertIn("0 with no recorded baseline", out)
        self.assertNotIn("SEEDED this run", err)


class NothingCheckedIsNotCleanTest(_DriftRun):
    def test_an_empty_group_scope_does_not_exit_zero(self):
        # `--group` takes a free-text name and nothing validates it against the manifest,
        # so a typo checked nothing and reported "0 changed ... of 0 checked", exit 0 —
        # could-not-check served as not-there, inside the run that exists to prevent it.
        self.group("oar", 2, baseline="current")
        code, out, err = self.run_cli("--group", "nosuchgroup")
        self.assertEqual(code, 1)
        self.assertIn("NOTHING WAS CHECKED", err)
        self.assertIn("nosuchgroup", err)


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


class GroupDriftFindingTest(_DriftRun):
    """ADR 0010: a group where EVERY COMPARED source changed gets a line in the run's own
    output and a row in DRIFT.md's "Groups whose every compared source changed" section —
    no ticket, since ADR 0015. ERF run 31022774644 is the case it exists for: `oar` was 484
    of 484 changed sources, 89% of the drift and a single template-level cause, and the old
    issue budget — spent smallest-group-first — reached it last and filed nothing for it.
    """

    def _findings(self, out):
        """The groups named in the "N group(s) where EVERY compared source changed" line,
        in the order the run printed them — or [] if the line is absent, since that line
        is only printed `if findings:`."""
        for line in out.splitlines():
            if "group(s) where EVERY compared source changed" in line:
                body = line.split(": ", 1)[1].split(". Reported in DRIFT.md")[0]
                return [part.split(" (")[0] for part in body.split(", ")]
        return []

    def test_a_group_where_every_compared_source_changed_gets_a_finding(self):
        self.group("oar", 4, baseline="stale")
        self.group("oam", 3, baseline="current")
        code, out, err = self.run_cli()
        self.assertEqual(self._findings(out), ["oar"],
                         "the whole-group drift got no finding of its own")
        self.assertIn("oar (4 of 4)", out)
        drift_md = (self.root / "DRIFT.md").read_text()
        self.assertIn("## Groups whose every compared source changed", drift_md)
        self.assertIn("| oar |", drift_md, "the group never reached the DRIFT.md table")

    def test_the_finding_does_not_replace_the_per_source_report(self):
        """ADR 0010: a finding says the group changed together and asserts nothing about
        why. It is not entitled to speak FOR a source that changed for its own reasons, so
        the per-source record — `changed-sources.tsv` — must survive alongside it exactly
        as it would without the finding."""
        self.group("oar", 4, baseline="stale")
        code, out, err = self.run_cli()
        self.assertEqual(self._findings(out), ["oar"])
        rows = [r.split("\t") for r in
                (self.root / "changed-sources.tsv").read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["oar-0", "oar-1", "oar-2", "oar-3"],
                         "the group finding suppressed the individual record")

    def test_a_group_holding_one_compared_source_gets_no_finding(self):
        """ADR 0010: more than one compared source. One source cannot corroborate itself
        — so the finding would say the same thing twice. Three ERF groups hold exactly one
        source (`constitution`, `external`, `public-utility-commission-policies`)."""
        self.group("constitution", 1, baseline="stale")
        self.group("oar", 3, baseline="stale")
        code, out, err = self.run_cli()
        self.assertEqual(self._findings(out), ["oar"],
                         "a group of one filed a finding that restates its own record")

    def test_a_group_that_was_never_compared_gets_no_finding(self):
        """THE REQUIRED CASE, and the one that decided ADR 0010's shape. oregon-counties
        reported 3,447 of 3,447 sources changed (corpus-toolkit#68) because every baseline
        was empty: nothing was ever compared, and a finding there would have diagnosed a
        seeding run as drift.

        The manifest is only partly unseeded, so this is not a wholly-unseeded run — that
        case is covered separately and cannot be what makes this pass."""
        self.group("counties", 3, baseline=None)
        self.group("oar", 2, baseline="stale")
        code, out, err = self.run_cli()
        self.assertEqual(self._findings(out), ["oar"],
                         "an unseeded group reported drift that never happened")

    def test_a_fetch_failure_is_not_a_compared_source_either(self):
        """The other half of "an uncompared source is not a changed source" (ADR 0010), and
        the one ERF actually hit: the DEQ group read 52/52 off a broken fetch.

        Two of the four were never compared, so the finding is about the two that were —
        and it must say `2 of 2`, not `2 of 4`, which would report the group as 50% drifted
        and contradict the rule that fired it."""
        self.group("blocked", 4, baseline="stale")
        for i in (0, 1):
            self.bodies[f"https://example.gov/blocked/{i}"] = OSError("HTTP Error 403")
        code, out, err = self.run_cli()
        self.assertEqual(self._findings(out), ["blocked"],
                         "every source that was compared changed, and nothing was said")
        self.assertIn("blocked (2 of 2)", out,
                      "the finding counted sources it never compared — or lost track of "
                      "how many were in the group at all")

    def test_a_source_that_is_both_unseeded_and_unfetchable_is_subtracted_once(self):
        """`unseeded` is counted before the fetch and `uncompared` after it, so ONE source
        that has no baseline and then fails to fetch increments both. Deriving the compared
        count as `total - unseeded - uncompared` removes it twice — here that reads 1
        compared source where there are 2, and the finding silently disappears under the
        "more than one compared source" rule. The count is measured at the comparison
        instead, so this shape cannot arise."""
        (self.root / "_meta" / "sources" / "mixed.yml").write_text(textwrap.dedent("""\
            group: mixed
            sources:
              - id: "mixed-gone"
                url: "https://example.gov/mixed/gone"
                format: html
                sha256: ""
              - id: "mixed-a"
                url: "https://example.gov/mixed/a"
                format: html
                sha256: "stale"
              - id: "mixed-b"
                url: "https://example.gov/mixed/b"
                format: html
                sha256: "stale"
            """))
        self.bodies["https://example.gov/mixed/gone"] = OSError("HTTP Error 403")
        self.bodies["https://example.gov/mixed/a"] = _body(1)
        self.bodies["https://example.gov/mixed/b"] = _body(2)

        code, out, err = self.run_cli()

        self.assertEqual(self._findings(out), ["mixed"],
                         f"the finding vanished: the group reads as 1 compared source, "
                         f"below the more-than-one rule:\n{out}")
        self.assertIn("mixed (2 of 2)", out,
                      f"the both-markers source was subtracted twice:\n{out}")

    def test_one_compared_source_holding_still_is_enough_to_withhold_the_finding(self):
        """ADR 0010 rejected ">80%" on principle, not preference: 100% is the only
        threshold that is itself an observation, and the sources that did NOT change are
        evidence against the very pattern the finding would assert. Nine of ten is nine of
        ten, and the tenth says the ten did not move together.

        Sized so a share rule and the observation rule disagree: 9 of 10 is 90%."""
        self.group("oar", 9, baseline="stale")
        self.group("oar", 1, baseline="current", start=9, file_stem="oar-stable")
        code, out, err = self.run_cli()
        self.assertIn("oar 9/10", out, "the fixture is not 90% drift")
        self.assertEqual(self._findings(out), [],
                         "a group with a source that held still still spoke for it")

    def test_a_group_whose_every_fetch_failed_gets_no_finding(self):
        """The adjacent shape to the unseeded one, and the reason the rule is stated over
        COMPARED sources rather than over the group. A group where nothing was fetched has
        no changed source and no compared source; "every compared source changed" is
        vacuously true of it, and a finding would report drift on a group the run never
        looked at."""
        self.group("blocked", 2, baseline="stale")
        self.group("oar", 2, baseline="stale")
        for i in (0, 1):
            self.bodies[f"https://example.gov/blocked/{i}"] = OSError("HTTP Error 403")
        code, out, err = self.run_cli()
        self.assertIn("blocked 0/2 [2 not compared]", out, "the fixture compared nothing")
        self.assertEqual(self._findings(out), ["oar"],
                         "a group nothing was fetched from reported whole-group drift")

    def test_the_largest_drifting_group_is_named_first(self):
        """`_group_drift_findings` sorts largest first, then by group name — a choice ADR
        0010 leaves open among the findings themselves, orthogonal to which groups get one
        at all. Named to sort LAST alphabetically, so alphabetical order and drift-size
        order disagree; a fixture where they coincide cannot tell which one the run used."""
        self.group("zbig", 5, baseline="stale")
        self.group("asmall", 2, baseline="stale")
        self.group("bmid", 3, baseline="stale")
        code, out, err = self.run_cli()
        self.assertEqual(self._findings(out), ["zbig", "bmid", "asmall"],
                         "findings were not ordered largest-first, ties by name")

    def test_the_run_names_the_groups_a_finding_fired_for(self):
        """corpus-toolkit#53's own lesson, one level up: a claim in the tracker that the
        run's own log never mentioned was read as reported when it was not. The finding
        line is now the run's own log, so it has to name the group."""
        self.group("oar", 3, baseline="stale")
        code, out, err = self.run_cli()
        self.assertIn("1 group(s) where EVERY compared source changed: oar (3 of 3)", out,
                      f"the run found whole-group drift and did not say so:\n{out}")


if __name__ == "__main__":
    unittest.main()
