"""`topa review`: prepare each review round of a TileOPs PR, and post its findings.

A round is due when the PR's head, title or body changes, or when someone other than
topa comments or reviews. `next` writes the round's inputs and a worktree at the PR
head; the reviewing agent writes `findings.json`; `post` submits them as one review.
Every body topa posts ends with `MARKER`, which is how topa tells its own comments
from others'.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from topa import sh
from topa.config import Config

MARKER = "<!-- topa-review -->"
POLL_SECONDS = 120

# The checklist each changed path loads, besides `general.md`.
CHECKLISTS = (
    ("tests.md", re.compile(r"^(tests|workloads)/")),
    ("manifest.md", re.compile(r"^src/tileops/manifest/")),
    ("docs.md", re.compile(r"^docs/|\.md$")),
)

# What others write on a PR, by the REST endpoint under `repos/<repo>/` that lists it.
KINDS = {"comment": "issues/{pr}/comments", "review_comment": "pulls/{pr}/comments", "review": "pulls/{pr}/reviews"}

THREADS = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}
"""


def checklists(paths: Iterable[str]) -> list[Path]:
    """`general.md`, then the checklist of each area the changed *paths* touch."""
    paths = list(paths)
    names = ["general.md"] + [name for name, pattern in CHECKLISTS if any(pattern.search(p) for p in paths)]
    return [Path(str(files("topa") / "checklists" / name)) for name in names]


def _header_path(name: str, prefix: str) -> str:
    """The path a `---` or `+++` header names, unquoting git's C-style quoting; "" for /dev/null."""
    name = name.removesuffix("\t")
    if name.startswith('"'):
        name = name[1:-1].encode("latin-1").decode("unicode_escape").encode("latin-1").decode()
    return name[len(prefix):] if name.startswith(prefix) else ""


def commentable(diff: str) -> set[tuple[str, str, int]]:
    """Every `(path, side, line)` a review comment can anchor on in a unified *diff*.

    `RIGHT` lines are added or context lines of the new file; `LEFT` lines are removed
    or context lines of the old file. Lines outside a hunk take no comment.
    """
    anchors: set[tuple[str, str, int]] = set()
    old_path = new_path = ""
    old = new = old_left = new_left = 0
    for line in diff.splitlines():
        if old_left or new_left:
            kind = line[:1] or " "
            if kind in "+ ":
                anchors.add((new_path, "RIGHT", new))
                new, new_left = new + 1, new_left - 1
            if kind in "- ":
                anchors.add((old_path, "LEFT", old))
                old, old_left = old + 1, old_left - 1
        elif line.startswith("--- "):
            old_path = _header_path(line[4:], "a/")
        elif line.startswith("+++ "):
            new_path = _header_path(line[4:], "b/")
        elif found := re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line):
            old, new = int(found.group(1)), int(found.group(3))
            old_left = int(found.group(2) or 1)
            new_left = int(found.group(4) or 1)
    return anchors


def by_others(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The comments or reviews a person wrote, leaving out bots, topa's own and empty ones."""
    return [c for c in items if c.get("type") != "Bot" and (c.get("body") or "").strip()
            and MARKER not in c["body"]]


def trigger(seen: dict[str, Any] | None, now: dict[str, Any]) -> str | None:
    """Why a round is due, comparing the PR as last reviewed with *now*; None if none is."""
    if not seen:
        return "first round"
    for key, reason in (("head", "new commits"), ("text", "title or body edited"), ("comment", "new comment"),
                        ("review_comment", "new review comment"), ("review", "new review")):
        if now[key] != seen.get(key):
            return reason
    return None


def _key(path: str, body: str) -> tuple[str, str]:
    return path, " ".join(body.replace(MARKER, "").split())


def select(comments: list[dict[str, Any]], anchors: set[tuple[str, str, int]],
           posted: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The *comments* to post, and how many repeat one topa already *posted* on that file.

    Raises SystemExit naming every comment whose line is not in *anchors*.
    """
    off = [c for c in comments if (c["path"], c.get("side", "RIGHT"), c["line"]) not in anchors]
    if off:
        lines = "\n".join(f"  {c['path']}:{c['line']} {c.get('side', 'RIGHT')}" for c in off)
        raise SystemExit(f"these comments are not on a line of pr.diff; move them or put them in the summary:\n{lines}")
    seen = {_key(c["path"], c["body"]) for c in posted if MARKER in (c.get("body") or "")}
    fresh = []
    for comment in comments:
        key = _key(comment["path"], comment["body"])
        if key not in seen:
            seen.add(key)
            fresh.append(comment)
    return fresh, len(comments) - len(fresh)


# --- GitHub and git ---------------------------------------------------------------


def _list(config: Config, pr: int, kind: str) -> list[dict[str, Any]]:
    """Every comment or review of *kind* on *pr*, oldest first."""
    endpoint = KINDS[kind].format(pr=pr)
    out = sh.run(["gh", "api", "--paginate", f"repos/{config.upstream}/{endpoint}", "--jq",
                  ".[] | {id, user: .user.login, type: .user.type, body, path, line, "
                  "reply_to: .in_reply_to_id, url: .html_url}"])
    return [{**json.loads(line), "kind": kind} for line in out.splitlines() if line.strip()]


def _snapshot(config: Config, pr: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """The PR, what `trigger` compares, and every comment and review on it."""
    view = sh.gh_json("pr", "view", str(pr), "--repo", config.upstream, "--json",
                      "title,body,state,author,baseRefName,headRefOid,url")
    items = [item for kind in KINDS for item in _list(config, pr, kind)]
    text = view["title"] + "\n" + (view["body"] or "").replace("\r", "")
    now = {"head": view["headRefOid"], "text": hashlib.sha256(text.encode()).hexdigest()[:16]}
    for kind in KINDS:
        now[kind] = max((c["id"] for c in by_others(items) if c["kind"] == kind), default=0)
    return view, now, items


def _refs(pr: int) -> tuple[str, str]:
    return f"refs/topa/review/{pr}/head", f"refs/topa/review/{pr}/base"


def _checkout(config: Config, pr: int, base: str, worktree: Path) -> tuple[str, str]:
    """Fetch the PR into `tileops_repo` and check out its head; return the head and its merge base."""
    repo = config.tileops_repo
    head_ref, base_ref = _refs(pr)
    remote = sh.github_remote(repo, config.upstream)
    sh.run(["git", "-C", repo, "fetch", "--quiet", remote, f"+pull/{pr}/head:{head_ref}",
            f"+refs/heads/{base}:{base_ref}"])
    head = sh.run(["git", "-C", repo, "rev-parse", head_ref])
    if (worktree / ".git").exists():
        sh.run(["git", "-C", worktree, "reset", "--quiet", "--hard", head])
    else:
        sh.run(["git", "-C", repo, "worktree", "prune"])
        sh.run(["git", "-C", repo, "worktree", "add", "--quiet", "--detach", worktree, head])
    return head, sh.run(["git", "-C", repo, "merge-base", base_ref, head])


def _release(config: Config, pr: int, worktree: Path) -> None:
    repo = config.tileops_repo
    if worktree.exists() and not sh.ok(["git", "-C", repo, "worktree", "remove", "--force", worktree]):
        shutil.rmtree(worktree, ignore_errors=True)
    sh.run(["git", "-C", repo, "worktree", "prune"], check=False)
    for ref in _refs(pr):
        sh.run(["git", "-C", repo, "update-ref", "-d", ref], check=False)


def _diff(config: Config, old: str, new: str) -> str | None:
    """The diff from *old* to *new*, or None when *old* is not in `tileops_repo`."""
    repo = config.tileops_repo
    if not sh.ok(["git", "-C", repo, "cat-file", "-e", f"{old}^{{commit}}"]):
        return None
    return sh.run(["git", "-C", repo, "diff", "--no-color", "--no-ext-diff", "-M", old, new]) + "\n"


def _paths(config: Config, base: str, head: str) -> list[str]:
    """Every path the PR adds, changes or removes; a rename lists both names."""
    out = sh.run(["git", "-C", config.tileops_repo, "diff", "-z", "--name-only", "--no-renames", base, head])
    return [path for path in out.split("\0") if path]


def _threads(config: Config, pr: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The unresolved review threads, each with every comment in it."""
    owner, name = config.upstream.split("/", 1)
    out = sh.run(["gh", "api", "graphql", "--paginate", "-f", f"query={THREADS}", "-F", f"owner={owner}",
                  "-F", f"name={name}", "-F", f"number={pr}", "--jq",
                  ".data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not) "
                  "| {outdated: .isOutdated, root: .comments.nodes[0].databaseId}"])
    comments = [c for c in items if c["kind"] == "review_comment"]
    threads = []
    for node in map(json.loads, filter(str.strip, out.splitlines())):
        thread = [c for c in comments if node["root"] in (c["id"], c["reply_to"])]
        if thread:
            threads.append({"path": thread[0]["path"], "line": thread[0]["line"], "outdated": node["outdated"],
                            "topa": MARKER in (thread[0]["body"] or ""),
                            "comments": [{"user": c["user"], "body": c["body"].replace(MARKER, "").strip(),
                                          "url": c["url"]} for c in thread]})
    return threads


def _checks(config: Config, pr: int) -> str:
    return sh.run(["gh", "pr", "checks", str(pr), "--repo", config.upstream, "--json", "name,state,link"],
                  check=False) or "[]"


def _submit(config: Config, pr: int, payload: dict[str, Any]) -> str:
    out = sh.run(["gh", "api", "--method", "POST", f"repos/{config.upstream}/pulls/{pr}/reviews",
                  "--input", "-"], input=json.dumps(payload))
    return json.loads(out)["html_url"]


# --- state ------------------------------------------------------------------------


def _directory(config: Config, pr: int) -> Path:
    return config.reviews / config.upstream / str(pr)


def _write(path: Path, value: Any) -> None:
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _read(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def _prepared(folder: Path) -> tuple[dict[str, Any], Path, dict[str, Any] | None]:
    """The PR's state, the directory of its next round, and that round if `next` prepared it."""
    state = _read(folder / "state.json") or {"round": 0}
    current = folder / f"round-{state['round'] + 1:02d}"
    return state, current, _read(current / "round.json")


# --- commands ---------------------------------------------------------------------


def next_round(config: Config, pr: int, *, wait: int = 0, max_rounds: int = 10) -> dict[str, Any]:
    """Prepare the next round of *pr*, polling up to *wait* seconds for one to be due.

    Preparing a round again, before `post`, refreshes its inputs and keeps `findings.json`.
    `round.json` is written last, so `post` refuses a round whose preparation failed.
    """
    folder = _directory(config, pr)
    folder.mkdir(parents=True, exist_ok=True)
    worktree = folder / "worktree"
    state, current, _ = _prepared(folder)
    deadline = time.monotonic() + wait
    while True:
        view, now, items = _snapshot(config, pr)
        if view["state"] != "OPEN":
            _release(config, pr, worktree)
            return {"state": view["state"].lower(), "rounds": state["round"]}
        reason = trigger(state.get("seen"), now)
        if reason and state["round"] >= max_rounds:
            return {"state": "limit", "rounds": state["round"]}
        if reason:
            break
        if time.monotonic() + POLL_SECONDS > deadline:
            return {"state": "idle", "rounds": state["round"]}
        time.sleep(POLL_SECONDS)

    current.mkdir(exist_ok=True)
    (current / "round.json").unlink(missing_ok=True)
    head, base = _checkout(config, pr, view["baseRefName"], worktree)
    now["head"] = head
    paths = _paths(config, base, head)
    _write(current / "pr.diff", _diff(config, base, head) or "")
    seen = state.get("seen") or {}
    since = _diff(config, seen["head"], head) if seen.get("head") not in (None, head) else None
    if since is None:
        (current / "since-last.diff").unlink(missing_ok=True)
    else:
        _write(current / "since-last.diff", since)
    _write(current / "pr.json", {"repo": config.upstream, "number": pr, "url": view["url"], "title": view["title"],
                                 "body": view["body"], "author": view["author"]["login"], "base": base,
                                 "head": head, "files": paths})
    _write(current / "threads.json", _threads(config, pr, items))
    _write(current / "comments.json", [c for c in by_others(items) if c["id"] > seen.get(c["kind"], 0)])
    _write(current / "checks.json", _checks(config, pr))
    result = {"state": "review", "round": state["round"] + 1, "trigger": reason, "dir": str(current),
              "worktree": str(worktree), "checklists": [str(p) for p in checklists(paths)]}
    _write(current / "round.json", {"seen": now, "result": result})
    return result


def post(config: Config, pr: int, *, request_changes: bool = False) -> dict[str, Any]:
    """Submit the prepared round's `findings.json` as one review on the round's head."""
    folder = _directory(config, pr)
    _, current, prepared = _prepared(folder)
    if not prepared:
        raise SystemExit(f"no prepared round for {config.upstream}#{pr}; run topa review next {pr}")
    findings = _read(current / "findings.json")
    if findings is None:
        raise SystemExit(f"write {current / 'findings.json'} first")
    summary = (findings.get("summary") or "").strip()
    comments, repeated = select(findings.get("comments") or [], commentable((current / "pr.diff").read_text()),
                                _list(config, pr, "review_comment"))
    ours = [r for r in _list(config, pr, "review") if MARKER in (r["body"] or "")]
    last = ours[-1]["body"].replace(MARKER, "").strip() if ours else None

    url = None
    if comments or summary != last or request_changes:
        url = _submit(config, pr, {
            "commit_id": prepared["seen"]["head"],
            "event": "REQUEST_CHANGES" if request_changes else "COMMENT",
            "body": f"{summary}\n\n{MARKER}",
            "comments": [{"path": c["path"], "line": c["line"], "side": c.get("side", "RIGHT"),
                          "body": f"{c['body']}\n\n{MARKER}"} for c in comments],
        })
    number = prepared["result"]["round"]
    _write(folder / "state.json", {"round": number, "seen": prepared["seen"]})
    return {"round": number, "posted": len(comments), "repeated": repeated, "review": url}
