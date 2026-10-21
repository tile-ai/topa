"""`topa round`: start, steer, inspect and stop one agent working one issue.

A round owns five things: a TileOPs worktree on its own branch, a herdr workspace
whose pane shows the agent, a detached TileFoundry checkout, a container with one
GPU in which the agent runs, and its round directory under `~/.topa/rounds/<slug>/`.
`round.json` in that directory names all of them; the ledger records what happened
to them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from topa import container, foundry, ledger, sh
from topa.config import Config
from topa.issue import Issue, branch_name, op_family, round_slug

PROMPT = ("Read `brief.md` in the current working directory and complete it. A human may "
          "reach you with `topa round say {slug} \"...\"`; treat that as a new instruction.")
SAY_LIMIT = 4000
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def kit() -> Path:
    return Path(str(files("topa") / "kit" / "round"))


def knowledge() -> Path:
    return Path(str(files("topa") / "knowledge" / "tilelang.md"))


def render(template: str, values: dict[str, Any]) -> str:
    """Fill every `{{NAME}}`; a name with no value is an error, never left in the text."""
    missing = sorted(set(PLACEHOLDER.findall(template)) - set(values))
    if missing:
        raise SystemExit(f"brief placeholders without a value: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)


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


def _cd_pane(pane: str, path: Path) -> None:
    """Enter the round directory, with its `bin/claude` ahead of the host's on PATH."""
    marker = f"topa-ready-{time.time_ns()}"
    half = len(marker) // 2
    where = shlex.quote(str(path))
    sh.herdr("pane", "run", pane,
             f'export PATH={where}/bin:"$PATH" && cd {where} '
             f"&& printf '%s%s\\n' {marker[:half]} {marker[half:]}")
    sh.run(["herdr", "pane", "wait-output", pane, "--source", "recent-unwrapped",
            "--match", marker, "--timeout", "30000"])


# --- start --------------------------------------------------------------------------


def _write_round_dir(config: Config, path: Path, values: dict[str, Any]) -> None:
    shutil.copytree(kit(), path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("brief.md", "__pycache__"))
    brief = render((kit() / "brief.md").read_text(), values)
    (path / "brief.md").write_text(brief)
    claude = path / ".claude"
    claude.mkdir(exist_ok=True)
    settings = {"model": config.model}
    (claude / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def _environment(meta: dict[str, Any], warning: str | None, mismatched: list[str]) -> str:
    rows = [
        ("container", meta["container"]),
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


def start(config: Config, number: int, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    sh.require("claude", "docker", "gh", "git", "herdr", "nvidia-smi")
    views = ledger.rounds()
    live = [view["round"] for view in views.values() if view["state"] in ledger.LIVE]
    if len(live) >= config.max_rounds and not force:
        raise SystemExit(f"{len(live)} rounds are live ({', '.join(live)}); max_rounds is "
                         f"{config.max_rounds}. Stop one, or pass --force.")
    issue = Issue.fetch(config, number)
    if issue.state != "open":
        raise SystemExit(f"#{number} is {issue.state}")
    for view in views.values():
        if view.get("issue") == number and view["state"] in ledger.LIVE:
            raise SystemExit(f"round {view['round']} already works #{number}")
    slug = round_slug(number, views.values())
    remote = sh.github_remote(config.tileops_repo, config.upstream)
    branch = branch_name(issue, slug, op_family(config.tileops_repo, f"{remote}/main", issue.title))
    worktree = config.worktree_root / f"tileops-{slug}"
    round_dir = config.rounds / slug
    plan = {"round": slug, "issue": number, "title": issue.title, "branch": branch,
            "worktree": str(worktree), "round_dir": str(round_dir)}
    if dry_run:
        return plan
    if round_dir.exists() or worktree.exists():
        raise SystemExit(f"{round_dir} or {worktree} already exists")

    ledger.append("create", round=slug, issue=number, issue_url=issue.url, title=issue.title,
                  branch=branch, worktree=str(worktree), round_dir=str(round_dir))
    try:
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

        values = {
            "SLUG": slug, "ISSUE": number, "ISSUE_URL": issue.url, "ISSUE_TITLE": issue.title,
            "UPSTREAM": config.upstream, "BRANCH": branch, "TILEOPS_BASE": tileops_base,
            "TILEOPS_WORKTREE": worktree, "TILEFOUNDRY_BASE": prepared.commit,
            "TILEFOUNDRY_SOURCE": source, "KNOWLEDGE": knowledge(),
        }
        _write_round_dir(config, round_dir, values)

        sh.note("starting the container")
        started = container.start(config, slug=slug, worktree=worktree, round_dir=round_dir,
                                  foundry=prepared, foundry_source=source, foundry_git=foundry_git,
                                  foundry_base=prepared.commit)
        if started.gpu_warning:
            sh.note(f"warning: {started.gpu_warning}")
        for item in prepared.report["mismatched"]:
            sh.note(f"warning: kept {item}")
        meta = {
            **plan, "issue_url": issue.url, "upstream": config.upstream,
            "tileops_base": tileops_base, "tilefoundry_base": prepared.commit,
            "foundry_source": str(source), "container": started.name,
            "gpu": started.gpu, "image": started.image, "workspace": workspace, "pane": pane,
            "model": config.model, "effort": config.effort,
            "brief_sha256": hashlib.sha256((round_dir / "brief.md").read_bytes()).hexdigest(),
        }
        current = Round(round_dir, meta)
        current.save()
        (round_dir / "environment.md").write_text(_environment(meta, started.gpu_warning, prepared.report["mismatched"]))

        sh.note(f"starting the agent in {pane}")
        wrapper = round_dir / "bin" / "claude"
        wrapper.parent.mkdir(exist_ok=True)
        wrapper.write_text(container.agent_wrapper(config, started.name))
        wrapper.chmod(0o755)
        _cd_pane(pane, round_dir)
        sh.herdr("agent", "start", slug, "--kind", "claude", "--pane", pane, "--timeout", "60000",
                 "--", "--dangerously-skip-permissions", "--model", config.model, "--effort", config.effort)
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
    try:
        rows = sh.gh_json("pr", "list", "--repo", config.upstream, "--head", branch, "--state", "all",
                          "--limit", "1", "--json", "number,state,url")
    except sh.CommandError:
        return None
    return rows[0] if rows else None


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
        pr = pull_request(config, view["branch"]) if view.get("branch") else None
        rows.append({
            "round": view["round"], "state": view["state"], "issue": view.get("issue"),
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
        branch = meta.get("branch")
        if branch and sh.ok(["git", "-C", config.tileops_repo, "rev-parse", "--verify", f"refs/heads/{branch}"]):
            if sh.ok(["git", "-C", config.tileops_repo, "branch", "-d", branch]):
                done.append(f"deleted branch {branch}")
            else:
                done.append(f"kept branch {branch}: it is not merged "
                            f"(git -C {config.tileops_repo} branch -D {branch} drops it)")
    ledger.append("remove", round=slug)
    return done
