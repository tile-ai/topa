from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from topa import ledger, review, sh
from topa.config import Config


@dataclass(frozen=True)
class Limits:
    poll: int = 180            # seconds
    attempts: int = 15
    idle: int = 3600           # seconds
    wall: int = 24 * 3600      # seconds
    reviewer: int = 1800       # seconds
    tries: int = 3             # reviewer runs per attempt


FULL = "Review the full diff."
RECHECK = ("Recheck: reassess every fault in {previous} (its comments, threads and summary) and every thread "
           "of `threads.json`, answer new comments in the summary, and leave `comments` empty.")

PROMPT = """You are the reviewer of attempt {attempt} on PR #{pr} of {repo}.

{mode}

Follow {instructions}.

| Input | Path |
| --- | --- |
| inputs | {dir} |
| PR head | {worktree} |
| review knowledge | {knowledge} |

Apply these checklists:
{checklists}

- Commands other than git, gh, topa and file tools run in the round container. Run a test
  when a finding or a thread's fix depends on behavior.
- Prefix any timing run with `topa-bench`.
- Do not change tracked files in the worktree, push, or write on GitHub.
- Write the findings to {dir}/findings.json, with "attempt": "{attempt}" added at the top level.
{retry}"""

RETROSPECTIVE = """Each directory under {attempts} holds one attempt's `threads.json`, `findings.json` and
`result.json`. Read {knowledge}, its `What earns an entry` section first.

Write `## Review knowledge` in {report}. For each thread a later attempt marked `fixed`,
write one candidate in the form that section asks for, linking
https://github.com/{repo}/pull/{pr}#discussion_r<thread id>. Say when a candidate matches
an entry already in the knowledge file. List findings an author disputed under `### Disputed`, with the
author's words. Write `None.` when there is nothing. Do not edit
the knowledge file or the checklists."""


# --- signature ------------------------------------------------------------------------


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).replace("\\r", "").encode()).hexdigest()[:16]


def signature(config: Config, pr: int, own: dict[str, list[int]] | None = None) -> dict[str, Any]:
    """*own* holds the ids topa posted, by endpoint: `reviews` and `comments` (inline)."""
    own = own or {}
    mine = {f"pulls/{pr}/reviews": set(own.get("reviews", ())), f"pulls/{pr}/comments": set(own.get("comments", ()))}
    view = sh.gh_json("api", f"repos/{config.upstream}/pulls/{pr}")
    # Why the merge base, not the base tip: the tip moves on every merge to main, the diff does not.
    merge_base = sh.run(["gh", "api", f"repos/{config.upstream}/compare/{view['base']['sha']}...{view['head']['sha']}",
                         "--jq", ".merge_base_commit.sha"])
    collections = {endpoint: sorted([c["id"], c["body"] or ""] for c in review._gh(config, endpoint)
                                    if c["type"] != "Bot" and c["id"] not in mine.get(endpoint, ())
                                    and review.MARKER not in (c["body"] or ""))
                   for endpoint in (f"issues/{pr}/comments", f"pulls/{pr}/comments", f"pulls/{pr}/reviews")}
    return {"state": "merged" if view.get("merged_at") else view["state"],
            "head": view["head"]["sha"], "base": f"{view['base']['ref']}@{merge_base}", "author": view["user"]["login"],
            "title": _digest(view["title"]), "body": _digest(view["body"] or ""),
            "labels": _digest(sorted(label["name"] for label in view["labels"])),
            "comments": _digest(collections),
            # Why resolved, not open: a thread topa just opened must not start another attempt.
            "threads": _digest(sorted(review.thread_roots(config, pr, resolved=True)))}


REASONS = (("head", "new commits"), ("base", "merge base changed"), ("title", "title edited"),
           ("body", "body edited"), ("labels", "labels changed"), ("comments", "comments changed"),
           ("threads", "threads resolved"))


def keys(now: dict[str, Any], last: dict[str, Any] | None) -> list[str]:
    return [key for key, _ in REASONS if last is None or now[key] != last.get(key)]


def changed(now: dict[str, Any], last: dict[str, Any] | None) -> str | None:
    if last is None:
        return "first review"
    return next((reason for key, reason in REASONS if now[key] != last.get(key)), None)


# --- state ----------------------------------------------------------------------------


def _write(path: Path, value: Any) -> None:
    """Atomically replace a JSON file."""
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temp, path)


def _log(round_dir: Path, event: str, **fields: Any) -> None:
    with (round_dir / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}) + "\n")


@contextlib.contextmanager
def _sole_driver(round_dir: Path) -> Iterator[None]:
    with (round_dir / "loop.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"another driver already runs in {round_dir}") from None
        yield


# --- one attempt ----------------------------------------------------------------------


def _claude(config: Config, round_dir: Path, prompt: str, timeout: int) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    sh.run(["claude", "-p", "--dangerously-skip-permissions",
            *(("--model", config.model) if config.model else ()),
            *(("--effort", config.effort) if config.effort else ()), prompt],
           cwd=round_dir, env=env, timeout=timeout)


def _why(error: Exception) -> str:
    """One line on why a reviewer run failed, without the prompt it was given."""
    if isinstance(error, sh.CommandError):
        last = next((line for line in reversed(error.output.splitlines()) if line.strip()), "no output")
        return f"exited {error.code}: {last[:300]}"
    if isinstance(error, subprocess.TimeoutExpired):
        return f"timed out after {error.timeout:.0f} s"
    return f"{type(error).__name__}: {error}"


def _review(config: Config, round_dir: Path, pr: int, attempt: str, prepared: dict[str, Any],
            limits: Limits, previous: Path | None) -> dict[str, Any]:
    folder = Path(prepared["dir"])
    errors: list[str] = []
    for tries in range(1, limits.tries + 1):
        (folder / "findings.json").unlink(missing_ok=True)
        retry = ("\nFix these errors:\n" + "\n".join(f"- {e}" for e in errors)
                 if errors else "")
        prompt = PROMPT.format(attempt=attempt, pr=pr, repo=config.upstream, retry=retry,
                               mode=RECHECK.format(previous=previous) if previous else FULL,
                               checklists="\n".join(f"- {p}" for p in prepared["checklists"]),
                               **{k: prepared[k] for k in ("instructions", "dir", "worktree", "knowledge")})
        try:
            _claude(config, round_dir, prompt, limits.reviewer)
            found = json.loads((folder / "findings.json").read_text())
            errors = ([f"`attempt` is {found.get('attempt')!r}, not {attempt!r}"]
                      if isinstance(found, dict) and found.get("attempt") != attempt else review.problems(found, folder))
            if not errors and previous and found["comments"]:
                errors = ["a recheck writes no inline comment; move it into the summary"]
        except (sh.CommandError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as error:
            errors = [_why(error)]
        if not errors:
            return found
        sh.note(f"reviewer try {tries}/{limits.tries} of {attempt}: {'; '.join(errors)[:300]}")
    raise SystemExit(f"the reviewer gave no valid findings for {attempt} in {limits.tries} tries")


def _record(folder: Path, found: dict[str, Any], event: str, url: str, now: dict[str, Any], mode: str) -> None:
    _write(folder / "result.json", {"head": now["head"], "mode": mode, "event": event, "review": url,
                                    "comments": len(found.get("comments") or []),
                                    "threads": found.get("threads") or [], "settled": review.settled(found)})


# --- the loop -------------------------------------------------------------------------


def drive(config: Config, slug: str, limits: Limits = Limits()) -> int:
    from topa.round import find

    current = find(config, slug)
    meta, round_dir = current.meta, current.dir
    config = dataclasses.replace(config, upstream=meta.get("upstream", config.upstream))
    pr, worktree = int(meta["pr"]), Path(meta["worktree"])
    path = round_dir / "loop.json"
    with _sole_driver(round_dir):
        state = {"attempts": 0, "last": None, "pending": None, "end": None, "settled": False,
                 "reviewed": None, "posted": None, "retrospective": False, "code": None, "own": {"reviews": [], "comments": []}, "fixed": [], "started": time.time(),
                 "changed": time.time()}
        if path.exists():
            state |= json.loads(path.read_text())
        me = sh.gh_login().lower()

        def save() -> None:
            _write(path, state)

        def end(reason: str, code: int) -> int:
            if not state["end"]:
                state.update(end=reason, code=code)
                save()
                _log(round_dir, "end", reason=reason)
                ledger.append("review-end", round=slug, review_end=reason)
                sh.note(f"loop ended: {reason}")
            if state["attempts"] and not state["retrospective"]:
                sh.note("writing the retrospective")
                prompt = RETROSPECTIVE.format(pr=pr, repo=config.upstream,
                                              attempts=round_dir / "attempts", knowledge=review.KNOWLEDGE,
                                              report=round_dir / "report.md")
                try:
                    _claude(config, round_dir, prompt, limits.reviewer)
                    state["retrospective"] = True
                    save()
                except (sh.CommandError, subprocess.TimeoutExpired) as error:
                    sh.note(f"the retrospective failed: {_why(error)}; rerun the loop to retry it")
            return state["code"]

        def landed(n: int, found: dict[str, Any], posted: dict[str, Any], pending: dict[str, Any]) -> None:
            _record(round_dir / "attempts" / str(n), found, pending["event"], posted["review"],
                    pending["signature"], pending["mode"])
            state["own"] = {"reviews": sorted({*state["own"]["reviews"], posted["id"]}),
                            "comments": sorted({*state["own"]["comments"], *posted["comment_ids"]})}
            fixed = {t["id"] for t in found["threads"] if t["status"] == "fixed"}
            state["fixed"] = sorted(fixed if pending["mode"] == "full" else {*state["fixed"], *fixed})
            if pending["mode"] == "full":
                state["reviewed"] = [pending["signature"]["head"], pending["signature"]["base"]]
            state.update(pending=None, last=pending["signature"], changed=time.time(), settled=review.settled(found),
                         posted=n)
            save()
            _log(round_dir, "posted", attempt=n, mode=pending["mode"], review_event=pending["event"],
                 review=posted["review"], comments=len(posted["comment_ids"]), settled=review.settled(found))

        if state["end"]:
            return end(state["end"], state["code"])
        while True:
            if ledger.rounds().get(slug, {}).get("state") not in ledger.LIVE:
                sh.note(f"round {slug} is no longer live; stopping")
                return 0
            if pending := state["pending"]:
                found = json.loads((round_dir / "attempts" / str(pending["n"]) / "findings.json").read_text())
                now = signature(config, pr, state["own"])
                if posted := review.posted(config, pr, pending["attempt"]):
                    landed(pending["n"], found, posted, pending)
                elif now["state"] == "open" and not changed(now, pending["signature"]):
                    folder = round_dir / "attempts" / str(pending["n"])
                    landed(pending["n"], found, review.post(config, pr, folder, event=pending["event"],
                                                            attempt=pending["attempt"]), pending)
                else:
                    state["pending"] = None
                    save()
                    _log(round_dir, "unposted", attempt=pending["n"])
                continue

            if time.time() - state["started"] > limits.wall:
                return end(f"ran {limits.wall // 3600} hours", 6)
            now = signature(config, pr, state["own"])
            if now["state"] != "open":
                return end(f"PR is {now['state']}", 0)

            reason = changed(now, state["last"])
            if state["settled"] and not reason:
                return end("settled", 0)
            if not reason:
                if time.time() - state["changed"] > limits.idle:
                    return end(f"no change in {limits.idle // 60} minutes", 5)
                time.sleep(limits.poll)
                continue
            if state["attempts"] >= limits.attempts:
                return end(f"{limits.attempts} attempts without settling", 3)

            state["attempts"] += 1
            n = state["attempts"]
            attempt = f"{slug}-{n}"
            mode = "recheck" if [now["head"], now["base"]] == state["reviewed"] else "full"
            save()
            _log(round_dir, "attempt", attempt=n, mode=mode, reason=reason, head=now["head"],
                 changed={key: [(state["last"] or {}).get(key), now[key]] for key in keys(now, state["last"])})
            sh.note(f"attempt {n} ({mode}) on {now['head'][:9]}: {reason}")
            folder = round_dir / "attempts" / str(n)
            prepared = review.prepare(config, pr, folder, worktree, fixed=state["fixed"] if mode == "recheck" else [])
            if hashlib.sha256((worktree / "pyproject.toml").read_bytes()).hexdigest() != meta["pyproject"]:
                return end("pyproject.toml changed, so the container is stale; start a new review round", 7)
            if prepared["head"] != now["head"]:
                _log(round_dir, "discarded", attempt=n, why="head moved while preparing")
                continue
            try:
                previous = round_dir / "attempts" / str(state["posted"]) / "findings.json" if mode == "recheck" else None
                found = _review(config, round_dir, pr, attempt, prepared, limits, previous)
            except SystemExit as error:
                return end(str(error), 4)
            after = signature(config, pr, state["own"])
            if after["state"] != "open":
                return end(f"PR is {after['state']}", 0)
            if moved := changed(after, now):
                sh.note(f"{moved} during the review; discarding attempt {n}")
                _log(round_dir, "discarded", attempt=n, why=moved, keys=keys(after, now))
                continue

            settled = review.settled(found)
            event = "APPROVE" if settled and now["author"].lower() != me else "COMMENT"
            pending = {"n": n, "attempt": attempt, "event": event, "mode": mode, "signature": now}
            state["pending"] = pending
            save()
            posted = review.post(config, pr, folder, event=event, attempt=attempt)
            landed(n, found, posted, pending)
            sh.note(f"{event} with {posted['posted']} comments: {posted['review']}")


# --- lessons --------------------------------------------------------------------------


def lessons(config: Config) -> dict[str, Any]:
    """`ready`: knowledge entries whose evidence spans two PRs or more, to assess against
    `Adding a row` in `general.md`. `fixed`: every topa finding a review round later saw
    fixed in code. `reports`: the review rounds whose report holds candidates.
    """
    text = review.KNOWLEDGE.read_text()
    candidates = text.split("## Candidates", 1)[-1].split("\n## ", 1)[0]
    ready = [line.strip() for line in candidates.splitlines()
             if (m := re.search(r"\[(\d+) PRs\]", line)) and int(m.group(1)) >= 2]
    fixed: dict[int, dict[str, Any]] = {}
    reports = []
    for round_dir in sorted(config.rounds.glob("*/")):
        try:
            meta = json.loads((round_dir / "round.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("kind") != "review":
            continue
        report = round_dir / "report.md"
        if report.is_file() and "## Review knowledge" in report.read_text():
            reports.append(str(report))
        for result in sorted((round_dir / "attempts").glob("*/result.json")):
            threads = {t["id"]: t for t in json.loads((result.parent / "threads.json").read_text())}
            for verdict in json.loads(result.read_text()).get("threads", []):
                thread = threads.get(verdict.get("id"))
                if verdict.get("status") == "fixed" and thread and thread["id"] not in fixed:
                    fixed[thread["id"]] = {
                        "pr": meta["pr"], "comment": thread["comments"][0][:300],
                        "link": f"https://github.com/{meta.get('upstream', config.upstream)}/pull/{meta['pr']}"
                                f"#discussion_r{thread['id']}",
                        "evidence": verdict.get("evidence", "")}
    return {"ready": ready, "fixed": list(fixed.values()), "reports": reports}
