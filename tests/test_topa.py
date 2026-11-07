from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from topa import config, gate, history, ledger, review, wiki
from topa.container import foreign_files
from topa.hook import decide
from topa.round import CLEAR_CLAUDE_ENV, _issue_work, brief, drop_branch, kit, render, round_slug, start

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
    text = (kit() / kind / name).read_text()
    title = "[Perf][foundry][Test] Schedule the kernel" if kind == "perf" else "[BugFix][Norm] Keep the row mean"
    text = re.sub(r"^# \[.*", f"# {title}", text)
    text = re.sub(r"<work/final_hir\.py[^\n]*>", HIR.rstrip("\n"), text)
    text = text.replace("sha256:<image digest>", "sha256:" + "a" * 64)
    text = text.replace("**<bar met | below bar>**", f"**{result}**").replace("<bar met | below bar>", result)
    text = text.replace("Op: <Op>", "Op: AFwdOp").replace("<benchmark module>", "benchmarks.ops.bench_a")
    text = re.sub(r"^<[^\n]*>$", "1", text, flags=re.MULTILINE)
    return gate.PLACEHOLDER.sub("1", text)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


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

    def test_unfilled_template_fails(self) -> None:
        for name in ("pr-template.md", "pr-comments.md"):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                (self.round / name).write_text((kit() / "perf" / name).read_text())
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

    def test_start_claims_one_round_per_op(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("topa.ledger.LEDGER", Path(folder, "ledger.jsonl")), \
                mock.patch("topa.sh.require"), mock.patch("topa.sh.github_remote"), \
                mock.patch("topa.round.topa_binary"):
            plan = start(defaults(), "SoftmaxFwdOp", page=page(), dry_run=True)
            self.assertEqual((plan["round"], plan["branch"]), ("softmaxfwd-r1", "perf/reduction/softmaxfwd-r1"))
            ledger.append("create", round="softmaxfwd-r1", op="SoftmaxFwdOp")
            with self.assertRaisesRegex(SystemExit, "round softmaxfwd-r1 already works SoftmaxFwdOp"):
                start(defaults(), "SoftmaxFwdOp", page=page(), dry_run=True)
            with self.assertRaisesRegex(SystemExit, "is not in the queue"):
                start(defaults(), "NoSuchFwdOp", page=page(), dry_run=True)

    def test_brief_renders_every_placeholder(self) -> None:
        for kind in ("perf", "general"):
            template = (kit() / "brief.md").read_text() + (kit() / kind / "brief.md").read_text()
            text = brief(kind, {name: name for name in re.findall(r"\{\{([A-Z_]+)\}\}", template)})
            self.assertNotIn("{{", text)
            self.assertEqual("### 1. Requirement and comparators" in text, kind == "perf")
        with self.assertRaises(SystemExit):
            render("{{A}} {{B}}", {"A": 1})

    def test_issue_sets_the_kind_and_the_requirement(self) -> None:
        issues = {
            2812: {"number": 2812, "title": "[BUG][Norm] LayerNorm loses precision", "body": "## Repro\nx = 1e36",
                   "labels": [{"name": "bug"}], "url": "https://github.com/tile-ai/TileOPs/issues/2812", "state": "OPEN"},
            2791: {"number": 2791, "title": "[Perf][GEMM] Close the back-to-back gap", "body": "Rows: `m4096`",
                   "labels": [], "url": "https://github.com/tile-ai/TileOPs/issues/2791", "state": "OPEN"},
        }
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("topa.ledger.LEDGER", Path(folder, "ledger.jsonl")), \
                mock.patch("topa.sh.require"), mock.patch("topa.sh.github_remote"), \
                mock.patch("topa.round.topa_binary"), \
                mock.patch("topa.sh.gh_json", side_effect=lambda *args: issues[int(args[2])]):
            general = start(defaults(), issue=2812, dry_run=True)
            perf = start(defaults(), issue=2791, dry_run=True)
            self.assertEqual((general["kind"], general["branch"]), ("general", "fix/issue/issue-2812-r1"))
            self.assertEqual((perf["kind"], perf["branch"]), ("perf", "perf/issue/issue-2791-r1"))
            ledger.append("create", **general)
            with self.assertRaisesRegex(SystemExit, "already works issue #2812"):
                start(defaults(), issue=2812, dry_run=True)
            values = {name: name for name in ("SLUG", "ROUND_DIR", "UPSTREAM", "BRANCH", "TILEOPS_BASE",
                                               "TILEOPS_WORKTREE", "TILEFOUNDRY_BASE", "TILEFOUNDRY_SOURCE",
                                               "KNOWLEDGE", "TOPA", "PRIOR_ATTEMPTS", "REVIEWER")}
            text = brief("general", values | _issue_work(defaults(), 2812)[1])
            self.assertIn("> ## Repro\n> x = 1e36", text)
            self.assertIn("`Closes #2812`", text)
            text = brief("perf", values | _issue_work(defaults(), 2791)[1])
            self.assertIn("`Refs #2791`", text)
            self.assertIn("copy their benchmark names into `workloads`", text)

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

    def test_ledger_fold(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "ledger.jsonl")
            for event, fields in (("create", {"pane": "w1:p1"}), ("start", {}), ("say", {"text": "hi"})):
                ledger.append(event, path, round="x-r1", **fields)
            view = ledger.rounds(path)["x-r1"]
        self.assertEqual((view["state"], view["pane"], "text" in view), ("running", "w1:p1", False))


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


if __name__ == "__main__":
    unittest.main()
