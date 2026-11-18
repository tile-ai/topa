from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from topa import sh
from topa.config import Config

MARKER = "<!-- topa-review -->"
ATTEMPT = "<!-- topa-attempt: {} -->"

DIRECTORY = Path(str(files("topa") / "kit" / "review" / "checklists"))
INSTRUCTIONS = DIRECTORY.parent / "review.md"
KNOWLEDGE = Path(str(files("topa") / "knowledge" / "review.md"))

# The checklist each changed path loads, besides `general.md`.
CHECKLISTS = (
    ("ops.md", re.compile(r"^src/tileops/(_ops|_kernels|_utils|csrc|backend)/")),
    ("kernels.md", re.compile(r"^src/tileops/(_kernels|csrc)/")),
    ("tests.md", re.compile(r"^(tests|workloads|benchmarks/tests)/")),
    ("benchmarks.md", re.compile(r"^benchmarks/")),
    ("manifest.md", re.compile(r"^src/tileops/manifest/")),
    ("docs.md", re.compile(r"^docs/|\.md$")),
)

THREADS = """query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $endCursor) { pageInfo { hasNextPage endCursor }
      nodes { isResolved comments(first: 1) { nodes { databaseId } } } } } } }"""


def checklists(paths: Iterable[str]) -> list[Path]:
    """`general.md`, then the checklist of each area the changed *paths* touch."""
    paths = list(paths)
    names = ["general.md"] + [name for name, pattern in CHECKLISTS if any(pattern.search(p) for p in paths)]
    return [DIRECTORY / name for name in names]


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


def thread_roots(config: Config, pr: int, *, resolved: bool) -> set[int]:
    owner, name = config.upstream.split("/", 1)
    test = ".isResolved" if resolved else "(.isResolved | not)"
    out = sh.run(["gh", "api", "graphql", "--paginate", "-f", f"query={THREADS}", "-F", f"owner={owner}",
                  "-F", f"name={name}", "-F", f"number={pr}", "--jq",
                  f".data.repository.pullRequest.reviewThreads.nodes[] | select({test}) "
                  "| .comments.nodes[0].databaseId"])
    return {int(i) for i in out.split()}


def _threads(config: Config, pr: int, comments: list[dict[str, Any]],
             fixed: Iterable[int] | None) -> list[dict[str, Any]]:
    """Topa's threads with every comment: the unresolved ones, or with *fixed*, every one not in it.

    Why: an author can resolve a thread without fixing it, so a review round re-judges it until a
    reviewer has seen it fixed.
    """
    roots = (thread_roots(config, pr, resolved=False) if fixed is None
             else {c["id"] for c in comments if c["reply_to"] is None} - set(fixed))
    threads = [[c for c in comments if root in (c["id"], c["reply_to"])] for root in roots]
    return [{"id": t[0]["id"], "path": t[0]["path"], "line": t[0]["line"],
             "comments": [f"{c['user']}: {c['body']}" for c in t]}
            for t in threads if t and MARKER in t[0]["body"]]


def review_ref(config: Config, pr: int) -> str:
    return f"refs/topa/review/{config.upstream}/{pr}"


def folder_of(config: Config, pr: int) -> Path:
    return config.reviews / config.upstream / str(pr)


def prepare(config: Config, pr: int, folder: Path | None = None, worktree: Path | None = None,
            fixed: Iterable[int] | None = None) -> dict[str, Any]:
    """Write the review inputs of *pr* into *folder* and check out its head.

    A given *worktree* is checked out in place, never re-created: a container mounts it.
    """
    folder = folder or folder_of(config, pr)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "findings.json").unlink(missing_ok=True)
    repo = config.tileops_repo
    view = sh.gh_json("api", f"repos/{config.upstream}/pulls/{pr}")
    remote = sh.github_remote(repo, config.upstream)
    ref = review_ref(config, pr)
    sh.run(["git", "-C", repo, "fetch", "--quiet", remote, f"+pull/{pr}/head:{ref}",
            f"+refs/heads/{view['base']['ref']}:{ref}-base"])
    head = sh.run(["git", "-C", repo, "rev-parse", ref])
    base = sh.run(["git", "-C", repo, "merge-base", f"{ref}-base", head])
    if worktree is None:
        worktree = folder / "worktree"
        sh.run(["git", "-C", repo, "worktree", "remove", "--force", worktree], check=False)
        sh.run(["git", "-C", repo, "worktree", "add", "--quiet", "--detach", worktree, head])
    else:
        sh.run(["git", "-C", worktree, "checkout", "--quiet", "--force", "--detach", head])
        sh.run(["git", "-C", worktree, "clean", "--quiet", "-fd"])
    paths = [p for p in sh.run(["git", "-C", repo, "diff", "-z", "--name-only", "--no-renames", base, head])
             .split("\0") if p]
    diff = sh.run(["git", "-C", repo, "diff", "--no-color", "--no-ext-diff", "-M", base, head])
    review_comments = _gh(config, f"pulls/{pr}/comments")
    others = [c for c in _gh(config, f"issues/{pr}/comments") + _gh(config, f"pulls/{pr}/reviews") + review_comments
              if c["type"] != "Bot" and (c["body"] or "").strip() and MARKER not in c["body"]]
    inputs = {"pr.json": {"number": pr, "title": view["title"], "body": view["body"] or "",
                          "author": view["user"]["login"], "association": view["author_association"],
                          "base": base, "head": head, "files": paths},
              "pr.diff": diff + "\n", "threads.json": _threads(config, pr, review_comments, fixed),
              "comments.json": others}
    for name, value in inputs.items():
        (folder / name).write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")
    return {"dir": str(folder), "worktree": str(worktree), "checklists": [str(p) for p in checklists(paths)],
            "instructions": str(INSTRUCTIONS), "knowledge": str(KNOWLEDGE), "head": head, "base": base}


def problems(found: Any, folder: Path) -> list[str]:
    """What keeps *found*, read from `findings.json` in *folder*, from being posted."""
    if not isinstance(found, dict) or not isinstance(found.get("summary"), str):
        return ["findings.json is not an object with a string `summary`"]
    comments, verdicts = found.get("comments"), found.get("threads")
    if not isinstance(comments, list) or not isinstance(verdicts, list):
        return ["`comments` and `threads` must be lists, empty when there is nothing"]
    errors = []
    if not isinstance(found.get("open_faults"), int) or found["open_faults"] < 0:
        errors.append("`open_faults` must be the number of faults the head still has")
    anchors = commentable((folder / "pr.diff").read_text())
    for c in comments:
        if not isinstance(c, dict) or not isinstance(c.get("path"), str) or not isinstance(c.get("line"), int) \
                or not isinstance(c.get("body"), str) or c.get("side", "RIGHT") not in ("LEFT", "RIGHT"):
            errors.append(f"a comment lacks path, line or body: {c}")
        elif (c["path"], c.get("side", "RIGHT"), c["line"]) not in anchors:
            errors.append(f"{c['path']}:{c['line']} is not on a line of pr.diff; move it onto one or into the summary")
    ids = {thread["id"] for thread in json.loads((folder / "threads.json").read_text())}
    status = {t["id"]: t.get("status") for t in verdicts if isinstance(t, dict) and isinstance(t.get("id"), int)}
    if len(status) != len(verdicts) or set(status) != ids or any(s not in ("fixed", "open") for s in status.values()):
        errors.append(f"`threads` needs exactly one entry per thread of threads.json ({sorted(ids)}), "
                      "each with a status of `fixed` or `open`")
    return errors


def settled(found: dict[str, Any]) -> bool:
    return (found["open_faults"] == 0 and not found["comments"]
            and all(t["status"] == "fixed" for t in found["threads"]))


def posted(config: Config, pr: int, attempt: str) -> dict[str, Any] | None:
    """The review topa posted for *attempt*, as `review` (URL), `id` and `comment_ids`, if one exists."""
    out = sh.run(["gh", "api", "--paginate", f"repos/{config.upstream}/pulls/{pr}/reviews", "--jq",
                  ".[] | {id, body, html_url}"])
    tag = ATTEMPT.format(attempt)
    found = next((r for line in out.splitlines() if line.strip()
                  for r in [json.loads(line)] if tag in (r["body"] or "")), None)
    return found and {"review": found["html_url"], "id": found["id"],
                      "comment_ids": comment_ids(config, pr, found["id"])}


def comment_ids(config: Config, pr: int, review_id: int) -> list[int]:
    out = sh.run(["gh", "api", "--paginate", f"repos/{config.upstream}/pulls/{pr}/reviews/{review_id}/comments",
                  "--jq", ".[].id"])
    return [int(i) for i in out.split()]


def post(config: Config, pr: int, folder: Path | None = None, *, event: str = "COMMENT",
         attempt: str | None = None) -> dict[str, Any]:
    """Post `findings.json` as one review; without *folder*, also remove the worktree it made."""
    single = folder is None
    folder = folder or folder_of(config, pr)
    head = json.loads((folder / "pr.json").read_text())["head"]
    findings = json.loads((folder / "findings.json").read_text())
    comments = select(findings.get("comments") or [], commentable((folder / "pr.diff").read_text()),
                      _gh(config, f"pulls/{pr}/comments"))
    tag = f"\n{ATTEMPT.format(attempt)}" if attempt else ""
    payload = {"commit_id": head, "event": event, "body": f"{findings.get('summary', '').strip()}\n\n{MARKER}{tag}",
               "comments": [{"path": c["path"], "line": c["line"], "side": c.get("side", "RIGHT"),
                             "body": f"{c['body']}\n\n{MARKER}"} for c in comments]}
    out = sh.run(["gh", "api", "--method", "POST", f"repos/{config.upstream}/pulls/{pr}/reviews", "--input", "-"],
                 input=json.dumps(payload))
    if single:
        sh.run(["git", "-C", config.tileops_repo, "worktree", "remove", "--force", folder / "worktree"], check=False)
    submitted = json.loads(out)
    return {"review": submitted["html_url"], "posted": len(comments), "id": submitted["id"],
            "comment_ids": comment_ids(config, pr, submitted["id"]) if comments else []}
