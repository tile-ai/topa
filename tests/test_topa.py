from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from topa import config as config_module
from topa import container, gate, ledger
from topa.grade import BEHIND, grade_op, parse
from topa.issue import Issue, branch_name, round_slug
from topa.round import kit, render

HIR = 'class Operator:\n    def kernel(self, x):\n        return tf.schedule((x,), op=T.cuda.sm90.Wgmma(n=16))\n'
OTHER_HIR = "class Operator:\n    def kernel(self, x):\n        return x\n"


def filled_template(classification: str) -> str:
    """The kit's PR template with every placeholder filled, as a round fills it."""
    text = (kit() / "pr-template.md").read_text()
    text = "# [Perf][foundry][Test] Schedule the kernel\n" + text.split("\n", 1)[1]
    text = re.sub(r"<work/final_hir\.py[^\n]*>", HIR.rstrip("\n"), text)
    for placeholder, value in {
        "sha256:<image digest>": "sha256:" + "a" * 64,
        "<external stack>": "fa3",
        "<marker>": "&#x1F7E2;",
        "**<measured SOTA | improvement without SOTA>**": f"**{classification}**",
        "`<exact public TileOPs Op name>`": "`TestFwdOp`",
    }.items():
        text = text.replace(placeholder, value)
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
            "work/a.py": OTHER_HIR, "work/final_hir.py": HIR, "work/twin.py": "twin\n",
            "e/check.json": '{"passed": true}', "e/a.json": "{\"ok\": 1}", "e/b.json": "{\"ok\": 1}", "e/log": "ok\n",
            "report.md": "ok\n", "findings.json": '{"findings": [], "issue": null}',
            "pr-template.md": filled_template("improvement without SOTA"),
        }
        for name, text in files.items():
            (self.round / name).parent.mkdir(parents=True, exist_ok=True)
            (self.round / name).write_text(text)

        def iteration(hir: str, report: str, verdict: str) -> dict:
            return {"name": hir, "hir": hir, "hypothesis": "h", "placement": "p", "measured_ms": 1.0,
                    "verdict": verdict, "analysis": {"command": f"tilefoundry analyze {hir} --json", "report": report}}

        run = {"command": "pytest x", "report": "e/log"}
        self.provenance = {
            "tileops_base": self.base, "tilefoundry_base": "b" * 40,
            "classification": "improvement without SOTA", "final_hir": "work/final_hir.py",
            "runtime_twin": "work/twin.py", "production_kernel": "src/tileops/kernels/k.py:K",
            "tilefoundry_check": {"command": "tilefoundry check work/twin.py", "report": "e/check.json"},
            "iterations": [iteration("work/a.py", "e/a.json", "rejected"),
                           iteration("work/final_hir.py", "e/b.json", "kept")],
            "decisions": [{"analysis_fact": "a", "hir_change": "h", "kernel_change": "k"}],
            "correctness": run, "benchmark": run, "profile": run,
        }
        self.write_provenance()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_provenance(self) -> None:
        (self.round / "provenance.json").write_text(json.dumps(self.provenance))

    def test_complete_round_passes(self) -> None:
        self.assertEqual(gate.validate_round(self.round, self.repo)["classification"], "improvement without SOTA")

    def pr(self, edit) -> None:
        """Rewrite the round's filled-in PR template through *edit*."""
        path = self.round / "pr-template.md"
        path.write_text(edit(path.read_text()))

    def test_unfilled_template_fails(self) -> None:
        (self.round / "pr-template.md").write_text((kit() / "pr-template.md").read_text())
        with self.assertRaisesRegex(gate.GateError, "placeholder"):
            gate.validate_round(self.round, self.repo)

    def test_each_defect_fails(self) -> None:
        finding = {"id": "f", "classification": "c", "command": "c", "expected": "e", "actual": "a",
                   "workaround_cost": "w", "public_surface": "p", "affected_workloads": [], "reproducer": "e/log"}
        cases = {
            "at least two": lambda: self.provenance.update(iterations=self.provenance["iterations"][1:]),
            "one tile-ai/TileFoundry issue": lambda: (self.round / "findings.json").write_text(
                json.dumps({"findings": [finding], "issue": "https://github.com/tile-ai/TileFoundry/pull/1"})),
            "null when there is no finding": lambda: (self.round / "findings.json").write_text(
                '{"findings": [], "issue": "https://github.com/tile-ai/TileFoundry/issues/1"}'),
            "exact final HIR": lambda: (self.round / "work/final_hir.py").write_text(HIR + "# changed\n"),
            "title as a `# ` line": lambda: self.pr(lambda t: t.split("\n", 1)[1]),
            "placeholder <benchmark>": lambda: self.pr(lambda t: t.replace("Method: 1", "Method: <benchmark>")),
            "Operator: `<Op name>`": lambda: self.pr(lambda t: t.replace("`TestFwdOp`", "TestFwdOp")),
            "full sha256": lambda: self.pr(lambda t: t.replace("a" * 64, "a" * 12)),
            "geometric mean row": lambda: self.pr(
                lambda t: "\n".join(line for line in t.splitlines() if not line.startswith("| geometric mean"))),
            "no comparison marker": lambda: self.pr(lambda t: t.replace("&#x1F7E2;", "")),
            "classification of provenance.json": lambda: self.pr(
                lambda t: t.replace("**improvement without SOTA**", "**measured SOTA**")),
            "forbidden paths": lambda: (Path(self.repo, "bench.py").write_text("x\n"),
                                        git(self.repo, "add", "."), git(self.repo, "commit", "-qm", "b")),
        }
        for message, defect in cases.items():
            with self.subTest(message):
                self.tearDown()
                self.setUp()
                defect()
                self.write_provenance()
                with self.assertRaisesRegex(gate.GateError, message):
                    gate.validate_round(self.round, self.repo)


class ContainerTests(unittest.TestCase):
    def test_wrapper_stays_the_claude_herdr_sees(self) -> None:
        config = replace(config_module.load(), docker_host="unix:///run/user/1/docker.sock")
        script = container.agent_wrapper(config, "topa-i1-r1")
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        self.assertIn("export DOCKER_HOST=unix:///run/user/1/docker.sock", script)
        last = script.strip().splitlines()[-1]
        # herdr names a pane's agent by its foreground process: no exec, or docker replaces it.
        self.assertTrue(last.startswith("docker exec --interactive --tty "), last)
        for name in container.HERDR_ENV:
            self.assertIn(f"--env {name} ", last)
        self.assertTrue(last.endswith('--workdir "$PWD" topa-i1-r1 claude "$@"'), last)

    def test_agent_shares_claude_state_but_not_its_code(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / ".claude" / "hooks").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text("{}")
            with mock.patch("shutil.which", return_value="/usr/bin/true"):
                mounts, env, _ = container._agent(home)
            volumes = [mounts[i + 1] for i in range(0, len(mounts), 2)]
            claude = home / ".claude"
            self.assertIn(f"{claude}:{claude}", volumes)
            self.assertIn(f"{claude / 'settings.json'}:{claude / 'settings.json'}:ro", volumes)
            self.assertIn(f"{claude / 'hooks'}:{claude / 'hooks'}:ro", volumes)
            # Only what exists is mounted read-only over the shared directory.
            self.assertFalse(any("plugins" in volume for volume in volumes))
            self.assertIn("/usr/bin/true:/usr/local/bin/claude:ro", volumes)
            self.assertIn(f"HOME={home}", env)
            self.assertIn("IS_SANDBOX=1", env)


class GradeTests(unittest.TestCase):
    def test_rival_named_most_often_and_its_geomean(self) -> None:
        def case(config: str, props: dict) -> str:
            rows = "".join(f'<property name="{k}" value="{v}" />' for k, v in props.items())
            return f'<testcase name="t[{config}]"><properties><property name="op" value="A" />{rows}</properties></testcase>'

        with tempfile.TemporaryDirectory() as folder:
            xml = Path(folder, "b.xml")
            xml.write_text("<testsuites><testsuite>" + "".join((
                case("w1", {"tileops_device_busy_ms": 1, "fla_device_busy_ms": 0.5, "torch-ref_device_busy_ms": 0.1}),
                case("w2", {"tileops_device_busy_ms": 1, "fla_device_busy_ms": 9, "fla_ratio": 0.8}),
                case("w3", {"tileops_device_busy_ms": 1, "torch_device_busy_ms": 0.9, "baseline_device_busy_ms": 0.1}),
            )) + "</testsuite></testsuites>")
            g = grade_op("A", parse(xml))
        self.assertEqual((g.rival, g.status), ("fla", BEHIND))
        self.assertAlmostEqual(g.speedup, (0.5 * 0.8) ** 0.5)


class RoundTests(unittest.TestCase):
    def test_names(self) -> None:
        issue = Issue(7, "[Kernel] GemmFp8FwdOp: x", "open", (), (), "u", 1)
        self.assertEqual(round_slug(7, [{"round": "i7-r1", "issue": 7}]), "i7-r2")
        self.assertEqual(branch_name(issue, "i7-r2", "gemm"), "feat/gemm/i7-r2")

    def test_brief_renders_every_placeholder(self) -> None:
        template = (kit() / "brief.md").read_text()
        text = render(template, {name: name for name in re.findall(r"\{\{([A-Z_]+)\}\}", template)})
        self.assertNotIn("{{", text)
        with self.assertRaises(SystemExit):
            render("{{A}} {{B}}", {"A": 1})

    def test_ledger_fold(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "ledger.jsonl")
            for event, fields in (("create", {"pane": "w1:p1"}), ("start", {}), ("say", {"text": "hi"})):
                ledger.append(event, path, round="i1-r1", **fields)
            view = ledger.rounds(path)["i1-r1"]
        self.assertEqual((view["state"], view["pane"], "text" in view), ("running", "w1:p1", False))


if __name__ == "__main__":
    unittest.main()
