"""`topa round`: start, steer, inspect and stop one agent working one op or one issue.

A round owns five things: a TileOPs worktree on its own branch, a herdr workspace
whose pane runs the agent, a detached TileFoundry checkout, a container with one
GPU, and its round directory under `~/.topa/rounds/<slug>/`. `round.json` in that
directory names all of them; the ledger records what happened to them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from topa import container, foundry, history, ledger, review, sh, wiki
from topa.config import Config

PROMPT = ("Read `brief.md` in the current working directory and complete it. A human may "
          "reach you with `topa round say {slug} \"...\"`; treat that as a new instruction.")
SAY_LIMIT = 4000
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


# A TileOPs issue whose title starts with `[Perf]` gets the perf contract.
PERF_ISSUE = re.compile(r"\[perf\]", re.IGNORECASE)
# TileOPs `scripts/ci/types.sh`: the branch prefix of an issue's title type; any other is `fix`.
BRANCH_PREFIX = {"bench": "bench", "benchmark": "bench", "bug": "fix", "chore": "chore", "design": "design",
                 "doc": "doc", "docs": "doc", "feat": "feat", "maintain": "maintain", "meta": "chore",
                 "refactor": "refactor", "style": "style", "test": "test"}


def kit() -> Path:
    return Path(str(files("topa") / "kit" / "round"))


def knowledge() -> Path:
    return Path(str(files("topa") / "knowledge" / "tilelang.md"))


def topa_binary() -> Path:
    """The `topa` of this install, by absolute path: the round's hook and brief call it."""
    path = Path(sys.executable).parent / "topa"
    if not path.exists():
        raise SystemExit(f"{path} does not exist; install topa with `uv pip install -e .`")
    return path


def render(template: str, values: dict[str, Any]) -> str:
    """Fill every `{{NAME}}`; a name with no value is an error, never left in the text."""
    missing = sorted(set(PLACEHOLDER.findall(template)) - set(values))
    if missing:
        raise SystemExit(f"brief placeholders without a value: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)


def round_slug(op: str, earlier: Iterable[dict[str, Any]]) -> str:
    """`<op>-r<k>`: the op in lower case without its `Op` suffix, k one past the slugs taken.

    The stem is cut to 26 characters so the slug fits herdr's agent names,
    `[a-z][a-z0-9_-]{0,31}`. An issue round passes `issue-<N>` as *op*.
    """
    stem = op.lower().removesuffix("op")[:26]
    taken = {view["round"] for view in earlier}
    k = 1
    while f"{stem}-r{k}" in taken:
        k += 1
    return f"{stem}-r{k}"


# --- finding a round ---------------------------------------------------------------


@dataclass(frozen=True)
class Round:
    dir: Path
    meta: dict[str, Any]

    @property
    def slug(self) -> str:
        return self.meta["round"]

    def save(self) -> None:
        (self.dir / "round.json").write_text(json.dumps(self.meta, indent=2) + "\n")


def find(config: Config, slug: str | None = None) -> Round:
    """The round named *slug*, or the one whose directory holds the working directory."""
    if slug:
        path = config.rounds / slug
    else:
        path = next((p for p in (Path.cwd(), *Path.cwd().parents) if (p / "round.json").is_file()), None)
        if path is None:
            raise SystemExit("not inside a round directory; name the round")
    meta_file = path / "round.json"
    if not meta_file.is_file():
        views = ledger.rounds()
        if slug in views:
            return Round(path, dict(views[slug]))
        raise SystemExit(f"no round {slug or path}")
    return Round(path, json.loads(meta_file.read_text()))


# --- herdr ------------------------------------------------------------------------


def _agent(pane: str) -> dict[str, Any]:
    try:
        return sh.herdr("agent", "get", pane).get("agent", {})
    except sh.CommandError:
        return {}


def wait_ready(pane: str, *, timeout: float = 60.0, settle: float = 2.0) -> bool:
    """Wait until Claude Code in *pane* takes input.

    `herdr agent start` returns once it detects the agent, while Claude Code may still
    be drawing its first screen; a prompt pasted then is dropped.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _agent(pane).get("interactive_ready"):
            time.sleep(settle)
            return True
        time.sleep(0.5)
    return False


def submit(pane: str, text: str, *, tries: int = 3, settle: float = 1.5) -> bool:
    """Submit a prompt and confirm a new turn started.

    Proof is a status *transition*: `state_change_seq` moves and the status is
    `working`. A paste can swallow the Enter and leave the text in the input box, so
    Enter is sent again; on an empty box it does nothing. An agent already working
    queues the text without changing state, so that delivery cannot be confirmed.
    """
    before = _agent(pane)
    seq = before.get("state_change_seq")
    sh.herdr("agent", "prompt", pane, text)
    sh.herdr("agent", "send-keys", pane, "enter")
    for attempt in range(tries):
        time.sleep(settle)
        now = _agent(pane)
        if now.get("agent_status") == "working" and now.get("state_change_seq") != seq:
            return True
        if attempt + 1 < tries:
            sh.herdr("agent", "send-keys", pane, "enter")
    if before.get("agent_status") == "working":
        sh.note(f"{pane} was already working; the message is most likely queued. "
                f"Check with: herdr agent read {pane}")
    else:
        sh.note(f"{pane} did not start a new turn; the text may be in its input box. "
                f"Look with: herdr agent attach {pane}")
    return False


# A herdr server started from inside Claude Code hands its CLAUDE* variables to every
# pane. The agent then counts as a child session of that one and saves no transcript.
CLEAR_CLAUDE_ENV = "for v in $(env | sed -n 's/^\\(CLAUDE[A-Za-z0-9_]*\\)=.*/\\1/p'); do unset \"$v\"; done"


def _cd_pane(pane: str, path: Path) -> None:
    marker = f"topa-ready-{time.time_ns()}"
    half = len(marker) // 2
    sh.herdr("pane", "run", pane,
             f"{CLEAR_CLAUDE_ENV}; cd {shlex.quote(str(path))} && printf '%s%s\\n' {marker[:half]} {marker[half:]}")
    sh.run(["herdr", "pane", "wait-output", pane, "--source", "recent-unwrapped",
            "--match", marker, "--timeout", "30000"])


# --- start --------------------------------------------------------------------------


def brief(kind: str, values: dict[str, Any]) -> str:
    """The shared brief with the *kind* section in place of `{{KIND}}`, every placeholder filled."""
    shared = (kit() / "brief.md").read_text()
    return render(shared.replace("{{KIND}}", (kit() / kind / "brief.md").read_text().strip()), values)


def _write_round_dir(config: Config, path: Path, kind: str, values: dict[str, Any], topa: Path) -> None:
    ignore = shutil.ignore_patterns("brief.md", "perf", "general", "__pycache__")
    shutil.copytree(kit(), path, dirs_exist_ok=True, ignore=ignore)
    shutil.copytree(kit() / kind, path, dirs_exist_ok=True, ignore=ignore)
    (path / "brief.md").write_text(brief(kind, values))
    claude = path / ".claude"
    claude.mkdir(exist_ok=True)
    settings = {
        **({"model": config.model} if config.model else {}),
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{shlex.quote(str(topa))} hook pre-tool-use", "timeout": 10},
        ]}]},
    }
    (claude / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def _environment(meta: dict[str, Any], warning: str | None, mismatched: list[str]) -> str:
    rows = [
        ("container", meta["container"]),
        ("ssh alias", meta["alias"]),
        ("physical GPU", meta["gpu"]),
        ("TileOPs worktree", meta["worktree"]),
        ("TileFoundry source", meta["foundry_source"]),
        ("agent image", meta["image"]),
    ]
    lines = ["# Round environment", "", "| | |", "| --- | --- |"]
    lines += [f"| {name} | `{value}` |" for name, value in rows]
    if warning:
        lines += ["", f"GPU: {warning}."]
    if mismatched:
        lines += ["", "TileFoundry dependencies kept at the runner's version:", ""]
        lines += [f"- {item}" for item in mismatched]
    return "\n".join(lines) + "\n"


def _wiki_work(op: wiki.Op, page: wiki.Page) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan fields and brief values of a round on *op* of the wiki queue."""
    requirement = (
        f"The rows of `{op.name}` behind in the [Perf Tracker of {page.date}]({page.url}) (nightly run "
        f"[{page.run_id}]({page.run_url}), commit `{page.commit}`, benchmark `{op.benchmark}`; CUPTI "
        f"`device_busy_ms` in microseconds; ratio = comparator / TileOPs):\n\n{op.table()}\n\n"
        f"The op is `{op.name}`. Take these rows and the op's manifest entry, Op, workloads, reference, "
        "tests and benchmark as the whole requirement.")
    plan = {"kind": "perf", "op": op.name, "benchmark": op.benchmark, "wiki": page.url,
            "nightly_commit": page.commit, "workloads": [row.workload for row in op.rows], "measured": op.measured}
    return plan, {"TITLE": f"bring {op.name} to its fastest comparator", "REQUIREMENT": requirement,
                  "LINK": "The PR links no issue."}


def _issue_work(config: Config, number: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan fields and brief values of a round on TileOPs issue *number*; its body is the requirement."""
    issue = sh.gh_json("issue", "view", str(number), "--repo", config.upstream,
                       "--json", "number,title,body,labels,url,state")
    if issue["state"] != "OPEN":
        raise SystemExit(f"issue #{number} is {issue['state'].lower()}")
    kind = "perf" if PERF_ISSUE.match(issue["title"]) else "general"
    labels = ", ".join(label["name"] for label in issue["labels"]) or "none"
    body = "\n".join(f"> {line}".rstrip() for line in (issue["body"] or "(no body)").splitlines())
    requirement = (f"Issue [#{number}]({issue['url']}): {issue['title']} (labels: {labels})\n\n{body}\n\n"
                   "Take this issue, with the code, tests and docs it names, as the whole requirement.")
    if kind == "perf":
        requirement += (" The op is the one the issue names, and its rows are the workloads the issue lists; "
                        "copy their benchmark names into `workloads` in `provenance.json`.")
    # A perf PR only refers to its issue: the nightly confirms the result, then the issue closes.
    link = (f"End Summary, above the `Op:` lines, with the line `Refs #{number}`." if kind == "perf"
            else f"End Summary with the line `Closes #{number}`.")
    plan = {"kind": kind, "issue": number, "issue_url": issue["url"]}
    return plan, {"TITLE": issue["title"], "REQUIREMENT": requirement, "LINK": link}


def _branch(plan: dict[str, Any], title: str, category: str, slug: str) -> str:
    if plan["kind"] == "perf":
        return f"perf/{(category or 'ops').replace('_', '-')}/{slug}"
    tag = re.match(r"\[([^\]]+)\]", title)
    return f"{BRANCH_PREFIX.get(tag[1].lower(), 'fix') if tag else 'fix'}/issue/{slug}"


def start(config: Config, name: str | None = None, *, issue: int | None = None, page: wiki.Page | None = None,
          dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Start a round on the op *name* of the wiki queue (the newest page unless *page* is
    given), or on the TileOPs *issue*."""
    sh.require("claude", config.reviewer_program, "docker", "gh", "git", "herdr", "ssh", "ssh-keygen", "nvidia-smi")
    if (name is None) == (issue is None):
        raise SystemExit("name an op or pass --issue <N>, not both")
    if issue is None:
        page = page or wiki.fetch(config)
        op = page.op(name)
        work, values = _wiki_work(op, page)
        stem, key, category = name, ("op", name), op.category
    else:
        work, values = _issue_work(config, issue)
        stem, key, category = f"issue-{issue}", ("issue", issue), "issue"
    remote = sh.github_remote(config.tileops_repo, config.upstream)
    topa = topa_binary()
    with ledger.claim():
        views = ledger.rounds()
        live = [view["round"] for view in views.values() if view["state"] in ledger.LIVE]
        if len(live) >= config.max_rounds and not force:
            raise SystemExit(f"{len(live)} rounds are live ({', '.join(live)}); max_rounds is "
                             f"{config.max_rounds}. Stop one, or pass --force.")
        for view in views.values():
            if view.get(key[0]) == key[1] and view["state"] in ledger.LIVE:
                raise SystemExit(f"round {view['round']} already works {name or f'issue #{issue}'}")
        slug = round_slug(stem, views.values())
        branch = _branch(work, values["TITLE"], category, slug)
        worktree = config.worktree_root / f"tileops-{slug}"
        round_dir = config.rounds / slug
        plan = {"round": slug, **work, "branch": branch, "worktree": str(worktree), "round_dir": str(round_dir)}
        if dry_run:
            return plan
        if round_dir.exists() or worktree.exists():
            raise SystemExit(f"{round_dir} or {worktree} already exists")
        ledger.append("create", **plan)
    try:
        if issue is not None:
            sh.run(["gh", "issue", "edit", str(issue), "--repo", config.upstream, "--add-assignee", "@me"])
        sh.bootstrap_docker(config)
        sh.note(f"fetching {remote}/main")
        sh.run(["git", "-C", config.tileops_repo, "fetch", "--quiet", remote, "main"])
        sh.note(f"creating worktree {worktree} on {branch}")
        created = sh.herdr("worktree", "create", "--cwd", str(config.tileops_repo),
                           "--branch", branch, "--base", f"{remote}/main", "--path", str(worktree),
                           "--label", slug, "--no-focus", "--json")
        workspace = created["workspace"]["workspace_id"]
        pane = (created.get("root_pane") or {}).get("pane_id") or created["panes"][0]["pane_id"]
        ledger.append("worktree", round=slug, workspace=workspace, pane=pane)
        tileops_base = sh.run(["git", "-C", worktree, "rev-parse", "HEAD"])

        sh.note("preparing TileFoundry")
        prepared = foundry.prepare(config, (worktree / "pyproject.toml").read_text())
        source, foundry_git = foundry.source(config, slug, prepared.commit)
        ledger.append("foundry", round=slug, foundry_source=str(source), tilefoundry_base=prepared.commit)

        if issue is None:
            sh.note("collecting earlier attempts")
            prior = history.collect(config, op.name, op.benchmark, work["workloads"], exclude=slug)
            values["PRIOR_ATTEMPTS"] = history.render(prior)
        else:
            prior = None
            values["PRIOR_ATTEMPTS"] = ("This round works an issue, so topa collected no earlier attempts. "
                                        "Search the upstream `[Perf]` PRs for the op yourself.")
        values |= {
            "SLUG": slug, "ROUND_DIR": round_dir, "UPSTREAM": config.upstream, "BRANCH": branch,
            "TILEOPS_BASE": tileops_base, "TILEOPS_WORKTREE": worktree, "TILEFOUNDRY_BASE": prepared.commit,
            "TILEFOUNDRY_SOURCE": source, "KNOWLEDGE": knowledge(), "TOPA": topa, "REVIEWER": config.reviewer,
            "CHECKLISTS": review.DIRECTORY,
        }
        _write_round_dir(config, round_dir, plan["kind"], values, topa)
        if prior is not None:
            (round_dir / "prior-attempts.json").write_text(json.dumps(prior, indent=2) + "\n")

        sh.note("starting the container")
        started = container.start(config, slug=slug, worktree=worktree, round_dir=round_dir,
                                  foundry=prepared, foundry_source=source, foundry_git=foundry_git,
                                  foundry_base=prepared.commit)
        if started.gpu_warning:
            sh.note(f"warning: {started.gpu_warning}")
        for item in prepared.report["mismatched"]:
            sh.note(f"warning: kept {item}")
        meta = {
            **plan, "upstream": config.upstream,
            "tileops_base": tileops_base, "tilefoundry_base": prepared.commit,
            "foundry_source": str(source), "container": started.name, "alias": started.alias,
            "gpu": started.gpu, "image": started.image, "workspace": workspace, "pane": pane,
            "model": config.model, "effort": config.effort, "reviewer": Path(config.reviewer_program).name,
            "brief_sha256": hashlib.sha256((round_dir / "brief.md").read_bytes()).hexdigest(),
        }
        current = Round(round_dir, meta)
        current.save()
        (round_dir / "environment.md").write_text(_environment(meta, started.gpu_warning, prepared.report["mismatched"]))

        sh.note(f"starting the agent in {pane}")
        _cd_pane(pane, round_dir)
        sh.herdr("agent", "start", slug, "--kind", "claude", "--pane", pane, "--timeout", "60000",
                 "--", "--dangerously-skip-permissions", *(("--model", config.model) if config.model else ()),
                 *(("--effort", config.effort) if config.effort else ()))
        if not wait_ready(pane):
            sh.note(f"{pane} did not report ready within 60 s; sending the brief anyway")
        confirmed = submit(pane, PROMPT.format(slug=slug))
    except BaseException as error:
        ledger.append("fail", round=slug, error=str(error).strip()[:2000])
        sh.note(f"round {slug} failed to start; release what it holds with: topa round stop {slug} --rm")
        raise
    ledger.append("start", round=slug, **{k: v for k, v in meta.items() if k != "round"},
                  prompt_confirmed=confirmed)
    return meta


# --- say ----------------------------------------------------------------------------


def say(config: Config, slug: str, text: str) -> bool:
    current = find(config, slug)
    text = " ".join(text.split())
    if not text:
        raise SystemExit("nothing to say")
    if len(text) > SAY_LIMIT:
        raise SystemExit(f"the message has {len(text)} characters; the limit is {SAY_LIMIT}. "
                         "Write the long part to a file in the round directory and point to it.")
    confirmed = submit(current.meta["pane"], text)
    ledger.append("say", round=slug, text=text, confirmed=confirmed)
    return confirmed


# --- status -------------------------------------------------------------------------


def pull_request(config: Config, branch: str) -> dict[str, Any] | None:
    """The newest PR from *branch* of the fork; another fork can use the same branch name."""
    owner = (config.fork_owner or sh.gh_login()).lower()
    rows = sh.gh_json("pr", "list", "--repo", config.upstream, "--head", branch, "--state", "all",
                      "--json", "number,state,url,mergeCommit,headRepositoryOwner,headRefOid")
    return next((row for row in rows or [] if row["headRepositoryOwner"]["login"].lower() == owner), None)


def status(config: Config, *, every: bool = False) -> list[dict[str, Any]]:
    try:
        agents = {a["pane_id"]: a for a in sh.herdr("agent", "list").get("agents", []) if a.get("pane_id")}
    except sh.CommandError:
        agents = {}
    rows = []
    for view in ledger.rounds().values():
        if view["state"] == "removed" and not every:
            continue
        agent = agents.get(view.get("pane", ""))
        try:
            pr = pull_request(config, view["branch"]) if view.get("branch") else None
        except sh.CommandError:
            pr = None
        rows.append({
            "round": view["round"], "state": view["state"],
            "op": view.get("op") or (f"#{view['issue']}" if view.get("issue") else None),
            "branch": view.get("branch"), "gpu": view.get("gpu"), "pane": view.get("pane"),
            "agent": agent.get("agent_status") if agent else None,
            "pr": pr, "updated": view.get("updated"),
        })
    return rows


# --- stop ---------------------------------------------------------------------------


def _project_dir(path: Path) -> Path:
    """Where Claude Code keeps the transcripts of sessions started in *path*."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(path))


def archive_sessions(round_dir: Path) -> int:
    source = _project_dir(round_dir)
    if not source.is_dir():
        return 0
    target = round_dir / "session"
    shutil.copytree(source, target, dirs_exist_ok=True)
    manifest = []
    for path in sorted(p for p in target.rglob("*") if p.is_file() and p.name != "MANIFEST.json"):
        manifest.append({"path": str(path.relative_to(target)), "bytes": path.stat().st_size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (target / "MANIFEST.json").write_text(json.dumps(
        {"source": str(source), "files": manifest}, indent=2) + "\n")
    return len(manifest)


def foundry_patch(round_dir: Path, source: Path, base: str) -> bool:
    """Export any change the round made to its TileFoundry checkout; it should make none."""
    patch = round_dir / "foundry.patch"
    if not (source / ".git").exists():
        return patch.exists()
    sh.run(["git", "-C", source, "add", "--all"])
    diff = sh.run(["git", "-C", source, "diff", "--binary", "--cached", base])
    if diff:
        patch.write_text(diff + "\n")
        return True
    patch.unlink(missing_ok=True)
    return False


def drop_branch(config: Config, branch: str) -> str:
    """Delete the round's local *branch* when its PR is merged with the branch's last commit.

    GitHub's PR state decides: `git branch -d` takes a pushed branch for merged, and misses squash merges.
    """
    tip = sh.run(["git", "-C", config.tileops_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                 check=False)
    if not tip:
        return f"branch {branch} is already gone"
    try:
        pr = pull_request(config, branch)
    except sh.CommandError:
        pr = None
    if not pr or pr["state"] != "MERGED" or pr.get("headRefOid") != tip:
        return (f"kept branch {branch}: no merged PR holds its last commit "
                f"(git -C {config.tileops_repo} branch -D {branch} drops it)")
    sh.run(["git", "-C", config.tileops_repo, "branch", "-D", branch])
    return f"deleted branch {branch}: PR #{pr['number']} is merged"


def stop(config: Config, slug: str, *, remove: bool = False) -> list[str]:
    current = find(config, slug)
    meta = current.meta
    done: list[str] = []
    pane = meta.get("pane")
    if pane and _agent(pane):
        sh.herdr("agent", "send-keys", pane, "esc", check=False)
        time.sleep(0.5)
        sh.herdr("agent", "send-keys", pane, "C-c", "C-c", check=False)
        time.sleep(1.5)
        done.append(f"stopped the agent in {pane}")

    if meta.get("container"):
        owned = [current.dir] + [Path(meta[key]) for key in ("worktree", "foundry_source") if meta.get(key)]
        if container.give_back(config, meta["container"], owned):
            done.append("gave the files the container wrote back to you")

    if current.dir.is_dir():
        count = archive_sessions(current.dir)
        done.append(f"archived {count} session files to {current.dir / 'session'}")
        if meta.get("foundry_source") and meta.get("tilefoundry_base"):
            if foundry_patch(current.dir, Path(meta["foundry_source"]), meta["tilefoundry_base"]):
                done.append(f"TileFoundry was changed: {current.dir / 'foundry.patch'}")

    if not remove:
        if meta.get("container") and sh.docker_ok(config, "container", "inspect", meta["container"]):
            sh.docker(config, "stop", meta["container"], check=False)
            done.append(f"stopped container {meta['container']}; its GPU is free")
        if pane:
            sh.herdr("pane", "close", pane, check=False)
        ledger.append("stop", round=slug)
        return done

    removed = container.remove(config, slug)
    if removed:
        done.append(f"removed {len(removed)} container(s)")
    (config.ssh_config_dir / container.alias(slug)).unlink(missing_ok=True)
    shutil.rmtree(config.ssh_key_root / slug, ignore_errors=True)
    if meta.get("foundry_source"):
        foundry.remove_source(config, Path(meta["foundry_source"]))
        done.append("removed the TileFoundry checkout")

    worktree = Path(meta["worktree"]) if meta.get("worktree") else None
    if meta.get("workspace"):
        sh.herdr("worktree", "remove", "--workspace", meta["workspace"], "--force", check=False)
    if worktree and worktree.exists():
        sh.run(["git", "-C", config.tileops_repo, "worktree", "remove", "--force", worktree], check=False)
    if worktree and worktree.exists():
        done.append(f"could not remove {worktree}; remove it by hand")
    elif worktree:
        done.append(f"removed {worktree}")
        if meta.get("branch"):
            done.append(drop_branch(config, meta["branch"]))
    ledger.append("remove", round=slug)
    return done
