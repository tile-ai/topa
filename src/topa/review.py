"""`topa review`: review a TileOPs PR once.

`prepare` writes the PR's inputs and checks out its head; the reviewing agent writes
`findings.json`; `post` submits it as one COMMENT review. Every body topa posts ends with
`MARKER`, which is how topa tells its own comments from others'.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from topa import sh
from topa.config import Config

MARKER = "<!-- topa-review -->"

# The checklist each changed path loads, besides `general.md`.
CHECKLISTS = (
    ("ops.md", re.compile(r"^src/tileops/(_ops|_kernels|_utils|csrc|backend)/")),
    ("kernels.md", re.compile(r"^src/tileops/(_kernels|csrc)/")),
    ("tests.md", re.compile(r"^(tests|workloads|benchmarks/tests)/")),
    ("benchmarks.md", re.compile(r"^benchmarks/")),
    ("manifest.md", re.compile(r"^src/tileops/manifest/")),
    ("docs.md", re.compile(r"^docs/|\.md$")),
)

# Each review thread: resolved or not, and the id of its first comment.
THREADS = """query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $endCursor) { pageInfo { hasNextPage endCursor }
      nodes { isResolved comments(first: 1) { nodes { databaseId } } } } } } }"""


def checklists(paths: Iterable[str]) -> list[Path]:
    """`general.md`, then the checklist of each area the changed *paths* touch."""
    paths = list(paths)
    names = ["general.md"] + [name for name, pattern in CHECKLISTS if any(pattern.search(p) for p in paths)]
    return [Path(str(files("topa") / "kit" / "review" / "checklists" / name)) for name in names]


def commentable(diff: str) -> set[tuple[str, str, int]]:
    """Every `(path, side, line)` of a hunk in *diff*: `RIGHT` in the new file, `LEFT` in the old."""
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
                anchors.add((new_path or old_path, "LEFT", old))  # GitHub names a renamed file by its new path
                old, old_left = old + 1, old_left - 1
        elif line.startswith(("--- ", "+++ ")):
            # "a/<path>" or "b/<path>", with a tab after a path holding a space; /dev/null names no file.
            path = line[6:].removesuffix("\t") if line[4:6] in ("a/", "b/") else ""
            old_path, new_path = (path, new_path) if line[0] == "-" else (old_path, path)
        elif found := re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line):
            old, new = int(found.group(1)), int(found.group(3))
            old_left = int(found.group(2) or 1)
            new_left = int(found.group(4) or 1)
    return anchors


def select(comments: list[dict[str, Any]], anchors: set[tuple[str, str, int]],
           posted: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The *comments* to post: refuse any off *anchors*, skip any topa already *posted* on that file."""
    off = [f"{c['path']}:{c['line']}" for c in comments if (c["path"], c.get("side", "RIGHT"), c["line"]) not in anchors]
    if off:
        raise SystemExit(f"not on a line of pr.diff; move onto one or into the summary: {', '.join(off)}")
    key = lambda c: (c["path"], " ".join(c["body"].replace(MARKER, "").split()))
    seen = {key(c) for c in posted if MARKER in (c["body"] or "")}
    return [c for c in comments if key(c) not in seen]


def _gh(config: Config, endpoint: str) -> list[dict[str, Any]]:
    """Every item the REST *endpoint* under the upstream repository lists."""
    out = sh.run(["gh", "api", "--paginate", f"repos/{config.upstream}/{endpoint}", "--jq",
                  ".[] | {id, user: .user.login, type: .user.type, body, path, line, reply_to: .in_reply_to_id}"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _threads(config: Config, pr: int, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The unresolved review threads topa started, each with every comment in it."""
    owner, name = config.upstream.split("/", 1)
    out = sh.run(["gh", "api", "graphql", "--paginate", "-f", f"query={THREADS}", "-F", f"owner={owner}",
                  "-F", f"name={name}", "-F", f"number={pr}", "--jq",
                  ".data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not) "
                  "| .comments.nodes[0].databaseId"])
    roots = {int(i) for i in out.split()}
    threads = [[c for c in comments if root in (c["id"], c["reply_to"])] for root in roots]
    return [{"path": t[0]["path"], "line": t[0]["line"], "comments": [f"{c['user']}: {c['body']}" for c in t]}
            for t in threads if t and MARKER in t[0]["body"]]


def prepare(config: Config, pr: int) -> dict[str, Any]:
    """Write the review inputs of *pr* and check out its head."""
    folder = config.reviews / config.upstream / str(pr)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "findings.json").unlink(missing_ok=True)
    repo, worktree = config.tileops_repo, folder / "worktree"
    view = sh.gh_json("api", f"repos/{config.upstream}/pulls/{pr}")
    remote = sh.github_remote(repo, config.upstream)
    sh.run(["git", "-C", repo, "fetch", "--quiet", remote, f"+pull/{pr}/head:refs/topa/review/{pr}",
            f"+refs/heads/{view['base']['ref']}:refs/topa/review/{pr}-base"])
    head = sh.run(["git", "-C", repo, "rev-parse", f"refs/topa/review/{pr}"])
    base = sh.run(["git", "-C", repo, "merge-base", f"refs/topa/review/{pr}-base", head])
    sh.run(["git", "-C", repo, "worktree", "remove", "--force", worktree], check=False)
    sh.run(["git", "-C", repo, "worktree", "add", "--quiet", "--detach", worktree, head])
    paths = [p for p in sh.run(["git", "-C", repo, "diff", "-z", "--name-only", "--no-renames", base, head])
             .split("\0") if p]
    diff = sh.run(["git", "-C", repo, "diff", "--no-color", "--no-ext-diff", "-M", base, head])
    review_comments = _gh(config, f"pulls/{pr}/comments")
    others = [c for c in _gh(config, f"issues/{pr}/comments") + _gh(config, f"pulls/{pr}/reviews") + review_comments
              if c["type"] != "Bot" and (c["body"] or "").strip() and MARKER not in c["body"]]
    inputs = {"pr.json": {"number": pr, "title": view["title"], "body": view["body"] or "",
                          "author": view["user"]["login"], "association": view["author_association"],
                          "base": base, "head": head, "files": paths},
              "pr.diff": diff + "\n", "threads.json": _threads(config, pr, review_comments),
              "comments.json": others}
    for name, value in inputs.items():
        (folder / name).write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")
    return {"dir": str(folder), "worktree": str(worktree), "checklists": [str(p) for p in checklists(paths)]}


def post(config: Config, pr: int) -> dict[str, Any]:
    """Submit `findings.json` as one COMMENT review on the prepared head, then remove the worktree."""
    folder = config.reviews / config.upstream / str(pr)
    head = json.loads((folder / "pr.json").read_text())["head"]
    findings = json.loads((folder / "findings.json").read_text())
    comments = select(findings.get("comments") or [], commentable((folder / "pr.diff").read_text()),
                      _gh(config, f"pulls/{pr}/comments"))
    payload = {"commit_id": head, "event": "COMMENT", "body": f"{findings.get('summary', '').strip()}\n\n{MARKER}",
               "comments": [{"path": c["path"], "line": c["line"], "side": c.get("side", "RIGHT"),
                             "body": f"{c['body']}\n\n{MARKER}"} for c in comments]}
    out = sh.run(["gh", "api", "--method", "POST", f"repos/{config.upstream}/pulls/{pr}/reviews", "--input", "-"],
                 input=json.dumps(payload))
    sh.run(["git", "-C", config.tileops_repo, "worktree", "remove", "--force", folder / "worktree"], check=False)
    return {"review": json.loads(out)["html_url"], "posted": len(comments)}
