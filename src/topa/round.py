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

from topa import container, foundry, gate, history, ledger, review, sh, states, wiki
from topa.config import Config
from topa.gate import FOUNDRY_KINDS

PROMPT = ("Read `brief.md` in the current working directory and complete it. A human may "
          "reach you with `topa round say {slug} \"...\"`; treat that as a new instruction.")
SAY_LIMIT = 4000
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


# The round kinds; each has a folder of that name in `kit/round/`. The TileFoundry kinds also share
# the text in `kit/round/foundry/`.
KINDS = ("perf", "new-op", "extend", "general")
FOUNDRY_PART = re.compile(r"^<!-- ([A-Z_]+) -->\n", re.MULTILINE)
PERF_ISSUE = re.compile(r"\[perf\]", re.IGNORECASE)
FEAT_ISSUE = re.compile(r"\[feat\]", re.IGNORECASE)
ISSUE_FIELDS = "number,title,body,labels,url,state,assignees"
OP_NAME = re.compile(r"\b[A-Z][A-Za-z0-9_]*Op\b")
MANIFEST_SPEC = "src/tileops/manifest/spec/"
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


def foundry_text() -> dict[str, str]:
    """The TileFoundry text the briefs of `FOUNDRY_KINDS` share: `FOUNDRY_<NAME>` to the text under `<!-- NAME -->`."""
    parts = FOUNDRY_PART.split((kit() / "foundry" / "brief.md").read_text())
    return {f"FOUNDRY_{name}": text.strip() for name, text in zip(parts[1::2], parts[2::2])}


def brief(kind: str, values: dict[str, Any]) -> str:
    """The shared brief with the *kind* section in place of `{{KIND}}`, every placeholder filled."""
    text = (kit() / "brief.md").read_text().replace("{{KIND}}", (kit() / kind / "brief.md").read_text().strip())
    # The shared TileFoundry text first: it holds placeholders of its own.
    for name, part in foundry_text().items():
        text = text.replace(f"{{{{{name}}}}}", part)
    return render(text, values)


def pr_comments(kind: str) -> str:
    """The PR comments file a round of *kind* fills in.

    The TileFoundry kinds share one. A new op's or an extension's rows answer to comparators alone, so
    their results table drops the main column.
    """
    if kind not in FOUNDRY_KINDS:
        return (kit() / kind / "pr-comments.md").read_text()
    lines = (kit() / "foundry" / "pr-comments.md").read_text().splitlines(keepends=True)
    if kind == "perf":
        return "".join(lines)
    header = next(i for i, line in enumerate(lines) if line.startswith("| Workload | main (us) |"))
    column = lines[header].split("|").index(" main (us) ")
    for i in range(header, header + 3):
        cells = lines[i].split("|")
        lines[i] = "|".join(cells[:column] + cells[column + 1:])
    return "".join(lines)


def _write_round_dir(config: Config, path: Path, kind: str, values: dict[str, Any], topa: Path) -> None:
    ignore = shutil.ignore_patterns("brief.md", *KINDS, "foundry", "__pycache__")
    shutil.copytree(kit(), path, dirs_exist_ok=True, ignore=ignore)
    shutil.copytree(kit() / kind, path, dirs_exist_ok=True, ignore=ignore)
    (path / "pr-comments.md").write_text(pr_comments(kind))
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


def manifest_entry(text: str, op: str) -> list[str] | None:
    """The lines under the top-level entry *op* of the manifest YAML *text*; None when it has none."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.fullmatch(rf"{re.escape(op)}:\s*(#.*)?", line)), None)
    if start is None:
        return None
    entry = []
    for line in lines[start + 1:]:
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            break
        entry.append(line)
    return entry


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _keys(entry: list[str]) -> str:
    """The indent of the entry's own keys."""
    first = next((line for line in entry if line.strip() and not line.lstrip().startswith("#")), "")
    return first[:_indent(first)]


def entry_field(entry: list[str], key: str) -> str | None:
    """The scalar *key* of the entry itself, not of a mapping nested deeper."""
    indent = _keys(entry)
    for line in entry:
        found = re.fullmatch(rf"{indent}{key}:\s*[\"']?([A-Za-z0-9_.-]+)[\"']?\s*(#.*)?", line)
        if found:
            return found[1]
    return None


def _dtype_cases(workload: str) -> list[str]:
    """Each `{...}` of the workload's `dtype_cases` list; a workload without the list is one case."""
    found = re.search(r"\bdtype_cases:\s*\[", workload)
    if not found:
        return [""]
    cases, depth, start = [], 0, 0
    for i in range(found.end(), len(workload)):
        if workload[i] in "[{":
            start = i if depth == 0 else start
            depth += 1
        elif workload[i] in "]}":
            if depth == 0:
                break
            depth -= 1
            if depth == 0:
                cases.append(workload[start:i + 1])
    return cases


def inventory(entry: list[str]) -> list[str]:
    """Each workload `label` x dtype case of the entry, as `<label> <case>`; empty when a workload has no label."""
    indent = len(_keys(entry))
    workloads: list[str] = []
    item: int | None = None
    inside = False
    for line in entry:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) == indent and stripped.startswith("workloads:"):
            inside = True
        elif inside and _indent(line) <= indent and not stripped.startswith("- "):
            break
        elif inside:
            # Each item starts with `- ` at the indent of the first; a deeper line continues it.
            item = _indent(line) if item is None else item
            if _indent(line) == item and stripped.startswith("- "):
                workloads.append(stripped[2:])
            elif workloads:
                workloads[-1] += " " + stripped
    cases = []
    for workload in workloads:
        label = re.search(r"\blabel:\s*[\"']?([^,}\s\"']+)", workload)
        if label is None:
            return []
        cases += [f"{label[1]} {case}".rstrip() for case in _dtype_cases(workload)]
    return cases


def _fetch_main(config: Config) -> str:
    """The admitted commit: upstream main, fetched now."""
    repo = config.tileops_repo
    remote = sh.github_remote(repo, config.upstream)
    sh.note(f"fetching {remote}/main")
    sh.run(["git", "-C", repo, "fetch", "--quiet", remote, "main"])
    # FETCH_HEAD, not `<remote>/main`: a remote whose fetch refspec leaves out main never updates that ref.
    return sh.run(["git", "-C", repo, "rev-parse", "FETCH_HEAD^{commit}"])


def _entry_files(config: Config, commit: str, op: str) -> list[str]:
    """The manifest file of each `<op>:` entry at *commit*, once per entry."""
    # Each hit is `<commit>:<path>:<line>:<text>`.
    hits = sh.run(["git", "-C", config.tileops_repo, "grep", "-n", "-E", f"^{op}:", commit, "--", MANIFEST_SPEC],
                  check=False).splitlines()
    return [hit.removeprefix(f"{commit}:").split(":", 1)[0] for hit in hits]


def _entry(config: Config, commit: str, op: str) -> tuple[str, list[str]]:
    """The one manifest entry of *op* at *commit*: its file and its lines."""
    found = _entry_files(config, commit, op)
    if len(found) != 1:
        raise SystemExit(f"{op} has {len(found)} manifest entries under {MANIFEST_SPEC} at {commit[:12]}; "
                         "a round needs exactly one")
    entry = manifest_entry(sh.run(["git", "-C", config.tileops_repo, "show", f"{commit}:{found[0]}"]), op)
    return found[0], entry or []


def _manifest_ops(config: Config, commit: str, names: Iterable[str]) -> dict[str, tuple[str, list[str]]]:
    """The manifest entry, file and lines, of each op of *names* that has one at *commit*."""
    return {op: _entry(config, commit, op) for op in names if _entry_files(config, commit, op)}


def _names(op: str, text: str | None) -> bool:
    return re.search(rf"\b{re.escape(op)}\b", text or "") is not None


def _issue(config: Config, number: int) -> dict[str, Any]:
    issue = sh.gh_json("issue", "view", str(number), "--repo", config.upstream, "--json", ISSUE_FIELDS)
    if issue["state"] != "OPEN":
        raise SystemExit(f"issue #{number} is {issue['state'].lower()}")
    return issue


def _find_issue(config: Config, op: str) -> dict[str, Any] | None:
    """The one open issue whose title names *op*, or None."""
    rows = sh.gh_json("issue", "list", "--repo", config.upstream, "--state", "open", "--search", f"{op} in:title",
                      "--limit", "100", "--json", ISSUE_FIELDS) or []
    found = [row for row in rows if _names(op, row["title"])]
    if len(found) > 1:
        numbers = ", ".join(f"#{row['number']}" for row in found)
        raise SystemExit(f"open issues {numbers} name {op}; pass the one to work with --issue <N>")
    return found[0] if found else None


def _quoted(issue: dict[str, Any]) -> str:
    labels = ", ".join(label["name"] for label in issue["labels"]) or "none"
    body = "\n".join(f"> {line}".rstrip() for line in (issue["body"] or "(no body)").splitlines())
    return f"Issue [#{issue['number']}]({issue['url']}): {issue['title']} (labels: {labels})\n\n{body}"


def _issue_work(issue: dict[str, Any], kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan fields and brief values of a perf or general round on *issue*; its body is the requirement."""
    number = issue["number"]
    requirement = f"{_quoted(issue)}\n\nTake this issue, with the code, tests and docs it names, as the whole requirement."
    if kind == "perf":
        requirement += (" The op is the one the issue names, and its rows are the workloads the issue lists; "
                        "copy their benchmark names into `workloads` in `provenance.json`.")
    # A perf PR only refers to its issue: the nightly confirms the result, then the issue closes.
    link = (f"End Summary, above the `Op:` lines, with the line `Refs #{number}`." if kind == "perf"
            else f"End Summary with the line `Closes #{number}`.")
    plan = {"kind": kind, "issue": number, "issue_url": issue["url"]}
    return plan, {"TITLE": issue["title"], "REQUIREMENT": requirement, "LINK": link}


def _extend_work(issue: dict[str, Any],
                 entries: dict[str, tuple[str, list[str]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan fields and brief values of a round on *issue* that changes the implemented ops of *entries*."""
    cases = {op: _cases(op, path, entry) for op, (path, entry) in entries.items()}
    listed = "\n".join(f"- `{op} {case}`" for op in entries for case in cases[op])
    requirement = (f"{_quoted(issue)}\n\nThe ops it names, with their manifest entries at the TileOPs base: "
                   f"{', '.join(f'`{op}` in `{path}`' for op, (path, _) in entries.items())}. Their cases, "
                   f"each op, workload `label` and dtype case:\n\n{listed}\n\n"
                   "Take the issue, these entries and these cases as the whole requirement.")
    plan = {"kind": "extend", "issue": issue["number"], "issue_url": issue["url"], "ops": list(entries),
            "inventory": [f"{op} {case}" for op in entries for case in cases[op]]}
    return plan, {"TITLE": issue["title"], "REQUIREMENT": requirement,
                  "LINK": f"End Summary with the line `Closes #{issue['number']}`."}


def _cases(op: str, path: str, entry: list[str]) -> list[str]:
    """The entry's workload `label` x dtype cases; an entry without a family or a label refuses."""
    cases = inventory(entry)
    if entry_field(entry, "family") is None or not cases:
        raise SystemExit(f"the manifest entry of {op} in {path} needs a family and a label on every workload")
    return cases


def _new_op_work(op: str, path: str, family: str, cases: list[str],
                 issue: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan fields and brief values of a round that adds the spec-only *op*."""
    listed = "\n".join(f"- `{case}`" for case in cases)
    requirement = (f"Add `{op}`, whose manifest entry in `{path}` is `spec-only` at the TileOPs base. Its cases, "
                   f"each workload `label` by dtype case:\n\n{listed}\n\n")
    if issue:
        requirement += f"{_quoted(issue)}\n\n"
    requirement += f"Take the entry{', the issue' if issue else ''} and these cases as the whole requirement."
    plan = {"kind": "new-op", "op": op, "family": family, "inventory": cases}
    link = "The PR links no issue."
    if issue:
        plan |= {"issue": issue["number"], "issue_url": issue["url"]}
        link = f"End Summary, above the `Op:` lines, with the line `Closes #{issue['number']}`."
    return plan, {"TITLE": f"add {op}", "REQUIREMENT": requirement, "LINK": link}


@dataclass
class Admission:
    """What admission found for a new round."""

    work: dict[str, Any]
    values: dict[str, Any]
    admitted: str
    stem: str
    category: str = "issue"
    issue: dict[str, Any] | None = None
    queue: wiki.Op | None = None


def _route(config: Config, name: str | None, number: int | None, page: wiki.Page | None) -> Admission:
    """Route and Issue: the kind of the round and the issue it works, or a refusal.

    Kernel work of a perf or feature issue runs through TileFoundry, so an issue routes by its
    title's TileOPs type and the ops it names (the words with a manifest entry): `[Perf]` gets
    perf; `[Feat]` naming one spec-only op new-op, several refuse, only implemented ops extend;
    any other issue general.
    """
    issue = _issue(config, number) if number is not None else None
    admitted = _fetch_main(config)
    if name is None:
        assert issue is not None
        if PERF_ISSUE.match(issue["title"]):
            return Admission(*_issue_work(issue, "perf"), admitted, f"issue-{number}", issue=issue)
        names = OP_NAME.findall(issue["title"]) if FEAT_ISSUE.match(issue["title"]) else []
        named = _manifest_ops(config, admitted, dict.fromkeys(names))
        status = {op: entry_field(entry, "status") for op, (_, entry) in named.items()}
        unknown = [f"{op} is {state}" for op, state in status.items() if state not in ("spec-only", "implemented")]
        if unknown:
            raise SystemExit(f"issue #{number} names ops of no known status: {', '.join(unknown)}")
        spec_only = [op for op, state in status.items() if state == "spec-only"]
        if len(spec_only) > 1:
            raise SystemExit(f"issue #{number} names several spec-only ops ({', '.join(spec_only)}); "
                             "start the one to add with `topa round start <Op> --issue <N>`")
        if named and not spec_only:
            return Admission(*_extend_work(issue, named), admitted, f"issue-{number}", issue=issue)
        if not spec_only:
            return Admission(*_issue_work(issue, "general"), admitted, f"issue-{number}", issue=issue)
        name = spec_only[0]
    path, entry = _entry(config, admitted, name)
    status = entry_field(entry, "status")
    if status != "spec-only":
        if issue is not None:
            raise SystemExit(f"{name} is {status}, not spec-only: start a perf round on it without --issue")
        page = page or wiki.fetch(config)
        op = page.op(name)
        return Admission(*_wiki_work(op, page), admitted, name, category=op.category, queue=op)
    family, cases = entry_field(entry, "family"), _cases(name, path, entry)
    if issue is not None and not _names(name, issue["title"]):
        raise SystemExit(f"the title of issue #{number} does not name {name}")
    issue = issue or _find_issue(config, name)
    return Admission(*_new_op_work(name, path, family, cases, issue), admitted, name, issue=issue)


def _check_owner(config: Config, admission: Admission, me: str) -> None:
    """Ownership of a new op or an extension: no one else holds its issue or has a PR open on the
    issue or the new op."""
    op, issue = admission.work.get("op"), admission.issue
    others = [user["login"] for user in (issue or {}).get("assignees", []) if user["login"].lower() != me.lower()]
    if others:
        raise SystemExit(f"issue #{issue['number']} is assigned to {', '.join(others)}")
    # `#N`, `<owner>/<repo>#N` or the issue URL, after any GitHub closing keyword.
    repo = re.escape(config.upstream)
    target = rf"(?:{repo}#|https://github\.com/{repo}/issues/|#)"
    closes = re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+{target}{issue['number']}\b",
                        re.IGNORECASE) if issue else None
    prs = sh.gh_json("pr", "list", "--repo", config.upstream, "--state", "open", "--limit", "1000",
                     "--json", "number,title,body,author,url") or []
    for pr in prs:
        if pr["author"]["login"].lower() == me.lower():
            continue
        if (op and (_names(op, pr["title"]) or _names(op, pr["body"]))) or (closes and closes.search(pr["body"] or "")):
            raise SystemExit(f"PR #{pr['number']} by {pr['author']['login']} already works "
                             f"{op or 'issue #' + str(issue['number'])}: {pr['url']}")


def _ops(work: dict[str, Any]) -> set[str]:
    """The ops a round works: its `op`, or an extension's `ops`."""
    return {*work.get("ops", ()), *filter(None, [work.get("op")])}


def _branch(plan: dict[str, Any], title: str, category: str, slug: str) -> str:
    if plan["kind"] == "perf":
        return f"perf/{(category or 'ops').replace('_', '-')}/{slug}"
    if plan["kind"] == "new-op":
        return f"feat/{plan['family'].replace('_', '-')}/{slug}"
    tag = re.match(r"\[([^\]]+)\]", title)
    return f"{BRANCH_PREFIX.get(tag[1].lower(), 'fix') if tag else 'fix'}/issue/{slug}"


def start(config: Config, name: str | None = None, *, issue: int | None = None, page: wiki.Page | None = None,
          dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Start a round on the op *name*, on the TileOPs *issue*, or on both.

    Admission routes the round to its kind, finds its issue, checks that no one else owns the
    work, and only then creates anything. A spec-only op gets a new-op round; any other op a
    perf round on the wiki queue (the newest page unless *page* is given).
    """
    sh.require("claude", config.reviewer_program, "docker", "gh", "git", "herdr", "ssh", "ssh-keygen", "nvidia-smi")
    if name is None and issue is None:
        raise SystemExit("name an op, pass --issue <N>, or both")
    topa = topa_binary()
    with ledger.claim():
        admission = _route(config, name, issue, page)
        work, values, number = admission.work, admission.values, admission.work.get("issue")
        views = ledger.rounds()
        for view in views.values():
            taken = sorted(_ops(view) & _ops(work)) + [f"issue #{number}"] * (
                number is not None and view.get("issue") == number)
            if taken and view["state"] in ledger.LIVE:
                raise SystemExit(f"round {view['round']} already works {taken[0]}")
        live = [view["round"] for view in views.values() if view["state"] in ledger.LIVE]
        if len(live) >= config.max_rounds and not force:
            raise SystemExit(f"{len(live)} rounds are live ({', '.join(live)}); max_rounds is "
                             f"{config.max_rounds}. Stop one, or pass --force.")
        me = sh.gh_login() if admission.issue or work["kind"] == "new-op" else ""
        if work["kind"] in ("new-op", "extend"):
            _check_owner(config, admission, me)
        assign = bool(admission.issue) and me.lower() not in {
            user["login"].lower() for user in admission.issue.get("assignees", [])}
        slug = round_slug(admission.stem, views.values())
        branch = _branch(work, values["TITLE"], admission.category, slug)
        worktree = config.worktree_root / f"tileops-{slug}"
        round_dir = config.rounds / slug
        plan = {"round": slug, **work, "branch": branch, "worktree": str(worktree), "round_dir": str(round_dir)}
        if dry_run:
            return plan
        if round_dir.exists() or worktree.exists():
            raise SystemExit(f"{round_dir} or {worktree} already exists")
        # A TileFoundry round enters its state graph at REQ.
        ledger.append("create", **plan, **({"phase": "REQ"} if work["kind"] in FOUNDRY_KINDS else {}))
    try:
        if assign:
            sh.run(["gh", "issue", "edit", str(number), "--repo", config.upstream, "--add-assignee", "@me"])
        sh.bootstrap_docker(config)
        sh.note(f"creating worktree {worktree} on {branch} at {admission.admitted[:12]}")
        created = sh.herdr("worktree", "create", "--cwd", str(config.tileops_repo),
                           "--branch", branch, "--base", admission.admitted, "--path", str(worktree),
                           "--label", slug, "--no-focus", "--json")
        workspace = created["workspace"]["workspace_id"]
        pane = (created.get("root_pane") or {}).get("pane_id") or created["panes"][0]["pane_id"]
        ledger.append("worktree", round=slug, workspace=workspace, pane=pane)
        tileops_base = sh.run(["git", "-C", worktree, "rev-parse", "HEAD"])

        sh.note("preparing TileFoundry")
        prepared = foundry.prepare(config, (worktree / "pyproject.toml").read_text())
        source, foundry_git = foundry.source(config, slug, prepared.commit)
        ledger.append("foundry", round=slug, foundry_source=str(source), tilefoundry_base=prepared.commit)

        # Only a perf round reads earlier attempts; a new op has none.
        prior = None
        if admission.queue is not None:
            sh.note("collecting earlier attempts")
            prior = history.collect(config, admission.queue.name, admission.queue.benchmark, work["workloads"],
                                    exclude=slug)
            values["PRIOR_ATTEMPTS"] = history.render(prior)
        elif work["kind"] == "perf":
            values["PRIOR_ATTEMPTS"] = ("This round works an issue, so topa collected no earlier attempts. "
                                        "Search the upstream `[Perf]` PRs for the op yourself.")
        values |= {
            "SLUG": slug, "ROUND_DIR": round_dir, "UPSTREAM": config.upstream, "BRANCH": branch,
            "TILEOPS_BASE": tileops_base, "TILEOPS_WORKTREE": worktree, "TILEFOUNDRY_BASE": prepared.commit,
            "TILEFOUNDRY_SOURCE": source, "KNOWLEDGE": knowledge(), "TOPA": topa, "REVIEWER": config.reviewer,
            "CHECKLISTS": review.DIRECTORY,
            **({"STATE_PATH": states.path(work["kind"])} if work["kind"] in FOUNDRY_KINDS else {}),
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
        if assign:
            sh.run(["gh", "issue", "edit", str(number), "--repo", config.upstream, "--remove-assignee", "@me"],
                   check=False)
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


# --- transition ---------------------------------------------------------------------


def transition(config: Config, target: str, reason: str, slug: str | None = None) -> dict[str, Any]:
    """Move a TileFoundry round to *target*: the edge must be in its kind's table, and its checks must pass."""
    current = find(config, slug)
    meta = current.meta
    kind = meta.get("kind", "perf")
    reason = " ".join(reason.split())
    if kind not in states.TRANSITIONS:
        raise SystemExit(f"a {kind} round has no states")
    if not reason:
        raise SystemExit("give the reason with --reason")
    with ledger.claim():
        view = ledger.rounds().get(current.slug, {})
        source = view.get("phase")
        allowed = states.TRANSITIONS[kind].get(source, frozenset())
        if target not in allowed:
            raise SystemExit(f"a {kind} round cannot move from {source} to {target}; "
                             f"it can move to {', '.join(sorted(allowed)) or 'nothing'}")
        evidence = gate.Evidence(current.dir, Path(meta["worktree"]), kind, meta.get("issue"),
                                 tuple(meta.get("workloads", ())), meta.get("measured", 0), op=view.get("op"),
                                 inventory=tuple(view.get("inventory", ())), published="pr" in view.get("history", ()))
        for check in states.checks(source, target):
            try:
                check(evidence)
            except gate.GateError as error:
                raise SystemExit(f"{source} -> {target}: FAIL: {error}") from error
        commit = sh.run(["git", "-C", meta["worktree"], "rev-parse", "HEAD"])
        return ledger.append("transition", round=current.slug,
                             **{"from": source, "to": target, "reason": reason, "commit": commit})


def resume(config: Config, slug: str, target: str, decision: str) -> dict[str, Any]:
    """A person's decision moves a STOPPED round back to an active state and tells its agent."""
    current = find(config, slug)
    kind = current.meta.get("kind", "perf")
    decision = " ".join(decision.split())
    if not decision:
        raise SystemExit("give the decision with --reason")
    if target not in states.FORWARD.get(kind, {}):
        raise SystemExit(f"a {kind} round resumes only at one of {', '.join(states.FORWARD.get(kind, {}))}")
    message = f"A person resumed this round at {target}: {decision} Continue from {target}."
    if len(message) > SAY_LIMIT:
        raise SystemExit(f"the decision is too long for a message; the limit is {SAY_LIMIT} characters")
    with ledger.claim():
        source = ledger.rounds().get(current.slug, {}).get("phase")
        if source != "STOPPED":
            raise SystemExit(f"{slug} is in {source}, not STOPPED")
        with (current.dir / "report.md").open("a") as report:
            report.write(f"\n- Decision of a person: resume at {target}. {decision}\n")
        commit = sh.run(["git", "-C", current.meta["worktree"], "rev-parse", "HEAD"])
        event = ledger.append("transition", round=current.slug, by="person",
                              **{"from": source, "to": target, "reason": decision, "commit": commit})
    say(config, slug, message)
    return event


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
            "round": view["round"], "state": view["state"], "phase": view.get("phase"),
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


def _worktrees_under(config: Config, folder: Path) -> list[Path]:
    listing = sh.run(["git", "-C", config.tileops_repo, "worktree", "list", "--porcelain"], check=False)
    paths = [Path(line.removeprefix("worktree ")) for line in listing.splitlines() if line.startswith("worktree ")]
    return [path for path in paths if path.resolve().is_relative_to(folder.resolve())]


def remove_worktrees_under(config: Config, folder: Path) -> tuple[int, list[Path]]:
    """Remove the TileOPs worktrees a round made inside its directory *folder*: how many went,
    and the ones that stayed, such as a locked one."""
    found = _worktrees_under(config, folder)
    for path in found:
        sh.run(["git", "-C", config.tileops_repo, "worktree", "remove", "--force", path], check=False)
    left = _worktrees_under(config, folder)
    return len(found) - len(left), left


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

    count, left = remove_worktrees_under(config, current.dir)
    if count:
        done.append(f"removed {count} worktree(s) the round made in {current.dir}")
    done += [f"could not remove {path}; remove it by hand" for path in left]
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
