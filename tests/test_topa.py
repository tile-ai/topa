from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from topa import config as config_module, gate, ledger, review
from topa.grade import BEHIND, grade_op, parse
from topa.hook import decide
from topa.issue import Issue, branch_name, round_slug
from topa.container import foreign_files
from topa.round import CLEAR_CLAUDE_ENV, kit, render

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


class HookTests(unittest.TestCase):
    def test_routing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            round_dir = Path(folder)
            (round_dir / "round.json").write_text('{"alias": "topa-i1-r1"}')
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
            ):
                routed = decide(event(command), round_dir)
                assert routed is not None, command
                self.assertTrue(routed["hookSpecificOutput"]["updatedInput"]["command"].startswith("ssh -T topa-i1-r1 "))


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
diff --git a/docs/a b.md b/docs/a b.md
--- a/docs/a b.md	
+++ b/docs/a b.md	
@@ -1 +1 @@
-x
+y
diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"
--- "a/docs/caf\\303\\251.md"
+++ "b/docs/caf\\303\\251.md"
@@ -0,0 +1 @@
+x
"""


class ReviewHelperTests(unittest.TestCase):
    def test_checklists_follow_changed_paths(self) -> None:
        names = lambda paths: [path.name for path in review.checklists(paths)]
        self.assertEqual(names(["src/tileops/_ops/a.py"]), ["general.md"])
        self.assertEqual(names(["workloads/a.py", "src/tileops/manifest/spec/a.yaml", "README.md"]),
                         ["general.md", "tests.md", "manifest.md", "docs.md"])
        self.assertTrue(all(path.is_file() for path in review.checklists(["tests/a.py", "docs/a.md"])))

    def test_comments_anchor_on_hunk_lines_only(self) -> None:
        self.assertEqual(review.commentable(DIFF), {
            ("tests/t.py", "RIGHT", 10), ("tests/t.py", "RIGHT", 11), ("tests/t.py", "RIGHT", 12),
            ("tests/t.py", "LEFT", 10), ("tests/t.py", "LEFT", 11), ("tests/t.py", "LEFT", 12),
            ("gone.py", "LEFT", 1), ("docs/caf\u00e9.md", "RIGHT", 1),
            ("docs/a b.md", "LEFT", 1), ("docs/a b.md", "RIGHT", 1)})

    def test_a_round_is_due_on_any_change(self) -> None:
        seen = {"head": "a", "text": "t", "comment": 1, "review_comment": 2, "review": 3}
        self.assertEqual(review.trigger(None, seen), "first round")
        self.assertIsNone(review.trigger(seen, dict(seen)))
        for key in seen:
            self.assertIsNotNone(review.trigger(seen, {**seen, key: "changed"}), key)

    def test_only_people_other_than_topa_count(self) -> None:
        items = [{"id": 1, "type": "User", "body": "a"}, {"id": 2, "type": "Bot", "body": "b"},
                 {"id": 3, "type": "User", "body": "c\n\n" + review.MARKER}, {"id": 4, "type": "User", "body": ""}]
        self.assertEqual([c["id"] for c in review.by_others(items)], [1])

    def test_select_drops_comments_topa_posted(self) -> None:
        posted = [{"path": "tests/t.py", "body": "Old  finding.\n\n" + review.MARKER},
                  {"path": "tests/t.py", "body": "A person said this."}]
        comments = [{"path": "tests/t.py", "line": 11, "body": "Old finding."},
                    {"path": "tests/t.py", "line": 12, "body": "A person said this."},
                    {"path": "gone.py", "line": 1, "side": "LEFT", "body": "New."},
                    {"path": "gone.py", "line": 1, "side": "LEFT", "body": "New."}]
        fresh, repeated = review.select(comments, review.commentable(DIFF), posted)
        self.assertEqual(([c["body"] for c in fresh], repeated), (["A person said this.", "New."], 2))

    def test_select_refuses_lines_off_the_diff(self) -> None:
        with self.assertRaisesRegex(SystemExit, "gone.py:1 RIGHT"):
            review.select([{"path": "gone.py", "line": 1, "body": "x"}], review.commentable(DIFF), [])


class ReviewCommandTests(unittest.TestCase):
    """`next` and `post` against a fake PR; git and GitHub are replaced."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        values = {name: config_module._coerce(name, value) for name, value in config_module.DEFAULTS.items()}
        self.config = config_module.Config(**values)
        self.view = {"title": "[Test] t", "body": "b", "state": "OPEN", "author": {"login": "a"},
                     "baseRefName": "main", "headRefOid": "h1", "url": "u"}
        self.posted: list[dict] = []
        self.payloads: list[dict] = []
        self.released: list[int] = []
        snapshot = lambda config, pr: (dict(self.view), {"head": self.view["headRefOid"], "text": self.view["body"],
                                                         "comment": 0, "review_comment": 0, "review": 0}, [])
        fakes = {
            "_snapshot": snapshot,
            "_checkout": lambda config, pr, base, worktree: (self.view["headRefOid"], "b0"),
            "_paths": lambda config, base, head: ["tests/t.py"],
            "_diff": lambda config, old, new: DIFF,
            "_threads": lambda config, pr, items: [],
            "_checks": lambda config, pr: "[]",
            "_release": lambda config, pr, worktree: self.released.append(pr),
            "_list": lambda config, pr, kind: [c for c in self.posted if c["kind"] == kind],
            "_submit": lambda config, pr, payload: self.payloads.append(payload) or "review-url",
        }
        for name, fake in fakes.items():
            patcher = mock.patch.object(review, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(config_module, "HOME", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def findings(self, result: dict, summary: str = "s", comments: list | None = None) -> None:
        Path(result["dir"], "findings.json").write_text(json.dumps({"summary": summary, "comments": comments or []}))

    def test_next_prepares_a_round_again_and_keeps_its_findings(self) -> None:
        first = review.next_round(self.config, 7)
        self.assertEqual((first["state"], first["round"], Path(first["dir"]).parent.parent.name),
                         ("review", 1, "TileOPs"))
        self.findings(first)
        self.assertEqual(review.next_round(self.config, 7), first)
        self.assertTrue(Path(first["dir"], "findings.json").is_file())

    def test_next_is_idle_after_post_until_the_pr_changes(self) -> None:
        self.findings(review.next_round(self.config, 7))
        review.post(self.config, 7)
        self.assertEqual(review.next_round(self.config, 7)["state"], "idle")
        self.view["headRefOid"] = "h2"
        again = review.next_round(self.config, 7)
        self.assertEqual((again["round"], again["trigger"]), (2, "new commits"))
        self.assertTrue(Path(again["dir"], "since-last.diff").is_file())

    def test_next_releases_a_closed_pr(self) -> None:
        self.view["state"] = "CLOSED"
        self.assertEqual(review.next_round(self.config, 7)["state"], "closed")
        self.assertEqual(self.released, [7])

    def test_next_stops_at_the_round_limit(self) -> None:
        self.findings(review.next_round(self.config, 7))
        review.post(self.config, 7)
        self.view["headRefOid"] = "h2"
        self.assertEqual(review.next_round(self.config, 7, max_rounds=1)["state"], "limit")

    def test_post_submits_one_comment_review_on_the_round_head(self) -> None:
        self.findings(review.next_round(self.config, 7), comments=[{"path": "tests/t.py", "line": 11, "body": "x"}])
        self.view["headRefOid"] = "h2"
        self.assertEqual(review.post(self.config, 7)["review"], "review-url")
        self.assertEqual(self.payloads, [{
            "commit_id": "h1", "event": "COMMENT", "body": "s\n\n" + review.MARKER,
            "comments": [{"path": "tests/t.py", "line": 11, "side": "RIGHT", "body": "x\n\n" + review.MARKER}]}])

    def test_post_skips_a_repeated_summary_with_no_new_comment(self) -> None:
        self.posted.append({"kind": "review", "id": 1, "body": "s\n\n" + review.MARKER})
        self.findings(review.next_round(self.config, 7))
        self.assertIsNone(review.post(self.config, 7)["review"])
        self.assertEqual(self.payloads, [])
        self.assertEqual(review.next_round(self.config, 7)["state"], "idle")

    def test_post_requests_changes_even_when_nothing_is_new(self) -> None:
        self.posted.append({"kind": "review", "id": 1, "body": "s\n\n" + review.MARKER})
        self.findings(review.next_round(self.config, 7))
        review.post(self.config, 7, request_changes=True)
        self.assertEqual([p["event"] for p in self.payloads], ["REQUEST_CHANGES"])

    def test_post_refuses_a_round_whose_preparation_failed(self) -> None:
        self.findings(review.next_round(self.config, 7))
        self.view["headRefOid"] = "h2"
        with mock.patch.object(review, "_threads", side_effect=RuntimeError("gh failed")):
            with self.assertRaises(RuntimeError):
                review.next_round(self.config, 7)
        with self.assertRaisesRegex(SystemExit, "no prepared round"):
            review.post(self.config, 7)

    def test_post_refuses_a_round_of_another_repository(self) -> None:
        self.findings(review.next_round(self.config, 7))
        other = dataclasses.replace(self.config, upstream="someone/TileOPs")
        with self.assertRaisesRegex(SystemExit, "no prepared round"):
            review.post(other, 7)


class ReviewGitHubTests(unittest.TestCase):
    """The GitHub reads, with `gh` replaced at the subprocess boundary."""

    def setUp(self) -> None:
        values = {name: config_module._coerce(name, value) for name, value in config_module.DEFAULTS.items()}
        self.config = config_module.Config(**values)
        self.calls: list[list[str]] = []

    def gh(self, outputs: dict[str, list[dict]]):
        """A fake `sh.run` printing, for the endpoint a call names, one JSON line per item."""
        def run(args, **kwargs):
            self.calls.append([str(arg) for arg in args])
            for key, items in outputs.items():
                if any(key in str(arg) for arg in args):
                    return "\n".join(json.dumps(item) for item in items)
            return ""
        return mock.patch.object(review.sh, "run", side_effect=run)

    def item(self, number: int, body: str = "b", **fields) -> dict:
        return {"id": number, "user": "u", "type": "User", "body": body, "path": None, "line": None,
                "reply_to": None, "url": "u", **fields}

    def test_snapshot_keeps_one_watermark_per_kind_over_every_page(self) -> None:
        view = {"title": "t", "body": "b", "headRefOid": "h"}
        outputs = {"issues/7/comments": [self.item(5), self.item(9, review.MARKER)],
                   "pulls/7/comments": [self.item(3, path="a.py")],
                   "pulls/7/reviews": [self.item(4), self.item(8, "")]}
        with self.gh(outputs), mock.patch.object(review.sh, "gh_json", return_value=view):
            _, now, items = review._snapshot(self.config, 7)
        self.assertEqual((now["comment"], now["review_comment"], now["review"], len(items)), (5, 3, 4, 5))
        self.assertTrue(all("--paginate" in call for call in self.calls))

    def test_threads_hold_every_reply_of_an_unresolved_root(self) -> None:
        items = [{**self.item(1, "first\n\n" + review.MARKER, path="a.py", line=3), "kind": "review_comment"},
                 {**self.item(2, "reply", reply_to=1), "kind": "review_comment"},
                 {**self.item(3, "other", path="b.py"), "kind": "review_comment"}]
        with self.gh({"graphql": [{"outdated": False, "root": 1}]}):
            threads = review._threads(self.config, 7, items)
        self.assertEqual([(t["path"], t["topa"], [c["body"] for c in t["comments"]]) for t in threads],
                         [("a.py", True, ["first", "reply"])])
        self.assertIn("--paginate", self.calls[0])

    def test_checkout_fetches_head_and_base_and_reuses_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            worktree = Path(folder, "worktree")
            with mock.patch.object(review.sh, "github_remote", return_value="up"), \
                    mock.patch.object(review.sh, "run", side_effect=lambda args, **kw: self.calls.append(
                        [str(a) for a in args]) or "c"):
                review._checkout(self.config, 7, "dev", worktree)
                (worktree / ".git").mkdir(parents=True)
                review._checkout(self.config, 7, "dev", worktree)
        fetch, merge_base = self.calls[0], self.calls[4]
        self.assertEqual(fetch[-2:], ["+pull/7/head:refs/topa/review/7/head", "+refs/heads/dev:refs/topa/review/7/base"])
        self.assertEqual(merge_base[-3:], ["merge-base", "refs/topa/review/7/base", "c"])
        self.assertEqual((self.calls[3][3:5], self.calls[7][3:5]), (["worktree", "add"], ["reset", "--quiet"]))

    def test_release_removes_the_worktree_and_refs(self) -> None:
        with mock.patch.object(review.sh, "ok", return_value=True) as ok, \
                mock.patch.object(review.sh, "run", side_effect=lambda args, **kw: self.calls.append(
                    [str(a) for a in args]) or ""):
            review._release(self.config, 7, Path(__file__))
        self.assertEqual(ok.call_args.args[0][3:5], ["worktree", "remove"])
        self.assertEqual([c[-1] for c in self.calls if "update-ref" in c],
                         ["refs/topa/review/7/head", "refs/topa/review/7/base"])

    def test_changed_paths_keep_names_with_newlines(self) -> None:
        with mock.patch.object(review.sh, "run", return_value="a.py\0docs/two\nlines.md\0") as run:
            self.assertEqual(review._paths(self.config, "b", "h"), ["a.py", "docs/two\nlines.md"])
        self.assertIn("-z", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
