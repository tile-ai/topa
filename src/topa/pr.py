"""`topa gate` and `topa pr`: check a round's evidence, then open or update its PR.

The PR comes from the fork: the branch is pushed to the remote of `fork_owner`
(the gh login by default), and the PR's head is `<fork_owner>:<branch>`. Its body
is `pr-template.md`, which becomes the squash commit's message; each section of
`pr-comments.md` is one comment.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from topa import ledger, sh
from topa.config import Config
from topa.gate import GateError, read_comments, read_pr, validate_round
from topa.round import Round, find, pull_request

CHECKLIST = {
    "perf": """
1. Was final_hir.py analyzed, finalized with schedule finalize, and the kernel written from its TIR?
2. Do the raw analyze reports justify the chosen placement over the rejected ones?
3. Was every row of the op measured alternating main, candidate and comparators, and every row of
   another op sharing a changed kernel alternating main and candidate?
4. Does every claim in pr-template.md and pr-comments.md match the evidence?""",
    "general": """
1. Does the diff do everything the issue asks, and nothing it does not?
2. Does each new test fail on the base and pass on the candidate?
3. Does every claim in pr-template.md and pr-comments.md match the evidence?""",
}


def gate(config: Config, slug: str | None = None) -> Round:
    current = find(config, slug)
    meta = current.meta
    try:
        validate_round(current.dir, Path(meta["worktree"]), "HEAD", tuple(meta.get("workloads", ())),
                       meta.get("measured", 0), meta.get("kind", "perf"), meta.get("issue"),
                       meta.get("op"), meta.get("benchmark"),
                       # A queue round whose round.json has no benchmark got a brief without the trailers.
                       trailers="benchmark" in meta or "op" not in meta)
    except GateError as error:
        raise SystemExit(f"topa gate: FAIL: {error}") from error
    print("topa gate: PASS")
    return current


def _post_comments(config: Config, number: int, comments: list[tuple[str, str]], folder: str) -> None:
    """Create or update one comment per section; ours is the one we wrote that opens with its heading."""
    me = sh.gh_login()
    out = sh.run(["gh", "api", "--paginate", f"repos/{config.upstream}/issues/{number}/comments",
                  "--jq", ".[] | {id, login: .user.login, body}"])
    existing: dict[str, int] = {}
    for line in out.splitlines():
        comment = json.loads(line)
        if comment["login"] == me:
            existing.setdefault(comment["body"].split("\n", 1)[0].strip(), comment["id"])
    for name, text in comments:
        path = Path(folder, "comment.md")
        path.write_text(text, encoding="utf-8")
        found = existing.get(f"## {name}")
        endpoint = (f"repos/{config.upstream}/issues/comments/{found}" if found
                    else f"repos/{config.upstream}/issues/{number}/comments")
        sh.run(["gh", "api", "-X", "PATCH" if found else "POST", endpoint, "-F", f"body=@{path}"])


def open_pr(config: Config, slug: str | None = None, *, reviewed: bool = False, watch: bool = True) -> str:
    current = gate(config, slug)
    kind = current.meta.get("kind", "perf")
    sh.note("Artifact checks passed. Re-open the actual files before creating the PR:\n" + CHECKLIST[kind]
            + "\n\nIf any answer is no or uncertain, continue the round instead of opening a PR.")
    if not reviewed:
        sh.note("PR not opened. Inspect those files, then rerun: topa pr --reviewed")
        raise SystemExit(2)

    worktree = Path(current.meta["worktree"])
    branch = sh.run(["git", "-C", worktree, "branch", "--show-current"])
    if not branch:
        raise SystemExit("the TileOPs worktree is not on a branch")
    owner = config.fork_owner or sh.gh_login()
    repo_name = config.upstream.split("/", 1)[1]
    fork = sh.github_remote(worktree, f"{owner}/{repo_name}")
    title, text = read_pr(current.dir)
    comments = read_comments(current.dir, kind)
    repo = ["--repo", config.upstream]

    sh.run(["git", "-C", worktree, "push", "--force-with-lease", "-u", fork, branch], capture=False)
    existing = pull_request(config, branch)
    # gh reads the body from a file; the template's first line is the title, not body.
    with tempfile.TemporaryDirectory() as folder:
        body = Path(folder, "pr-body.md")
        body.write_text(text, encoding="utf-8")
        if existing and existing["state"] in ("OPEN", "CLOSED"):
            if existing["state"] == "CLOSED":
                sh.run(["gh", "pr", "reopen", str(existing["number"]), *repo])
            url, number = existing["url"], existing["number"]
        else:
            url = sh.run(["gh", "pr", "create", *repo, "--base", "main", "--head", f"{owner}:{branch}",
                          "--title", title, "--body-file", body])
            number = int(url.rstrip("/").rsplit("/", 1)[1])
        # Recorded before the comments, so a PR whose comments fail is still the round's.
        ledger.append("pr", round=current.slug, pr=url)
        _post_comments(config, number, comments, folder)
        if existing:
            sh.run(["gh", "pr", "edit", str(number), *repo, "--title", title, "--body-file", body])
    print(url)
    if watch:
        sh.run(["gh", "pr", "checks", url, *repo, "--watch", "--interval", "300"], check=False, capture=False)
    return url

