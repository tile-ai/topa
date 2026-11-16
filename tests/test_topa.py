from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from topa import config, gate, gpu, history, ledger, pr, review, setup_cmd, states, wiki
from topa.container import foreign_files
from topa.hook import decide
from topa.round import (
    CLEAR_CLAUDE_ENV,
    KINDS,
    _entry,
    _fetch_main,
    _issue_work,
    _manifest_ops,
    brief,
    drop_branch,
    entry_field,
    foundry_text,
    inventory,
    kit,
    manifest_entry,
    pr_comments,
    remove_worktrees_under,
    render,
    round_slug,
    start,
    transition,
)
from topa.round import status as round_status

HIR = 'class Operator:\n    def kernel(self, x):\n        return tf.schedule((x,), op=T.cuda.sm90.Wgmma(n=16))\n'
PAGE = Path(__file__).parent / "fixtures" / "Perf-Tracker-2026-11-06.md"
PAGE_URL = "https://github.com/tile-ai/TileOPs/wiki/Perf-Tracker-2026-11-06"


def defaults(**changes) -> config.Config:
    values = {name: config._coerce(name, value) for name, value in config.DEFAULTS.items()}
    return config.Config(**{**values, "reviewer": "review-agent", **changes})


def page(text: str | None = None) -> wiki.Page:
    return wiki.parse(PAGE.read_text() if text is None else text, "2026-11-06", PAGE_URL)


def filled(name: str, result: str = "bar met", kind: str = "perf") -> str:
    """A kit PR file with every placeholder filled, as a round fills it."""
    text = pr_comments(kind) if name == "pr-comments.md" else (kit() / kind / name).read_text()
    title = {"perf": "[Perf][foundry][Test] Schedule the kernel", "new-op": "[Feat][foundry][Test] Add AFwdOp",
             "extend": "[Feat][foundry][Test] Serve the zero-centered weight",
             "general": "[BugFix][Norm] Keep the row mean"}[kind]
    text = re.sub(r"^# \[.*", f"# {title}", text)
    text = re.sub(r"<work/final_hir\.py[^\n]*>", HIR.rstrip("\n"), text)
    text = text.replace("sha256:<image digest>", "sha256:" + "a" * 64)
    text = text.replace("**<bar met | below bar>**", f"**{result}**").replace("<bar met | below bar>", result)
    text = text.replace("Op: <Op>", "Op: AFwdOp").replace("#<N>", "#7").replace("<benchmark module>", "benchmarks.ops.bench_a")
    text = re.sub(r"^<[^\n]*>$", "1", text, flags=re.MULTILINE)
    return gate.PLACEHOLDER.sub("1", text)


MANIFEST_PATH = "src/tileops/manifest/spec/a.yaml"
MANIFEST = "AFwdOp:\n  family: test\n  status: {a}\n\nBFwdOp:\n  family: test\n  status: {b}\n"
ROPE_ENTRY = ["  family: rope", "  status: spec-only", "  workloads:",
              "  - {B: 4, dtype_cases: [{T: float16}, {T: bfloat16}], label: llama3-8b-prefill}"]


def issue(number: int, title: str, labels: list[str], body: str = "b", assignees: list[str] = (),
          state: str = "OPEN") -> dict:
    return {"number": number, "title": title, "body": body, "state": state, "labels": [{"name": n} for n in labels],
            "assignees": [{"login": login} for login in assignees],
            "url": f"https://github.com/tile-ai/TileOPs/issues/{number}"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class ConfigTests(unittest.TestCase):
    def test_every_setting_is_documented(self) -> None:
        usage = (Path(__file__).parents[1] / "docs" / "usage.md").read_text()
        rows = re.findall(r"^\| (`[^|]+`) \|", usage, re.MULTILINE)
        documented = {name for row in rows for name in re.findall(r"`([a-z_]+)`", row)}
        self.assertEqual(sorted(set(config.DEFAULTS) ^ documented), [])

    def test_first_run_writes_a_config_that_loads_then_asks_for_the_reviewer(self) -> None:
        env = {name: value for name, value in os.environ.items() if not name.startswith("TOPA_")}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, "HOME", Path(folder)), \
                mock.patch.object(config, "CONFIG", Path(folder, "config.toml")):
            with self.assertRaisesRegex(SystemExit, "set reviewer"):
                setup_cmd.setup(config.load())
            self.assertTrue(config.CONFIG.is_file())
            self.assertEqual(config.load(), defaults(reviewer=None))


class GateTests(unittest.TestCase):
    """One complete round passes; each listed change makes it fail."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.round, self.repo = Path(self.tmp.name, "round"), Path(self.tmp.name, "tileops")
        kernel = self.repo / "src/tileops/kernels/k.py"
        kernel.parent.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "T")
        kernel.write_text("old\n")
        self.manifest = self.repo / "src/tileops/manifest/spec/a.yaml"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(MANIFEST.format(a="spec-only", b="spec-only"))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        kernel.write_text("new\n")
        git(self.repo, "commit", "-qam", "kernel")

        files = {
            "work/final_hir.py": HIR, "work/twin.py": "twin\n", "work/final.tir": "tir\n",
            "e/pass.json": '{"passed": true}', "e/twin.json": '{"passed": true}', "e/analysis.json": '{"ok": 1}', "e/log": "ok\n",
            "report.md": "ok\n", "findings.json": '{"findings": [], "issue": null}',
            "pr-template.md": filled("pr-template.md"), "pr-comments.md": filled("pr-comments.md"),
        }
        for name, text in files.items():
            (self.round / name).parent.mkdir(parents=True, exist_ok=True)
            (self.round / name).write_text(text)

        def analyze(flag: str, **fields) -> dict:
            return {**fields, "command": f"tilefoundry analyze work/final_hir.py:M out {flag} --json",
                    "report": "e/analysis.json"}

        run = {"command": "pytest x", "report": "e/log"}
        self.provenance = {
            "tileops_base": self.base, "tilefoundry_base": "b" * 40, "result": "bar met",
            "candidate_commit": git(self.repo, "rev-parse", "HEAD"),
            "final_hir": "work/final_hir.py", "runtime_twin": "work/twin.py",
            "production_kernel": "src/tileops/kernels/k.py:K",
            "reference_check": {"command": "tilefoundry check work/final_hir.py:M --expected r.pt --json e/pass.json",
                                "report": "e/pass.json"},
            "floor": [analyze("--roofline", workload="1")],
            "placements": [analyze("--performance", placement="a", chosen=True),
                           analyze("--performance", placement="b", chosen=False)],
            "schedule": {"command": "tilefoundry schedule finalize work/final_hir.py:M work/final.tir",
                         "report": "work/final.tir"},
            "twin_check": {"command": "tilefoundry check work/twin.py:T --json e/twin.json", "report": "e/twin.json"},
            "iterations": [{"change": "c", "basis": "b", "predicted_us": 1.0, "measured_us": 1.0}],
            "tests": run, "benchmark": run, "reviews": ["e/log"],
        }
        self.write_provenance()
        self.workloads, self.measured = ("1",), 1

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def validate(self) -> dict:
        return gate.validate_round(self.round, self.repo, workloads=self.workloads, measured=self.measured)

    def write_provenance(self) -> None:
        (self.round / "provenance.json").write_text(json.dumps(self.provenance))

    def test_complete_round_passes(self) -> None:
        self.assertEqual(self.validate()["result"], "bar met")

    def test_main_and_the_candidate_differ_only_beyond_the_noise(self) -> None:
        """main / candidate below 0.99 is slower; candidate / main below 0.99 beats main."""
        table = (self.round / "pr-comments.md").read_text()
        row = "| 1 | 1 | 1 | 1 | 1 | 1 | 1 |"
        for result, main, error in (("bar met", "0.99", None), ("bar met", "0.989", "is slower than main"),
                                    ("below bar", "1.005", "must beat main"), ("below bar", "1.011", None)):
            self.provenance["result"] = result
            self.write_provenance()
            text = table.replace(row, f"| 1 | {main} | 1 | 1 | 1 | 1 | 1 |")
            (self.round / "pr-comments.md").write_text(text.replace("**bar met**", f"**{result}**"))
            self.edit("pr-template.md", lambda t: re.sub(r"Result: .*", f"Result: {result}", t))
            if error is None:
                self.assertEqual(self.validate()["result"], result)
            else:
                with self.assertRaisesRegex(gate.GateError, error):
                    self.validate()

    def test_trailers_name_the_round_op(self) -> None:
        kwargs = {"workloads": self.workloads, "measured": self.measured, "benchmark": "benchmarks.ops.bench_a"}
        self.assertEqual(gate.validate_round(self.round, self.repo, op="AFwdOp", **kwargs)["result"], "bar met")
        with self.assertRaisesRegex(gate.GateError, "says `Op: AFwdOp`; the round records `BFwdOp`"):
            gate.validate_round(self.round, self.repo, op="BFwdOp", **kwargs)
        self.edit("pr-template.md", lambda t: t.split("\nOp:")[0] + "\n")
        self.assertEqual(gate.validate_round(self.round, self.repo, trailers=False, **kwargs)["result"], "bar met")

    def edit(self, name: str, change) -> None:
        path = self.round / name
        path.write_text(change(path.read_text()))

    def test_other_op_row_with_main_s_program_is_untimed_with_its_comparison_recorded(self) -> None:
        untimed = "| BFwdOp | w | unchanged | unchanged |"
        other = "| --- | --- | ---: | ---: |\n"
        table = (self.round / "pr-comments.md").read_text().replace(f"{other}| 1 | 1 | 1 | 1 |", other + untimed)
        (self.round / "pr-comments.md").write_text(table)
        with self.assertRaisesRegex(gate.GateError, "program_diff must be an object"):
            self.validate()
        self.provenance["program_diff"] = {"command": "diff -r main/ cand/", "report": "e/log"}
        self.write_provenance()
        self.assertEqual(self.validate()["result"], "bar met")
        # Only a pair of markers in an other-ops row stands for main's program; a results row needs times.
        for old, new in ((untimed, "| BFwdOp | w | unchanged | 1 |"),
                         ("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | unchanged | unchanged | 1 | 1 | 1 | 1 |")):
            with self.subTest(new):
                (self.round / "pr-comments.md").write_text(table.replace(old, new))
                with self.assertRaisesRegex(gate.GateError, "needs positive times"):
                    self.validate()

    def test_tie_within_a_timer_step_meets_the_bar(self) -> None:
        self.edit("pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 2 | 1.02 | 0.5 | 1 | 1 | 0.98 |")
                  .replace("| timer step (us) | 1 |", "| timer step (us) | 0.02 |"))
        self.assertEqual(self.validate()["result"], "bar met")

    def test_tie_at_the_dram_floor_meets_the_bar(self) -> None:
        self.edit("pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 2 | 1 | 1 | 1 | 0.5 | 0.5 |")
                  .replace("| timer step (us) | 1 |", "| timer step (us) | 0.02 |"))
        self.assertEqual(self.validate()["result"], "bar met")

    def test_general_round_skips_the_perf_checks(self) -> None:
        """No perf record, no findings.json, and a change under a forbidden path."""
        for name in ("work/final_hir.py", "findings.json"):
            (self.round / name).unlink()
        Path(self.repo, "benchmarks").mkdir()
        Path(self.repo, "benchmarks/b.py").write_text("x\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "b")
        self.provenance["candidate_commit"] = git(self.repo, "rev-parse", "HEAD")
        body = filled("pr-template.md", kind="general") + "\nCloses #7\n"
        (self.round / "pr-template.md").write_text(body)
        (self.round / "pr-comments.md").write_text(filled("pr-comments.md", kind="general"))
        self.provenance = {key: self.provenance[key] for key in ("tileops_base", "candidate_commit", "tests", "reviews")}
        self.write_provenance()
        gate.validate_round(self.round, self.repo, kind="general", issue=7)
        for message, text in {
            "line Closes #7": body.replace("Closes #7", "Refs #7"),
            "must end with the line Closes #7": body + "\n- 1\n",
            "Summary section alone": body + "\n## Regression\n\n- 1\n",
            "title does not follow \\[Type\\]": body.replace("[BugFix][Norm]", "[Bug]"),
            "placeholder": None,
            "sections Test Plan are empty": "## Test Plan\n",
        }.items():
            with self.subTest(message):
                if text is None or text.startswith("## Test Plan"):
                    (self.round / "pr-comments.md").write_text(text or (kit() / "general" / "pr-comments.md").read_text())
                    text = body
                (self.round / "pr-template.md").write_text(text)
                with self.assertRaisesRegex(gate.GateError, message):
                    gate.validate_round(self.round, self.repo, kind="general", issue=7)

    def test_perf_issue_round_takes_its_rows_from_provenance(self) -> None:
        self.edit("pr-template.md", lambda t: t.replace("\n\nOp:", "\nRefs #7\n\nOp:"))
        self.provenance["workloads"] = ["1"]
        self.write_provenance()
        self.assertEqual(gate.validate_round(self.round, self.repo, issue=7)["result"], "bar met")
        for message, workloads in {"floor lacks the rows behind w2": ["1", "w2"], "non-empty list": []}.items():
            with self.subTest(message):
                self.provenance["workloads"] = workloads
                self.write_provenance()
                with self.assertRaisesRegex(gate.GateError, message):
                    gate.validate_round(self.round, self.repo, issue=7)

    def new_op(self) -> None:
        """Turn the complete perf round into a complete new-op round on issue 7."""
        body = filled("pr-template.md", kind="new-op").replace("\n\nOp:", "\nCloses #7\n\nOp:")
        (self.round / "pr-template.md").write_text(body)
        (self.round / "pr-comments.md").write_text(filled("pr-comments.md", kind="new-op"))
        self.provenance |= {"workloads": ["1"], "base_tests": {"command": "pytest t", "report": "e/log"}}
        self.inventory = ("1",)
        self.commit(**{MANIFEST_PATH: MANIFEST.format(a="implemented", b="spec-only")})

    def validate_new_op(self) -> dict:
        return gate.validate_round(self.round, self.repo, kind="new-op", issue=7, op="AFwdOp", inventory=self.inventory)

    def commit(self, *paths: str, **texts: str) -> None:
        for path, text in {**dict.fromkeys(paths, "x\n"), **texts}.items():
            Path(self.repo, path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.repo, path).write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "c")
        self.provenance["candidate_commit"] = git(self.repo, "rev-parse", "HEAD")
        self.write_provenance()

    def test_new_op_round_passes(self) -> None:
        """A new op has no main: no main column, and below bar asks for the stop records, not a row faster than main."""
        self.new_op()
        self.assertEqual(self.validate_new_op()["result"], "bar met")
        self.commit("benchmarks/ops/bench_a.py", "workloads/a.py")
        self.assertEqual(self.validate_new_op()["result"], "bar met")
        (self.round / "report.md").write_text("## Stop records\n\n(1) holds.\n")
        self.provenance["result"] = "below bar"
        self.write_provenance()
        self.edit("pr-template.md", lambda t: t.replace("Result: bar met", "Result: below bar"))
        self.edit("pr-comments.md", lambda t: t.replace("**bar met**", "**below bar**")
                  .replace("| 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 1 | 0.5 | 1 | 0.5 | 0.5 |"))
        self.assertEqual(self.validate_new_op()["result"], "below bar")

    def test_new_op_row_without_a_candidate_time_fails(self) -> None:
        """No main column checks the candidate cell, so the ratio check must, for 0, nan, text or none."""
        self.new_op()
        table = (self.round / "pr-comments.md").read_text()
        for row in ("| 1 | 0 | 1 | 1 | 1 | 1 |", "| 1 | nan | 1 | 1 | 1 | 1 |", "| 1 | x | 1 | 1 | 1 | 1 |", "| 1 |"):
            with self.subTest(row):
                (self.round / "pr-comments.md").write_text(table.replace("| 1 | 1 | 1 | 1 | 1 | 1 |", row))
                with self.assertRaisesRegex(gate.GateError, "row '1' needs a positive candidate time"):
                    self.validate_new_op()

    def test_new_op_defect_fails(self) -> None:
        p = self.provenance
        cases = {
            "title does not follow \\[Feat\\]\\[foundry\\]": lambda: self.edit(
                "pr-template.md", lambda t: t.replace("[Feat][foundry][Test]", "[Perf][foundry][Test]")),
            "line Closes #7": lambda: self.edit("pr-template.md", lambda t: t.replace("Closes #7", "Refs #7")),
            "schedule must be an object": lambda: p.pop("schedule"),
            "findings.json": lambda: (self.round / "findings.json").unlink(),
            # The admitted inventory, not the provenance workloads, names the cases.
            "floor lacks the rows behind w2": lambda: (setattr(self, "inventory", ("1", "w2")),
                                                       p.update(workloads=["1"])),
            "results table has 1 rows; the op has 2 measured rows": lambda: (
                setattr(self, "inventory", ("1", "w2")), p["floor"].append({**p["floor"][0], "workload": "w2"})),
            "no admitted inventory": lambda: setattr(self, "inventory", ()),
            "more than one timer step behind": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 1 | 0.5 | 1 | 0.5 | 0.5 |")
                .replace("| timer step (us) | 1 |", "| timer step (us) | 0.02 |")),
            "row 'OtherFwdOp w' is slower than main": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| --- | --- | ---: | ---: |\n| 1 | 1 | 1 | 1 |",
                                                      "| --- | --- | ---: | ---: |\n| OtherFwdOp | w | 1 | 2 |")),
            "forbidden paths: scripts/a.py": lambda: self.commit("scripts/a.py"),
            "set the status of AFwdOp from spec-only to implemented": lambda: self.commit(
                **{MANIFEST_PATH: MANIFEST.format(a="implemented", b="spec-only").replace("test", "other")}),
            "status of AFwdOp": lambda: self.commit(**{MANIFEST_PATH: MANIFEST.format(a="nonsense", b="spec-only")}),
            "status of AFwdOp from": lambda: self.commit(
                **{MANIFEST_PATH: MANIFEST.format(a="spec-only", b="implemented")}),
            "to implemented, and change nothing else": lambda: self.commit("src/tileops/manifest/spec/b.yaml"),
            "base_tests must be an object": lambda: p.pop("base_tests"),
            "non-empty `## Stop records`": lambda: (p.update(result="below bar"), self.edit(
                "pr-comments.md", lambda t: t.replace("**bar met**", "**below bar**"))),
        }
        for message, defect in cases.items():
            with self.subTest(message):
                self.tearDown()
                self.setUp()
                p = self.provenance
                self.new_op()
                defect()
                self.write_provenance()
                with self.assertRaisesRegex(gate.GateError, message):
                    self.validate_new_op()

    def extend(self) -> None:
        """Turn the complete perf round into a complete extend round on issue 7: it serves case a, and keeps b."""
        (self.round / "pr-template.md").write_text(filled("pr-template.md", kind="extend"))
        self.edit_extend_tables("| AFwdOp | b |")
        self.provenance |= {"workloads": ["AFwdOp a"], "base_tests": {"command": "pytest t", "report": "e/log"}}
        self.provenance["floor"][0]["workload"] = "AFwdOp a"
        self.inventory = ("AFwdOp a", "AFwdOp b")
        self.write_provenance()

    def edit_extend_tables(self, other: str) -> None:
        (self.round / "pr-comments.md").write_text(filled("pr-comments.md", kind="extend").replace(
            "| 1 | 1 | 1 | 1 | 1 | 1 |", "| AFwdOp a | 1 | 1 | 1 | 1 | 1 |").replace(
            "| --- | --- | ---: | ---: |\n| 1 |", f"| --- | --- | ---: | ---: |\n{other}"))

    def validate_extend(self) -> dict:
        return gate.validate_round(self.round, self.repo, kind="extend", issue=7, inventory=self.inventory)

    def test_extend_round_passes(self) -> None:
        """The rows it serves meet the bar with no main column; below bar asks for the stop records."""
        self.extend()
        self.assertEqual(self.validate_extend()["result"], "bar met")
        # A case whose generated program is main's is listed untimed, with the program comparison recorded.
        self.edit_extend_tables("| AFwdOp | b | unchanged | unchanged |")
        self.provenance["program_diff"] = {"command": "diff -r main/ cand/", "report": "e/log"}
        self.write_provenance()
        self.assertEqual(self.validate_extend()["result"], "bar met")
        (self.round / "report.md").write_text("## Stop records\n\n(1), (2) and (3) hold for AFwdOp a.\n")
        self.provenance["result"] = "below bar"
        self.write_provenance()
        self.edit("pr-comments.md", lambda t: t.replace("**bar met**", "**below bar**")
                  .replace("| AFwdOp a | 1 | 1 | 1 | 1 | 1 |", "| AFwdOp a | 1 | 0.5 | 2 | 0.5 | 0.5 |")
                  .replace("| timer step (us) | 1 |", "| timer step (us) | 0.02 |"))
        self.assertEqual(self.validate_extend()["result"], "below bar")

    def test_extend_defect_fails(self) -> None:
        p = self.provenance
        cases = {
            "title does not follow \\[Feat\\]\\[foundry\\]": lambda: self.edit(
                "pr-template.md", lambda t: t.replace("[Feat][foundry][Test]", "[BugFix][foundry][Test]")),
            "must end with the line Closes #7": lambda: self.edit("pr-template.md", lambda t: t + "\nOp: AFwdOp\n"),
            "no table has the cases AFwdOp b": lambda: self.edit_extend_tables("| OtherFwdOp | b |"),
            "row 'AFwdOp b' is slower than main": lambda: self.edit_extend_tables("| AFwdOp | b | 1 | 2 |"),
            "results table holds AFwdOp b, which are not rows to serve": lambda: (
                self.edit_extend_tables("| OtherFwdOp | w | 1 | 1 |"), self.edit("pr-comments.md", lambda t: t.replace("| AFwdOp a | 1 | 1 | 1 | 1 | 1 |",
                                                      "| AFwdOp a | 1 | 1 | 1 | 1 | 1 |\n| AFwdOp b | 1 | 1 | 1 | 1 | 1 |"))),
            "provenance workloads AFwdOp z are not admitted cases": lambda: p.update(workloads=["AFwdOp z"]),
            "forbidden paths: src/tileops/manifest/spec/a.yaml": lambda: self.commit(
                **{MANIFEST_PATH: MANIFEST.format(a="implemented", b="spec-only")}),
            # A file moved out of a forbidden root is still a change under it.
            "forbidden paths: src/tileops/manifest/spec/a.yaml$": lambda: (
                git(self.repo, "mv", MANIFEST_PATH, "src/tileops/a.yaml"), self.commit()),
            "program_diff must be an object": lambda: self.edit_extend_tables("| AFwdOp | b | unchanged | unchanged |"),
            "base_tests must be an object": lambda: p.pop("base_tests"),
            "non-empty `## Stop records`": lambda: (p.update(result="below bar"), self.edit(
                "pr-comments.md", lambda t: t.replace("**bar met**", "**below bar**"))),
        }
        for message, defect in cases.items():
            with self.subTest(message):
                self.tearDown()
                self.setUp()
                p = self.provenance
                self.extend()
                defect()
                self.write_provenance()
                with self.assertRaisesRegex(gate.GateError, message):
                    self.validate_extend()
        self.commit(**{MANIFEST_PATH: MANIFEST.format(a="implemented", b="spec-only")})
        with self.assertRaisesRegex(gate.GateError, "an extension changes no src/tileops/manifest/ file"):
            gate.check_op(gate.Evidence(self.round, self.repo, "extend", 7, inventory=self.inventory))

    def test_unfilled_template_fails(self) -> None:
        for name in ("pr-template.md", "pr-comments.md"):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                (self.round / name).write_text(pr_comments("perf") if name == "pr-comments.md"
                                               else (kit() / "perf" / name).read_text())
                with self.assertRaisesRegex(gate.GateError, "placeholder"):
                    self.validate()

    def test_each_defect_fails(self) -> None:
        finding = {"id": "f", "classification": "c", "command": "c", "expected": "e", "actual": "a",
                   "workaround_cost": "w", "public_surface": "p", "affected_workloads": [], "reproducer": "e/log"}
        p = self.provenance
        cases = {
            "reference_check must record a tilefoundry command": lambda: p["reference_check"].update(command="python check.py"),
            "at least two placements": lambda: p.update(placements=p["placements"][:1]),
            "positive measured_us": lambda: p["iterations"][0].update(measured_us=0),
            "reviews must be a non-empty list": lambda: p.update(reviews=[]),
            "one tile-ai/TileFoundry issue": lambda: (self.round / "findings.json").write_text(
                json.dumps({"findings": [finding], "issue": "https://github.com/tile-ai/TileFoundry/pull/1"})),
            "null when there is no finding": lambda: (self.round / "findings.json").write_text(
                '{"findings": [], "issue": "https://github.com/tile-ai/TileFoundry/issues/1"}'),
            "exact final HIR": lambda: (self.round / "work/final_hir.py").write_text(HIR + "# changed\n"),
            "title as a `# ` line": lambda: self.edit("pr-template.md", lambda t: t.split("\n", 1)[1]),
            "placeholder ```text": lambda: self.edit("pr-comments.md",
                                                     lambda t: t.replace("```text\n1\n```", "```text\n<x>\n```")),
            "placeholder <x>": lambda: self.edit("pr-template.md", lambda t: t.replace("## Summary\n\n- 1", "## Summary\n\n- <x>")),
            "Summary section alone": lambda: self.edit("pr-template.md", lambda t: t + "\n## Benchmark\n\n- 1\n"),
            "exactly the `Op:`, `Benchmark:`, `Result:` lines": lambda: self.edit(
                "pr-template.md", lambda t: t.replace("Benchmark: benchmarks.ops.bench_a\n", "")),
            "end with one paragraph of exactly": lambda: self.edit(
                "pr-template.md", lambda t: t + "\nRefs #1\n"),
            "says `Result: below bar`; the round records `bar met`": lambda: self.edit(
                "pr-template.md", lambda t: t.replace("Result: bar met", "Result: below bar")),
            "TileFoundry Findings, in order": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("## TileFoundry Findings", "## Findings")),
            "full sha256": lambda: self.edit("pr-comments.md", lambda t: t.replace("a" * 64, "a" * 12)),
            "main \\(us\\)": lambda: self.edit("pr-comments.md", lambda t: t.replace("| main (us) ", "| ")),
            "row 'OtherFwdOp w' is slower than main": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| --- | --- | ---: | ---: |\n| 1 | 1 | 1 | 1 |", "| --- | --- | ---: | ---: |\n| OtherFwdOp | w | 1 | 2 |")),
            "the result of provenance.json": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("**bar met**", "**below bar**")),
            "below bar result must beat main": lambda: (p.update(result="below bar"), self.edit(
                "pr-comments.md", lambda t: t.replace("**bar met**", "**below bar**"))),
            "more than one timer step behind": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 1 | 1 | 0.5 | 1 | 0.5 | 0.5 |")
                .replace("| timer step (us) | 1 |", "| timer step (us) | 0.02 |")),
            "rows 1 appear more than once": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 1 | 1 | 1 | 1 | 1 | 1 |\n| 1 | 2 | 1 | 1 | 1 | 1 | 1 |")),
            "timer step must be a positive": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| timer step (us) | 1 |", "| timer step (us) | x |")),
            "states ratio 1.0, its times give 0.5000": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 1 | 1 | 1 | 1 | 0.5 | 1 |")),
            "other-ops table needs": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| Op | Workload | main (us) | candidate (us) |", "| Op | Workload | main (us) | new (us) |")),
            "needs positive times": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | inf | 1 | 1 | 1 | 1 | 1 |")),
            "row '1' is slower than main": lambda: self.edit(
                "pr-comments.md", lambda t: t.replace("| 1 | 1 | 1 | 1 | 1 | 1 | 1 |", "| 1 | 1 | 2 | 1 | 1 | 1 | 1 |")),
            "the results table lacks the rows behind w2": lambda: (
                setattr(self, "workloads", ("1", "w2")), p["floor"].append({**p["floor"][0], "workload": "w2"})),
            "the op has 2 measured rows": lambda: setattr(self, "measured", 2),
            "floor lacks the rows behind w2": lambda: setattr(self, "workloads", ("1", "w2")),
            "candidate_commit must be the commit": lambda: p.update(candidate_commit="c" * 40),
            "positive predicted_us": lambda: p["iterations"][0].pop("predicted_us"),
            "exposes a local path": lambda: self.edit("pr-comments.md", lambda t: t + "/home/x/log\n"),
            "forbidden paths": lambda: (Path(self.repo, "benchmarks").mkdir(),
                                        Path(self.repo, "benchmarks/b.py").write_text("x\n"),
                                        git(self.repo, "add", "."), git(self.repo, "commit", "-qm", "b"),
                                        p.update(candidate_commit=git(self.repo, "rev-parse", "HEAD"))),
        }
        for message, defect in cases.items():
            with self.subTest(message):
                self.tearDown()
                self.setUp()
                p = self.provenance
                defect()
                self.write_provenance()
                with self.assertRaisesRegex(gate.GateError, message):
                    self.validate()


class HookTests(unittest.TestCase):
    def test_configured_reviewer_runs_on_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            round_dir = Path(folder)
            (round_dir / "round.json").write_text('{"alias": "topa-x-r1", "reviewer": "review-agent"}')
            event = lambda command: {"tool_input": {"command": command}, "cwd": folder}
            review = '"review" < /dev/null > evidence/review-1.md 2>&1'
            self.assertIsNone(decide(event(f"cd /wt && /opt/bin/review-agent --read-only {review}"), round_dir))
            self.assertIsNotNone(decide(event(f"other-agent {review}"), round_dir))

    def test_routing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            round_dir = Path(folder)
            (round_dir / "round.json").write_text('{"alias": "topa-x-r1"}')
            event = lambda command: {"tool_input": {"command": command}, "cwd": folder}
            for command in (
                "git status && ls",
                "/x/.venv/bin/topa pr --reviewed",
                '/x/.venv/bin/topa round transition PR --round x-r1 --reason "published"',
                'gh issue view 1 2>&1 | head -300; echo "rc=$?"',
                'cd /x && grep -n "hir\\|iteration" gate.py | head -60',
                "git log --oneline\ncat notes.md > /dev/null 2>&1",
            ):
                self.assertIsNone(decide(event(command), round_dir), command)
            for command in (
                "cd work && pytest -x",
                "python run.py | head",
                "ls\npython run.py",
                "git status; nvidia-smi &",
                "echo 'unbalanced",
                'echo "$(python work/probe.py)"',
                "echo `python work/probe.py`",
                'echo "$(ls $(python x.py))"',
                "cat <(python x.py)",
                "echo $\\\n(python work/probe.py)",
                "echo \\\\\npython work/probe.py",
                'echo "$(echo ok # )\npython work/probe.py\n)"',
            ):
                routed = decide(event(command), round_dir)
                assert routed is not None, command
                self.assertTrue(routed["hookSpecificOutput"]["updatedInput"]["command"].startswith("ssh -T topa-x-r1 "))


class WikiTests(unittest.TestCase):
    def test_page(self) -> None:
        parsed = page()
        self.assertEqual((parsed.run_id, parsed.commit), ("42257687830", "6da068de6055db5e1f31f43fba83860d257f0d98"))
        self.assertEqual([op.name for op in parsed.queue[:3]], ["GQAVarlenFwdOp", "GroupedGEMMFwdOp", "SoftmaxFwdOp"])
        self.assertEqual(len(parsed.queue), 19)
        op = parsed.op("GroupedGEMMFwdOp")
        self.assertEqual((op.category, op.label, op.worst, op.measured, op.nightlies, op.benchmark),
                         ("gemm", "P2", 0.8588, 10, 2, "benchmarks.ops.bench_grouped_gemm"))
        self.assertEqual(op.rows[0], wiki.Row("qwen3-30b-ep8-skew-down-bfloat16", 48.16, "quack", 41.361, 0.8588))

    def test_unexpected_format_fails_loudly(self) -> None:
        text = PAGE.read_text()
        cases = {
            "no 'Nightly run": text.replace("Nightly run [", "Run ["),
            "no table with the header": text.replace("| Worst ratio |", "| Worst |"),
            "unexpected row": text.replace("| 0.8119 | 2 of 63 |", "| 0.8119 | 2/63 |"),
            "no '### SoftmaxFwdOp'": text.replace("### SoftmaxFwdOp", "### Softmax"),
            "1 rows, the queue says 2": re.sub(r"\| `llama-8b-chunk-decodes-float16` .*\n", "", text),
            "the line under the header": text.replace("| ---: | --- | --- | --- | ---: | ---: | ---: |", ""),
            "has ops the queue lacks: ReciprocalFwdOp": re.sub(r"\| 19 \| .*\n", "", text),
            "repeated '### DivFwdOp' heading": text + "\n### DivFwdOp\n",
            "repeated '## Queue' heading": text + "\n## Queue\n",
            "DivFwdOp is listed twice": text.replace("| 5 | [RMSNormFwdOp](#rmsnormfwdop) |", "| 5 | [DivFwdOp](#divfwdop) |"),
        }
        for message, broken in cases.items():
            with self.subTest(message), self.assertRaisesRegex(wiki.FormatError, re.escape(message)):
                page(broken)


class RoundTests(unittest.TestCase):
    def test_slug(self) -> None:
        self.assertEqual(round_slug("SoftmaxFwdOp", [{"round": "softmaxfwd-r1"}]), "softmaxfwd-r2")
        long = round_slug("FusedAddRMSNormPerTokenQuantFwdOp", [])
        self.assertRegex(long, r"^[a-z][a-z0-9_-]{0,31}$")

    def admit(self, issues: dict | None = None, prs: list | None = None, found: list | None = None,
              entry: list[str] = ROPE_ENTRY, spec_only: tuple[str, ...] = ("RoPECosSinFwdOp",),
              implemented: tuple[str, ...] = ()):
        """Mock what admission reads: the fetched main, the manifest, the issues, the PRs and the gh login."""
        def gh_json(*args):
            if args[:2] == ("issue", "view"):
                return issues[int(args[2])]
            return list(prs or []) if args[:2] == ("pr", "list") else list(found or [])

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for patch in (mock.patch("topa.ledger.LEDGER", Path(folder.name, "ledger.jsonl")),
                      mock.patch("topa.sh.require"), mock.patch("topa.round.topa_binary"),
                      mock.patch("topa.round._fetch_main", return_value="c" * 40),
                      mock.patch("topa.round._entry", return_value=("src/tileops/manifest/spec/rope.yaml", entry)),
                      mock.patch("topa.round._manifest_ops", side_effect=lambda config, commit, names: {
                          name: (f"{name}.yaml", [line.replace("spec-only", "implemented") if name in implemented
                                                  else line for line in ROPE_ENTRY])
                          for name in names if name in spec_only + implemented}),
                      mock.patch("topa.sh.gh_json", side_effect=gh_json),
                      mock.patch("topa.sh.gh_login", return_value="me")):
            stack.enter_context(patch)

    def test_start_claims_one_round_per_op(self) -> None:
        self.admit(entry=["  family: reduction", "  status: implemented"])
        plan = start(defaults(), "SoftmaxFwdOp", page=page(), dry_run=True)
        self.assertEqual((plan["round"], plan["branch"]), ("softmaxfwd-r1", "perf/reduction/softmaxfwd-r1"))
        ledger.append("create", round="softmaxfwd-r1", op="SoftmaxFwdOp")
        with self.assertRaisesRegex(SystemExit, "round softmaxfwd-r1 already works SoftmaxFwdOp"):
            start(defaults(), "SoftmaxFwdOp", page=page(), dry_run=True)
        with self.assertRaisesRegex(SystemExit, "is not in the queue"):
            start(defaults(), "NoSuchFwdOp", page=page(), dry_run=True)

    def test_brief_renders_every_placeholder(self) -> None:
        shared = "".join([(kit() / "brief.md").read_text(), *foundry_text().values()])
        for kind in KINDS:
            template = shared + (kit() / kind / "brief.md").read_text()
            text = brief(kind, {name: name for name in re.findall(r"\{\{([A-Z_]+)\}\}", template)})
            self.assertNotIn("{{", text)
            self.assertEqual("### Implementation rounds" in text, kind != "general")
            self.assertEqual("round transition" in text, kind != "general")
            # In state PR a TileFoundry round does not rerun the cleanup and reviews of PR steps 4-6.
            self.assertEqual("skip steps 4-6" in text, kind != "general")
            self.assertIn("4. Run cleanup steps 5-6.\n5. Run the review", text)
            self.assertEqual("### Op and tests" in text, kind in ("new-op", "extend"))
            self.assertEqual("Add no op." in text, kind in ("perf", "extend"))
            # Only a perf brief carries earlier attempts.
            self.assertEqual("PRIOR_ATTEMPTS" in text, kind == "perf")
        # Every piece of the shared TileFoundry text reaches both kinds that build with TileFoundry.
        for kind in gate.FOUNDRY_KINDS:
            used = set(re.findall(r"\{\{(FOUNDRY_[A-Z_]+)\}\}", (kit() / kind / "brief.md").read_text()))
            self.assertEqual(used, set(foundry_text()), kind)
        with self.assertRaises(SystemExit):
            render("{{A}} {{B}}", {"A": 1})

    def test_issue_routes_to_perf_new_op_extend_or_general(self) -> None:
        """`[Perf]` wins; a `[Feat]` title naming one spec-only op gets new-op, only implemented ops extend;
        any other issue general. A label does not route."""
        self.admit({number: issue(number, title, labels) for number, title, labels in (
            (1, "[FEAT][RoPE] Add the cos/sin cache op", ["new-op"]), (2, "[FEAT][RoPE] Add RoPECosSinFwdOp", []),
            (3, "[Perf] Speed up RoPECosSinFwdOp", []), (4, "[BUG][Norm] Fix SoftmaxFwdOp and BFwdOp", ["bug"]),
            (5, "[FEAT] Add RoPECosSinFwdOp and AFwdOp", []), (6, "[FEAT] Serve SoftmaxFwdOp and BFwdOp with NoSuchOp", []),
            (7, "[Feat] Tune SoftmaxFwdOp tails", []), (8, "[FEAT] Serve CFwdOp", []),
            (9, "[BugFix] Fix RoPECosSinFwdOp", []), (10, "[Doc] Explain SoftmaxFwdOp", []))},
            spec_only=("RoPECosSinFwdOp", "AFwdOp"), implemented=("SoftmaxFwdOp", "BFwdOp"))
        plans = [start(defaults(), issue=number, dry_run=True) for number in (1, 2, 3, 4, 6, 9, 10)]
        self.assertEqual([(plan["kind"], plan["branch"], plan.get("op"), plan.get("ops")) for plan in plans], [
            ("general", "feat/issue/issue-1-r1", None, None),
            ("new-op", "feat/rope/ropecossinfwd-r1", "RoPECosSinFwdOp", None),
            ("perf", "perf/issue/issue-3-r1", None, None),
            ("general", "fix/issue/issue-4-r1", None, None),
            ("extend", "feat/issue/issue-6-r1", None, ["SoftmaxFwdOp", "BFwdOp"]),
            ("general", "fix/issue/issue-9-r1", None, None), ("general", "doc/issue/issue-10-r1", None, None)])
        # Two ops with the same labels keep apart by the op in each case.
        self.assertEqual(plans[4]["inventory"], [f"{op} llama3-8b-prefill {{T: {t}}}" for op in ("SoftmaxFwdOp", "BFwdOp")
                                                 for t in ("float16", "bfloat16")])
        with mock.patch("topa.round._manifest_ops", return_value={"CFwdOp": ("c.yaml", ["  status: retired"])}), \
                self.assertRaisesRegex(SystemExit, "names ops of no known status: CFwdOp is retired"):
            start(defaults(), issue=8, dry_run=True)
        ledger.append("create", **plans[4])
        with self.assertRaisesRegex(SystemExit, "round issue-6-r1 already works SoftmaxFwdOp"):
            start(defaults(), issue=7, dry_run=True)
        self.assertEqual((plans[1]["family"], plans[1]["issue"], plans[1]["inventory"]),
                         ("rope", 2, ["llama3-8b-prefill {T: float16}", "llama3-8b-prefill {T: bfloat16}"]))
        with self.assertRaisesRegex(SystemExit, "several spec-only ops \\(RoPECosSinFwdOp, AFwdOp\\)"):
            start(defaults(), issue=5, dry_run=True)
        ledger.append("create", **plans[1])
        with self.assertRaisesRegex(SystemExit, "already works RoPECosSinFwdOp"):
            start(defaults(), "RoPECosSinFwdOp", dry_run=True)

    def test_issue_and_perf_briefs_link_their_issue(self) -> None:
        bug = issue(2812, "[BUG][Norm] LayerNorm loses precision", ["bug"], "## Repro\nx = 1e36")
        values = {name: name for name in ("SLUG", "ROUND_DIR", "UPSTREAM", "BRANCH", "TILEOPS_BASE",
                                           "TILEOPS_WORKTREE", "TILEFOUNDRY_BASE", "TILEFOUNDRY_SOURCE",
                                           "KNOWLEDGE", "TOPA", "PRIOR_ATTEMPTS", "REVIEWER", "CHECKLISTS",
                                           "STATE_PATH")}
        text = brief("general", values | _issue_work(bug, "general")[1])
        self.assertIn("> ## Repro\n> x = 1e36", text)
        self.assertIn("`Closes #2812`", text)
        text = brief("perf", values | _issue_work(issue(2791, "[Perf][GEMM] Close the gap", []), "perf")[1])
        self.assertIn("`Refs #2791`", text)
        self.assertIn("copy their benchmark names into `workloads`", text)

    def test_new_op_admission_refuses_work_someone_else_owns(self) -> None:
        """Issues and PRs of others refuse; the user's own draft PR and a title that only contains the op do not."""
        mine = {"number": 8, "title": "[Feat] Add RoPECosSinFwdOp", "body": "", "author": {"login": "me"},
                "url": "u8", "isDraft": True}
        other = {"number": 9, "title": "[Feat] Rotary cache", "body": "Adds RoPECosSinFwdOp.",
                 "author": {"login": "alice"}, "url": "u9"}
        closing = {"number": 10, "title": "[Feat] Rotary", "body": "Closes #2", "author": {"login": "bob"}, "url": "u10"}
        near = issue(6, "[Feat] Add RoPECosSinFwdOpV2", [])
        issues = {2: issue(2, "[FEAT][RoPE] Add RoPECosSinFwdOp", []), 3: issue(3, "[FEAT] Add a cache", []),
                  4: issue(4, "[FEAT] Add RoPECosSinFwdOp", [], assignees=["alice", "me"]),
                  7: issue(7, "[FEAT] Add RoPECosSinFwdOp", [], state="CLOSED")}
        cases = [
            ({"prs": [mine], "found": [near]}, "RoPECosSinFwdOp", None, None),
            ({"prs": [mine]}, "RoPECosSinFwdOp", 2, None),
            ({"prs": [mine, other]}, "RoPECosSinFwdOp", 2, "PR #9 by alice already works RoPECosSinFwdOp"),
            ({"prs": [closing]}, "RoPECosSinFwdOp", 2, "PR #10 by bob"),
            *[({"prs": [{**closing, "body": body}]}, "RoPECosSinFwdOp", 2, "PR #10 by bob") for body in (
                "fixed tile-ai/TileOPs#2", "RESOLVES https://github.com/tile-ai/TileOPs/issues/2", "Close: #2")],
            # Another repository's issue 2, or issue 20, is not this one.
            *[({"prs": [{**closing, "body": body}]}, "RoPECosSinFwdOp", 2, None) for body in (
                "Closes other/repo#2", "Fixes https://github.com/other/repo/issues/2", "Closes #20")],
            ({}, "RoPECosSinFwdOp", 4, "issue #4 is assigned to alice"),
            ({}, "RoPECosSinFwdOp", 3, "the title of issue #3 does not name RoPECosSinFwdOp"),
            ({}, "RoPECosSinFwdOp", 7, "issue #7 is closed"),
            ({"found": [issues[2], issues[4]]}, "RoPECosSinFwdOp", None, "open issues #2, #4 name RoPECosSinFwdOp"),
            ({"entry": ["  status: implemented"]}, "SoftmaxFwdOp", 2, "SoftmaxFwdOp is implemented, not spec-only"),
            ({"entry": ROPE_ENTRY[1:]}, "RoPECosSinFwdOp", None, "needs a family and a label"),
            ({"entry": ROPE_ENTRY[:-1] + ["  - {B: 1, dtype_cases: [{T: float16}]}"]}, "RoPECosSinFwdOp", None,
             "needs a family and a label"),
        ]
        for mocks, op, number, refusal in cases:
            with self.subTest(refusal or number):
                self.admit(issues, **mocks)
                if refusal is None:
                    plan = start(defaults(), op, issue=number, dry_run=True)
                    self.assertEqual((plan["kind"], plan.get("issue")), ("new-op", number))
                    continue
                with self.assertRaisesRegex(SystemExit, refusal):
                    start(defaults(), op, issue=number, dry_run=True)
                self.assertEqual(ledger.rounds(), {})

    def test_manifest_is_read_from_the_fetched_upstream_main(self) -> None:
        entry = ("# family\n\nRoPECosSinFwdOp:  # cache\n  family: rope\n  ref_api: \"x\"\n  # status: implemented\n"
                 "  signature:\n    status: implemented\n  status: spec-only\n  workloads:\n"
                 "  # a comment\n  - {B: 1, dtype_cases: [{T: float16}, {T: [bfloat16, float32]}], label: a}\n"
                 "  - B: 2\n    label: b\n  roofline: {}\n\nSoftmaxFwdOp:\n  status: implemented\n")
        rope = manifest_entry(entry, "RoPECosSinFwdOp")
        self.assertEqual((entry_field(rope, "status"), entry_field(rope, "family")), ("spec-only", "rope"))
        self.assertEqual(inventory(rope), ["a {T: float16}", "a {T: [bfloat16, float32]}", "b"])
        self.assertEqual(entry_field(manifest_entry(entry, "SoftmaxFwdOp"), "status"), "implemented")
        self.assertIsNone(manifest_entry(entry, "RoPECosSinFwd"))
        with tempfile.TemporaryDirectory() as folder:
            upstream, local = Path(folder, "upstream"), Path(folder, "local")
            subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(local)], check=True)
            git(local, "remote", "add", "origin", str(upstream))
            # A refspec without main: `origin/main` never exists, so only FETCH_HEAD has the fetched main.
            git(local, "config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other")
            spec = upstream / "src/tileops/manifest/spec/rope.yaml"
            spec.parent.mkdir(parents=True)
            spec.write_text(entry)
            (spec.parent / "softmax.yaml").write_text("SoftmaxFwdOp:\n  status: spec-only\n")
            git(upstream, "add", ".")
            git(upstream, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "a")
            config_ = defaults(tileops_repo=local)
            with mock.patch("topa.sh.github_remote", return_value="origin"):
                commit = _fetch_main(config_)
            found = _manifest_ops(config_, commit, ["RoPECosSinFwdOp", "NoSuchFwdOp"])
            self.assertEqual(_entry(config_, commit, "RoPECosSinFwdOp")[0], "src/tileops/manifest/spec/rope.yaml")
            with self.assertRaisesRegex(SystemExit, "SoftmaxFwdOp has 2 manifest entries"):
                _manifest_ops(config_, commit, ["SoftmaxFwdOp"])
        self.assertEqual({op: path for op, (path, _) in found.items()},
                         {"RoPECosSinFwdOp": "src/tileops/manifest/spec/rope.yaml"})

    def test_branch_is_dropped_only_when_its_pr_merged_its_tip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            subprocess.run(["git", "init", "-q", "-b", "main", folder], check=True)
            git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "a")
            git(repo, "branch", "perf/x/x-r1")
            tip = git(repo, "rev-parse", "HEAD")
            for pr, kept in ((None, True), ({"number": 1, "state": "OPEN", "headRefOid": tip}, True),
                             ({"number": 1, "state": "CLOSED", "headRefOid": tip}, True),
                             ({"number": 1, "state": "MERGED", "headRefOid": "0" * 40}, True),
                             ({"number": 1, "state": "MERGED", "headRefOid": tip}, False)):
                with self.subTest(pr), mock.patch("topa.round.pull_request", return_value=pr):
                    drop_branch(defaults(tileops_repo=repo), "perf/x/x-r1")
                    self.assertEqual(bool(git(repo, "branch", "--list", "perf/x/x-r1")), kept)

    def test_removing_a_round_removes_the_worktrees_it_made_inside_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo, round_dir = Path(folder, "repo"), Path(folder, "round")
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "a")
            inside, gone, locked = (round_dir / "work" / name for name in ("cand", "gone", "locked"))
            neighbor = Path(folder, "round2", "cand")
            for path in (inside, gone, locked, neighbor):
                git(repo, "worktree", "add", "-q", "--detach", str(path))
            shutil.rmtree(gone)
            git(repo, "worktree", "lock", str(locked))
            self.assertEqual(remove_worktrees_under(defaults(tileops_repo=repo), round_dir), (2, [locked]))
            listed = {line.removeprefix("worktree ") for line in git(repo, "worktree", "list", "--porcelain").splitlines()
                      if line.startswith("worktree ")}
            self.assertEqual([str(p) in listed for p in (inside, gone, locked, neighbor)], [False, False, True, True])
            self.assertFalse(inside.exists())

    def test_ledger_fold(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "ledger.jsonl")
            for event, fields in (("create", {"pane": "w1:p1"}), ("start", {}), ("say", {"text": "hi"})):
                ledger.append(event, path, round="x-r1", **fields)
            view = ledger.rounds(path)["x-r1"]
        self.assertEqual((view["state"], view["pane"], "text" in view), ("running", "w1:p1", False))


class TransitionTests(unittest.TestCase):
    """A new-op round on issue 7, created in REQ."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "tileops"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "a")
        for patch in (mock.patch("topa.ledger.LEDGER", root / "ledger.jsonl"), mock.patch("topa.config.HOME", root)):
            patch.start()
            self.addCleanup(patch.stop)
        self.config = defaults()
        self.dir = self.config.rounds / "x-r1"
        self.dir.mkdir(parents=True)
        meta = {"round": "x-r1", "kind": "new-op", "op": "AFwdOp", "issue": 7, "worktree": str(self.repo)}
        (self.dir / "round.json").write_text(json.dumps(meta))
        ledger.append("create", **meta, phase="REQ")

    def move(self, target: str) -> dict:
        return transition(self.config, target, "  the\nreason ", "x-r1")

    def test_the_table_refuses_edges_off_the_graph(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot move from REQ to FLOOR; it can move to HIR, REQ, STOPPED"):
            self.move("FLOOR")
        perf, new_op = states.TRANSITIONS["perf"], states.TRANSITIONS["new-op"]
        self.assertEqual((perf["FLOOR"] - {"FLOOR", "HIR", "REQ", "STOPPED"}, new_op["FLOOR"] - perf["FLOOR"]),
                         ({"IMPL"}, {"OP"}))
        self.assertNotIn("OP", perf)
        # Back to a rework state behind, never ahead; REVIEW reaches PR once the PR is published.
        self.assertTrue({"HIR", "FLOOR", "OP", "IMPL", "CLEAN", "DONE"} <= new_op["CI"])
        self.assertNotIn("CLEAN", new_op["HIR"])
        self.assertIn("PR", new_op["REVIEW"])
        self.assertNotIn("DONE", new_op)
        self.assertEqual(states.TRANSITIONS["extend"], new_op)
        meta = json.loads((self.dir / "round.json").read_text())
        (self.dir / "round.json").write_text(json.dumps({**meta, "kind": "general"}))
        with self.assertRaisesRegex(SystemExit, "a general round has no states"):
            self.move("HIR")

    def test_each_edge_runs_its_checks(self) -> None:
        self.assertEqual(states.checks("FLOOR", "IMPL"), (gate.check_floor,))
        self.assertEqual(states.checks("CLEAN", "IMPL"), ())
        self.assertEqual(states.checks("IMPL", "STOPPED"), (gate.check_stop_records,))
        rule = mock.Mock(side_effect=gate.GateError("the new rule"))
        with mock.patch.dict(states.CHECKS, {("REQ", "HIR"): (rule,)}), \
                self.assertRaisesRegex(SystemExit, "REQ -> HIR: FAIL: the new rule"):
            self.move("HIR")
        self.assertEqual((rule.call_args.args[0].dir, rule.call_args.args[0].kind), (self.dir, "new-op"))
        self.move("HIR")
        with self.assertRaisesRegex(SystemExit, "HIR -> FLOOR: FAIL: .*provenance.json"):
            self.move("FLOOR")
        (self.dir / "check.json").write_text("{}")
        (self.dir / "provenance.json").write_text(json.dumps(
            {"reference_check": {"command": "tilefoundry check h.py", "report": "check.json"}}))
        self.move("FLOOR")
        with self.assertRaisesRegex(SystemExit, "FLOOR -> STOPPED: FAIL: .*report.md"):
            self.move("STOPPED")
        (self.dir / "report.md").write_text("## Stop records\n\nNo comparator.\n")
        self.move("STOPPED")
        with self.assertRaisesRegex(SystemExit, "cannot move from STOPPED to REQ; it can move to nothing"):
            self.move("REQ")

    def test_a_move_is_a_ledger_event_and_status_shows_the_state(self) -> None:
        self.move("HIR")
        event = list(ledger.events())[-1]
        self.assertEqual({key: event[key] for key in ("event", "round", "from", "to", "reason", "commit")},
                         {"event": "transition", "round": "x-r1", "from": "REQ", "to": "HIR", "reason": "the reason",
                          "commit": git(self.repo, "rev-parse", "HEAD")})
        view = ledger.rounds()["x-r1"]
        self.assertEqual((view["phase"], view["state"], "reason" in view), ("HIR", "starting", False))
        with mock.patch("topa.sh.herdr", return_value={}):
            self.assertEqual([row["phase"] for row in round_status(self.config)], ["HIR"])

    def test_review_reaches_pr_only_once_the_pr_is_published(self) -> None:
        ledger.append("transition", round="x-r1", **{"from": "CLEAN", "to": "REVIEW"})
        with self.assertRaisesRegex(SystemExit, "REVIEW -> PR: FAIL: the PR is not published yet; move to FINAL"):
            self.move("PR")
        ledger.append("pr", round="x-r1", pr="https://github.com/tile-ai/TileOPs/pull/1")
        self.assertEqual(self.move("PR")["to"], "PR")

    def test_pr_needs_state_pr_for_a_foundry_round_only(self) -> None:
        with self.assertRaisesRegex(SystemExit, "the round is in state REQ"):
            pr.open_pr(self.config, "x-r1")
        meta = json.loads((self.dir / "round.json").read_text())
        (self.dir / "round.json").write_text(json.dumps({**meta, "kind": "general"}))
        with mock.patch("topa.pr.gate", side_effect=SystemExit("gated")), self.assertRaisesRegex(SystemExit, "gated"):
            pr.open_pr(self.config, "x-r1")


class EnvironmentTests(unittest.TestCase):
    def test_pane_drops_claude_variables_only(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "KEEP": "1"}
        for shell in ("sh", "bash"):
            out = subprocess.run([shell, "-c", f"{CLEAR_CLAUDE_ENV}; env"], env=env,
                                 capture_output=True, text=True, check=True).stdout
            self.assertNotIn("CLAUDE", out, shell)
            self.assertIn("KEEP=1", out, shell)

    def test_own_files_are_not_foreign(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "work").mkdir()
            (root / "work" / "a.py").write_text("")
            self.assertFalse(foreign_files([root, root / "missing"]))


DIFF = """diff --git a/tests/t.py b/tests/t.py
--- a/tests/t.py
+++ b/tests/t.py
@@ -10,3 +10,3 @@ def f():
 keep
---x
+++x
 keep
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x
diff --git a/old.py b/new.py
similarity index 50%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -1 +1 @@
-x
+y
diff --git a/docs/a b.md b/docs/a b.md
--- a/docs/a b.md	
+++ b/docs/a b.md	
@@ -1 +1 @@
-x
+y
"""


class ReviewTests(unittest.TestCase):
    def test_checklists_follow_changed_paths(self) -> None:
        names = lambda paths: [path.name for path in review.checklists(paths)]
        self.assertEqual(names(["src/tileops/elementwise.py", "scripts/a.py"]), ["general.md"])
        self.assertEqual(names(["src/tileops/_kernels/gemm/a.py"]), ["general.md", "ops.md", "kernels.md"])
        self.assertEqual(names(["src/tileops/_ops/gemm.py"]), ["general.md", "ops.md"])
        self.assertEqual(names(["benchmarks/tests/a.py"]), ["general.md", "tests.md", "benchmarks.md"])
        self.assertEqual(names(["workloads/a.py", "src/tileops/manifest/spec/a.yaml", "README.md"]),
                         ["general.md", "tests.md", "manifest.md", "docs.md"])
        every = ["src/tileops/csrc/a.h", "tests/a.py", "benchmarks/ops/a.py", "src/tileops/manifest/a.py", "docs/a.md"]
        self.assertEqual(names(every), ["general.md", "ops.md", "kernels.md", "tests.md", "benchmarks.md",
                                       "manifest.md", "docs.md"])
        self.assertTrue(all(path.is_file() for path in review.checklists(every)))

    def test_comments_anchor_on_hunk_lines_only(self) -> None:
        self.assertEqual(review.commentable(DIFF), {
            ("tests/t.py", "RIGHT", 10), ("tests/t.py", "RIGHT", 11), ("tests/t.py", "RIGHT", 12),
            ("tests/t.py", "LEFT", 10), ("tests/t.py", "LEFT", 11), ("tests/t.py", "LEFT", 12),
            ("gone.py", "LEFT", 1), ("new.py", "LEFT", 1), ("new.py", "RIGHT", 1),
            ("docs/a b.md", "LEFT", 1), ("docs/a b.md", "RIGHT", 1)})

    def test_select_refuses_lines_off_the_diff(self) -> None:
        with self.assertRaisesRegex(SystemExit, "gone.py:1"):
            review.select([{"path": "gone.py", "line": 1, "body": "x"}], review.commentable(DIFF), [])

    def test_select_skips_comments_topa_posted(self) -> None:
        posted = [{"path": "tests/t.py", "body": "Old  finding.\n\n" + review.MARKER},
                  {"path": "tests/t.py", "body": "A person said this."}]
        comments = [{"path": "tests/t.py", "line": 11, "body": "Old finding."},
                    {"path": "tests/t.py", "line": 12, "body": "A person said this."},
                    {"path": "gone.py", "line": 1, "side": "LEFT", "body": "Old finding."}]
        fresh = review.select(comments, review.commentable(DIFF), posted)
        self.assertEqual([(c["path"], c["body"]) for c in fresh],
                         [("tests/t.py", "A person said this."), ("gone.py", "Old finding.")])


class HistoryTests(unittest.TestCase):
    def test_prs_match_on_op_benchmark_or_workload_newest_first(self) -> None:
        prs = [{"number": 2172, "title": "[Perf][GEMM] a", "state": "CLOSED", "body": "`bench_gemm` on qwen2.5-7b-b64"},
               {"number": 2834, "title": "[Perf][GEMM] b", "state": "MERGED", "body": "GEMMFwdOp rows"},
               {"number": 1567, "title": "[Perf][GroupedGemm] c", "state": "CLOSED", "body": "bench_grouped_gemm"},
               {"number": 2900, "title": "[Feat] GEMMFwdOp knobs", "state": "OPEN", "body": None}]
        prs += [{"number": 2950, "title": "[Perf][foundry][GEMM] d", "state": "MERGED",
                 "body": "- bench_gemm tile\n\nOp: GEMMFwdOp\nBenchmark: benchmarks.ops.bench_gemm\nResult: below bar"},
                {"number": 2960, "title": "[Perf][foundry][GEMM] e", "state": "MERGED",
                 "body": "- GEMMFwdOp shares it\n\nOp: BMMFwdOp\nResult: bar met"},
                {"number": 2970, "title": "[Perf][foundry][GEMM] f", "state": "OPEN",
                 "body": "- x\r\n\r\nOp: GEMMFwdOp \t\r\nResult: bar met\r\n"}]
        found = history.matching_prs(prs, "GEMMFwdOp", "benchmarks.ops.bench_gemm", ["qwen2.5-7b-b64-bfloat16"])
        self.assertEqual([(pr["number"], pr["matched"], pr["result"]) for pr in found],
                         [(2970, ["Op line"], "bar met"), (2950, ["Op line"], "below bar"), (2834, ["op"], None),
                          (2172, ["benchmark", "workload qwen2.5-7b-b64"], None)])
        self.assertIn("merged, below bar: [Perf][foundry][GEMM] d", history.render(
            {"op": "GEMMFwdOp", "pull_requests": found, "search_error": None, "local": [], "local_scope": "here"}))

    def test_local_attempts_skip_other_ops_and_malformed_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            rounds = Path(folder)
            for slug, op, files in [
                ("gemm-r1", "GEMMFwdOp", {"report.md": "## Stop records\n" + "x" * 2000 + "\n## Other\n",
                                          "provenance.json": '{"result": "below bar"}'}),
                ("gemm-r2", "GEMMFwdOp", {"provenance.json": "null"}),
                ("gemm-r3", "GEMMFwdOp", {}),
                ("softmax-r1", "SoftmaxFwdOp", {}),
            ]:
                (rounds / slug).mkdir()
                for name, text in {"round.json": json.dumps({"round": slug, "op": op}), **files}.items():
                    (rounds / slug / name).write_text(text)
            for slug, text in [("broken", "{"), ("listed", "[]")]:
                (rounds / slug).mkdir()
                (rounds / slug / "round.json").write_text(text)
            found = {a["round"]: a for a in history.local_attempts(rounds, "GEMMFwdOp", "tile-ai/TileOPs", "gemm-r3")}
        self.assertEqual(sorted(found), ["gemm-r1", "gemm-r2"])
        self.assertEqual(found["gemm-r1"]["result"], "below bar")
        self.assertTrue(found["gemm-r1"]["excerpts"]["Stop records"].endswith("[...]"))
        self.assertEqual((found["gemm-r2"]["result"], found["gemm-r2"]["report"]), (None, None))

    def test_render_is_bounded_and_says_when_the_search_failed(self) -> None:
        error = history.sh.CommandError(["gh"], 1, "HTTP 502")
        with tempfile.TemporaryDirectory() as folder, mock.patch("topa.config.HOME", Path(folder)), \
                mock.patch("topa.history.sh.run", side_effect=error):
            failed = history.collect(defaults(), "AOp", "benchmarks.ops.bench_a", [], exclude="x")
        self.assertIn("PR search unavailable: HTTP 502", history.render(failed))
        prs = [{"number": n, "title": "[Perf] t", "state": "MERGED", "url": "u", "matched": ["op"]}
               for n in range(20, 0, -1)]
        prs[0]["matched"] = [f"workload w{i}" for i in range(2000)]
        prs[1]["title"] = "[Perf] " + "t" * 20000
        text = history.render(failed | {"search_error": None, "search_truncated": True, "pull_requests": prs})
        self.assertEqual(text.count("\n- [#"), history.MAX_PRS)
        self.assertIn(f"{20 - history.MAX_PRS} older matches are in `prior-attempts.json`", text)
        shown = history.MAX_REASONS
        self.assertIn(f"workload w{shown - 1}, {2000 - shown} more)", text)
        self.assertIn(f"stopped at GitHub's {history.SEARCH_CAP} results", text)
        self.assertIn("No earlier round of this op", text)
        self.assertLessEqual(len(text), history.MAX_SECTION + 100)


class GpuTests(unittest.TestCase):
    def test_an_exclusive_card_is_unusable_only_while_a_process_computes_on_it(self):
        modes = [["0", "Default"], ["1", "Exclusive_Process"], ["2", "Exclusive_Process"], ["3", "Prohibited"]]
        with mock.patch("topa.gpu._query", return_value=modes):
            self.assertEqual(gpu.unusable(defaults(), []), {3})
            self.assertEqual(gpu.unusable(defaults(), [(0, "7"), (1, "8")]), {1, 3})
            self.assertEqual(gpu.unusable(defaults(gpu_exclude=(0,)), []), {0, 3})


if __name__ == "__main__":
    unittest.main()
