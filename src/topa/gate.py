"""`topa gate`: the facts a program can check before a round opens its PR.

It does not judge the optimization. It checks that the records the brief asks for
exist, agree with each other, and match the TileOPs diff and the PR text.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path


class GateError(ValueError):
    """The round is missing mechanically verifiable PR evidence."""


# The kinds that build a kernel with TileFoundry and pass its records.
FOUNDRY_KINDS = ("perf", "new-op")
# The repository's own tooling, and for a perf round what it is measured against; a new op adds
# its benchmark and workloads, and changes only its manifest status line (`check_op`).
FORBIDDEN_ROOTS = {
    "perf": ("benchmarks/", "workloads/", "src/tileops/manifest/", "scripts/", ".github/"),
    "new-op": ("scripts/", ".github/"),
}
# The round's PR, from the files of its kind in `kit/round/`, filled in: `pr-template.md` is
# the title and a body of one Summary section, which becomes the squash commit's message;
# `pr-comments.md` holds one comment per section.
PR_FILE = "pr-template.md"
COMMENTS_FILE = "pr-comments.md"
FOUNDRY_SECTIONS = ("Test Plan", "TileFoundry Description", "Performance", "Result And Limitations",
                    "TileFoundry Findings")
COMMENT_SECTIONS = {"perf": FOUNDRY_SECTIONS, "new-op": FOUNDRY_SECTIONS, "general": ("Test Plan",)}
# TileOPs `scripts/ci/types.sh`: `[Type] description` or `[Type][Scope] description`.
GENERAL_TITLE = re.compile(r"\[(Bench|BugFix|Chore|CI|Design|Doc|Enhancement|Feat|Fix|Maintain|Perf|Refactor|"
                           r"Style|Test)\](\[[A-Za-z0-9_-]+\])? [A-Z`].+")
# The `[foundry]` origin makes TileOPs's auto-label workflow add the `foundry` label.
FOUNDRY_TITLE = {
    "perf": ("[Perf][foundry][Family] Description", re.compile(r"\[Perf\]\[foundry\]\[[A-Za-z0-9_-]+\] [A-Z`].+")),
    "new-op": ("[Feat][foundry][Family] Description", re.compile(r"\[Feat\]\[foundry\]\[[A-Za-z0-9_-]+\] [A-Z`].+")),
}
RESULTS = ("bar met", "below bar")
# A time ratio at or above this is noise: the nightly's threshold for a row behind.
NOISE = 0.99
# The last lines of a perf PR body, which `topa.history` reads to find earlier attempts at an op.
TRAILER = re.compile(r"^(Op|Benchmark|Result): *(\S.*?) *$", re.MULTILINE)
# How far a stated ratio may sit from the one its rounded times give.
RATIO_ROUNDING = 0.01
ENVIRONMENT_ROWS = ("image", "digest", "gpu", "timer", "timer step (us)")
# A template placeholder: `<...>` outside code, except the HTML tags a PR may use; or a
# code block whose only line is one.
PLACEHOLDER = re.compile(r"<(?!/?(?:br|b|i|em|strong|code|sub|sup|details|summary)\b)[^<>\n]+>")
EMPTY_BLOCK = re.compile(r"^```\w*\n<[^\n]*>\n```", re.MULTILINE)
LOCAL_PATH = re.compile(r"(?:/home/|/mnt/|/tmp/|/workspace/|/Users/|file://|[A-Za-z]:\\)")
MANIFEST = "src/tileops/manifest/"


def _real_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not value.strip().startswith("<")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise GateError(f"expected a JSON object: {path}")
    return value


def _required_file(root: Path, relative: object) -> Path:
    if not _real_text(relative):
        raise GateError("required artifact path is empty")
    path = (root / str(relative)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise GateError(f"artifact escapes the round directory: {relative}") from error
    if not path.is_file() or path.stat().st_size == 0:
        raise GateError(f"required artifact is missing or empty: {relative}")
    return path


def _record(root: Path, record: object, section: str, *, tilefoundry: bool = True) -> Path:
    """The report of a `{command, report}` record; a TileFoundry record's command runs `tilefoundry`."""
    if not isinstance(record, dict):
        raise GateError(f"{section} must be an object with a command and a report")
    command = record.get("command")
    if not _real_text(command) or "..." in command:
        raise GateError(f"{section} must record the exact command")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise GateError(f"{section} command cannot be parsed: {error}") from error
    if tilefoundry and Path(argv[0]).name != "tilefoundry":
        raise GateError(f"{section} must record a tilefoundry command")
    return _required_file(root, record.get("report"))


def _git(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    process = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=text)
    if process.returncode != 0:
        detail = process.stderr.strip() if text else process.stderr.decode(errors="replace").strip()
        raise GateError(detail or f"git {' '.join(args)} failed")
    return process


def _validate_production_diff(repo: Path, base: str, head: str, kind: str) -> list[str]:
    """The paths the diff changes; it must change some, with no authorship trailer, and none
    the kind forbids."""
    process = _git(repo, "diff", "--name-only", "-z", base, head, text=False)
    changed = [part.decode(errors="replace") for part in process.stdout.split(b"\0") if part]
    if not changed:
        raise GateError("base-to-head production diff is empty")
    forbidden = [path for path in changed if path.startswith(FORBIDDEN_ROOTS.get(kind, ()))]
    if forbidden:
        raise GateError("production diff changes forbidden paths: " + ", ".join(forbidden))
    messages = _git(repo, "log", "--format=%B", f"{base}..{head}").stdout
    if re.search(r"^(Co-authored-by|Signed-off-by):", messages, flags=re.MULTILINE | re.IGNORECASE):
        raise GateError("commit messages contain an extra authorship trailer")
    return changed


def sections(text: str) -> list[tuple[str, str]]:
    """Each `## ` section of *text* outside code blocks: its heading and its text, heading line included."""
    found: list[tuple[str, str]] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            found.append((line[3:].strip(), ""))
        if found:
            found[-1] = (found[-1][0], found[-1][1] + line)
    return found


def trailers(body: str) -> dict[str, str]:
    """The `Op:`, `Benchmark:` and `Result:` lines of a perf PR body."""
    return {key: value.strip() for key, value in TRAILER.findall((body or "").replace("\r\n", "\n"))}


def read_pr(round_dir: Path) -> tuple[str, str]:
    """The title and the body of the round's PR, from its filled-in template."""
    text = _required_file(round_dir, PR_FILE).read_text(encoding="utf-8").lstrip()
    first, _, body = text.partition("\n")
    if not first.startswith("# "):
        raise GateError(f"{PR_FILE} must start with the PR title as a `# ` line")
    return first[2:].strip(), body.strip() + "\n"


def read_comments(round_dir: Path, kind: str) -> list[tuple[str, str]]:
    """The PR's comments: one per section of the filled-in comments file, in order."""
    found = sections(_required_file(round_dir, COMMENTS_FILE).read_text(encoding="utf-8"))
    if tuple(name for name, _ in found) != COMMENT_SECTIONS[kind]:
        raise GateError(f"{COMMENTS_FILE} must have exactly the sections {', '.join(COMMENT_SECTIONS[kind])}, in order")
    empty = [name for name, text in found if not text.partition("\n")[2].strip()]
    if empty:
        raise GateError(f"{COMMENTS_FILE} sections {', '.join(empty)} are empty")
    return [(name, text.strip() + "\n") for name, text in found]


def _table(lines: list[str], header: str) -> list[list[str]]:
    """The cells of the Markdown table whose header line starts with *header*, header first."""
    start = next((i for i, line in enumerate(lines) if line.startswith(header)), None)
    if start is None:
        return []
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return [row for row in rows if not all(set(cell) <= set("-: ") for cell in row)]


def _time(value: object) -> float | None:
    """*value* as a measured time: a finite positive number, else None."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 and not isinstance(value, bool) else None


def _not_slower(table: list[list[str]], label: str) -> set[str]:
    """The rows of *table* faster than main beyond `NOISE`; a row slower beyond it, or without times, fails."""
    main, candidate = table[0].index("main (us)"), table[0].index("candidate (us)")
    faster = set()
    for row in table[1:]:
        name = " ".join(cell.strip("`") for cell in row[:label]) if label else row[0].strip("`")
        times = [_time(row[i]) if i < len(row) else None for i in (main, candidate)]
        if None in times:
            raise GateError(f"Performance: row {name!r} needs positive times for main and candidate")
        if times[0] / times[1] < NOISE:
            raise GateError(f"Performance: row {name!r} is slower than main: "
                            f"main / candidate {times[0] / times[1]:.4f}")
        if times[1] / times[0] < NOISE:
            faster.add(name)
    return faster


def _validate_performance(text: str, workloads: tuple[str, ...], measured: int, result: str,
                          main: bool = True) -> None:
    """The environment, the method, one row per workload of the op, and no row slower than main.

    The op's own table holds the bar: under `bar met` each row beats its comparator or ties
    it, or the DRAM floor, within one timer step; under `below bar` some row beats main beyond
    `NOISE`. Other ops that share a changed kernel only must not be slower than main.
    A new op has no *main*: its table has no main column, and `below bar` asks for nothing more.
    """
    lines = text.splitlines()
    values = {row[0]: row[1].strip("`") for row in _table(lines, "| Environment |")[1:] if len(row) >= 2}
    missing = [row for row in ENVIRONMENT_ROWS if row not in values]
    if missing:
        raise GateError(f"Performance: the environment table lacks {', '.join(missing)}")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", values["digest"]) is None:
        raise GateError("Performance: the digest must be the full sha256 of the image")
    step = _time(values["timer step (us)"])
    if step is None:
        raise GateError("Performance: the timer step must be a positive number of microseconds")
    if not any(line.startswith("Method: ") for line in lines):
        raise GateError("Performance: the Method line is missing")
    results = _table(lines, "| Workload |")
    columns = ["main (us)"] * main + ["candidate (us)", "DRAM floor (us)", "ratio"]
    if not results or not set(columns) <= set(results[0]):
        raise GateError(f"Performance: the results table needs {', '.join(columns[:-1])} and {columns[-1]} columns")
    faster = _not_slower(results, 0) if main else set()
    others = _table(lines, "| Op | Workload |")
    if others and not {"main (us)", "candidate (us)"} <= set(others[0]):
        raise GateError("Performance: the other-ops table needs main (us) and candidate (us) columns")
    if others:
        _not_slower(others, 2)
    names = [row[0].strip("`") for row in results[1:]] + [
        " ".join(cell.strip("`") for cell in row[:2]) for row in others[1:]]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise GateError(f"Performance: rows {', '.join(repeated)} appear more than once")
    seen = {row[0].strip("`") for row in results[1:]}
    if not seen or len(seen) < measured:
        raise GateError(f"Performance: the results table has {len(seen)} rows; the op has {measured} measured rows")
    absent = [name for name in workloads if name not in seen]
    if absent:
        raise GateError(f"Performance: the results table lacks the rows behind {', '.join(absent)}")
    # Each ratio column follows its comparator's time column.
    candidate, floor = results[0].index("candidate (us)"), results[0].index("DRAM floor (us)")
    ratios = [i for i, name in enumerate(results[0]) if name == "ratio"]
    for row in results[1:]:
        name = row[0].strip("`")
        ours, least = (_time(row[i]) if i < len(row) else None for i in (candidate, floor))
        # Checked here for every kind: a new op's table skips the main comparison that checks it too.
        if ours is None:
            raise GateError(f"Performance: row {name!r} needs a positive candidate time")
        if least is None:
            raise GateError(f"Performance: row {name!r} needs a positive DRAM floor")
        for i in ratios:
            comparator, stated = (_time(row[j]) if j < len(row) else None for j in (i - 1, i))
            if comparator is None or stated is None:
                raise GateError(f"Performance: row {name!r} needs a positive comparator time and ratio")
            if abs(stated - comparator / ours) > RATIO_ROUNDING:
                raise GateError(f"Performance: row {name!r} states ratio {stated}, "
                                f"its times give {comparator / ours:.4f}")
            # Rounded to the nanosecond, so a gap of exactly one step is a tie.
            if result == "bar met" and round(ours - max(comparator, least), 3) > step:
                raise GateError(f"Performance: row {name!r} is more than one timer step behind its "
                                "comparator and the floor, so the bar is not met")
    if main and result == "below bar" and not faster & seen:
        raise GateError(f"Performance: a below bar result must beat main on a row by candidate / main below {NOISE}")


def _validate_pr(round_dir: Path, kind: str, issue: int | None) -> None:
    """The PR text of either kind: filled in, no local path, the kind's title and sections, the issue link."""
    title, body = read_pr(round_dir)
    read_comments(round_dir, kind)
    texts = [(PR_FILE, f"{title}\n{body}"),
             (COMMENTS_FILE, _required_file(round_dir, COMMENTS_FILE).read_text(encoding="utf-8"))]
    for name, text in texts:
        left = PLACEHOLDER.search(re.sub(r"```.*?```", "", text, flags=re.DOTALL)) or EMPTY_BLOCK.search(text)
        if left:
            raise GateError(f"{name} still has the placeholder {left.group(0)}")
        if LOCAL_PATH.search(text):
            raise GateError(f"{name} exposes a local path")
    if kind in FOUNDRY_TITLE and not FOUNDRY_TITLE[kind][1].fullmatch(title):
        raise GateError(f"the {PR_FILE} title does not follow {FOUNDRY_TITLE[kind][0]}")
    if kind not in FOUNDRY_TITLE and not GENERAL_TITLE.fullmatch(title):
        raise GateError(f"the {PR_FILE} title does not follow [Type] Description or [Type][Scope] Description")
    if not body.startswith("## Summary\n") or [name for name, _ in sections(body)] != ["Summary"]:
        raise GateError(f"the {PR_FILE} body must be the Summary section alone")
    if issue is not None:
        link = f"{'Refs' if kind == 'perf' else 'Closes'} #{issue}"
        if not re.search(rf"^{link}$", body, flags=re.MULTILINE):
            raise GateError(f"{PR_FILE} must link the issue with the line {link}")


def _validate_comments(round_dir: Path, kind: str, final_hir: Path, result: str) -> None:
    comments = dict(read_comments(round_dir, kind))
    match = re.search(r"```python\s*\n(.*?)\n```", comments["TileFoundry Description"], flags=re.DOTALL)
    if not match:
        raise GateError("TileFoundry Description must contain one Python HIR block")
    if match.group(1).strip() != final_hir.read_text(encoding="utf-8").strip():
        raise GateError("TileFoundry Description is not the exact final HIR")
    stated = comments["Result And Limitations"].splitlines()[1:]
    if next((line.strip() for line in stated if line.strip()), None) != f"**{result}**":
        raise GateError(f"Result And Limitations must open with **{result}**, the result of provenance.json")


def _validate_findings(round_dir: Path) -> None:
    data = _load_json(_required_file(round_dir, "findings.json"))
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise GateError("findings.json must contain a findings list")
    # A round reports its findings to TileFoundry as one issue, so the link is one
    # field for the whole file rather than one per finding.
    issue = data.get("issue")
    if findings:
        if not isinstance(issue, str) or not re.fullmatch(r"https://github\.com/tile-ai/TileFoundry/issues/\d+", issue):
            raise GateError("findings.json issue must be the one tile-ai/TileFoundry issue URL "
                            "that holds every finding of this round")
    elif issue is not None:
        raise GateError("findings.json issue must be null when there is no finding")
    for finding in findings:
        if not isinstance(finding, dict) or not _real_text(finding.get("id")):
            raise GateError("every finding must have an id")
        for field in ("classification", "command", "expected", "actual", "workaround_cost", "public_surface"):
            if not _real_text(finding.get(field)):
                raise GateError(f"every finding must record {field}")
        if not isinstance(finding.get("affected_workloads"), list):
            raise GateError("every finding must record affected_workloads")
        _required_file(round_dir, finding.get("reproducer"))


def _list(provenance: dict, name: str) -> list:
    value = provenance.get(name)
    if not isinstance(value, list) or not value:
        raise GateError(f"provenance {name} must be a non-empty list")
    return value


@dataclass
class Evidence:
    """The records of a perf or new-op round, which the checks on its state edges and the gate read.

    A perf round on the wiki queue checks its rows behind, *queue_rows*, and its count of measured
    rows, *queue_measured*; a new-op round checks every case of its admitted *inventory*; any
    other round takes its rows from `workloads` in `provenance.json`. *op*, *inventory* and
    *published* come from the ledger.
    """

    dir: Path
    repo: Path
    kind: str = "perf"
    issue: int | None = None
    queue_rows: tuple[str, ...] = ()
    queue_measured: int = 0
    head: str = "HEAD"
    op: str | None = None
    inventory: tuple[str, ...] = ()
    published: bool = False

    @cached_property
    def provenance(self) -> dict:
        return _load_json(_required_file(self.dir, "provenance.json"))

    @cached_property
    def base(self) -> str:
        base = self.provenance.get("tileops_base")
        if not isinstance(base, str) or re.fullmatch(r"[0-9a-f]{40}", base) is None:
            raise GateError("provenance tileops_base must be the full admitted commit")
        return base

    @property
    def from_queue(self) -> bool:
        return self.kind == "perf" and self.issue is None

    @cached_property
    def workloads(self) -> tuple[str, ...]:
        if self.from_queue:
            return self.queue_rows
        if self.kind == "new-op":
            if not self.inventory:
                raise GateError("the round has no admitted inventory")
            return self.inventory
        workloads = tuple(_list(self.provenance, "workloads"))
        if not all(_real_text(name) for name in workloads):
            raise GateError("provenance workloads must name the workloads the issue or the manifest lists")
        return workloads

    @property
    def measured(self) -> int:
        return self.queue_measured if self.from_queue else len(self.workloads)


def check_reference(evidence: Evidence) -> None:
    """HIR -> FLOOR: the HIR was checked against the reference."""
    _record(evidence.dir, evidence.provenance.get("reference_check"), "reference_check")


def check_floor(evidence: Evidence) -> None:
    """FLOOR -> OP or IMPL: a floor record per row, and at least two placements with exactly one chosen."""
    floor = _list(evidence.provenance, "floor")
    absent = sorted(set(evidence.workloads) - {record.get("workload") for record in floor if isinstance(record, dict)})
    if absent:
        raise GateError(f"floor lacks the rows behind {', '.join(absent)}")
    for index, record in enumerate(floor):
        _record(evidence.dir, record, f"floor[{index}]")
    placements = _list(evidence.provenance, "placements")
    if len(placements) < 2 or sum(isinstance(p, dict) and p.get("chosen") is True for p in placements) != 1:
        raise GateError("placements must compare at least two placements and choose exactly one")
    for index, record in enumerate(placements):
        if not isinstance(record, dict) or not _real_text(record.get("placement")):
            raise GateError(f"placements[{index}] must describe its placement")
        _record(evidence.dir, record, f"placements[{index}]")


def _implemented(text: str, op: str | None) -> str | None:
    """*text* with the `status: spec-only` line of the entry *op* set to `implemented`; None when it has none."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.fullmatch(rf"{re.escape(op or '')}:\s*(#.*)?\n?", line)),
                 None)
    if not op or start is None:
        return None
    indent = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            return None
        # Only a key of the entry itself, not one nested deeper, is its status.
        indent = indent or line[:len(line) - len(line.lstrip())]
        if re.fullmatch(rf"{indent}status:\s*spec-only\s*\n?", line):
            lines[i] = line.replace("spec-only", "implemented")
            return "".join(lines)
    return None


def check_op(evidence: Evidence) -> None:
    """OP -> IMPL: the manifest changes only by the op's `status: spec-only` turning `implemented`, and
    the new tests failed on the base."""
    changed = _git(evidence.repo, "diff", "--name-only", evidence.base, "--", MANIFEST).stdout.split()
    path = evidence.repo / changed[0] if len(changed) == 1 else None
    if path is None or not path.is_file() or path.read_text(encoding="utf-8") != _implemented(
            _git(evidence.repo, "show", f"{evidence.base}:{changed[0]}").stdout, evidence.op):
        raise GateError(f"the {MANIFEST} diff must set the status of {evidence.op} from spec-only to "
                        "implemented, and change nothing else")
    _record(evidence.dir, evidence.provenance.get("base_tests"), "base_tests", tilefoundry=False)


def check_implementation(evidence: Evidence) -> None:
    """IMPL -> CLEAN: the implementation records and the Performance table under the bar of the result.

    A perf round also changes the production kernel it declares; a new op may wrap an existing
    kernel, and its `below bar` needs the stop records.
    """
    provenance = evidence.provenance
    result = provenance.get("result")
    if result not in RESULTS:
        raise GateError(f"provenance result must be one of: {', '.join(RESULTS)}")
    _required_file(evidence.dir, provenance.get("final_hir"))
    _required_file(evidence.dir, provenance.get("runtime_twin"))
    _record(evidence.dir, provenance.get("schedule"), "schedule")
    _record(evidence.dir, provenance.get("twin_check"), "twin_check")
    for index, iteration in enumerate(_list(provenance, "iterations")):
        if not isinstance(iteration, dict) or not all(_real_text(iteration.get(k)) for k in ("change", "basis")):
            raise GateError(f"iterations[{index}] must record its change and its basis")
        for key in ("predicted_us", "measured_us"):
            if not isinstance(iteration.get(key), (int, float)) or _time(iteration[key]) is None:
                raise GateError(f"iterations[{index}] must record a positive {key}")
    _record(evidence.dir, provenance.get("benchmark"), "benchmark", tilefoundry=False)
    if evidence.kind == "perf":
        declaration = provenance.get("production_kernel")
        if not _real_text(declaration):
            raise GateError("provenance must declare production_kernel as src/tileops/<path>.py:<Symbol>")
        path, _, symbol = declaration.rpartition(":")
        if not path.startswith("src/tileops/") or not path.endswith(".py") or not _real_text(symbol):
            raise GateError("production_kernel must be src/tileops/<path>.py:<Symbol>")
        if path not in _git(evidence.repo, "diff", "--name-only", evidence.base).stdout.splitlines():
            raise GateError("the declared production kernel file is unchanged")
    performance = dict(sections(_required_file(evidence.dir, COMMENTS_FILE).read_text(encoding="utf-8")))
    if "Performance" not in performance:
        raise GateError(f"{COMMENTS_FILE} lacks the Performance section")
    _validate_performance(performance["Performance"], evidence.workloads, evidence.measured, result,
                          main=evidence.kind == "perf")
    if evidence.kind == "new-op" and result == "below bar":
        check_stop_records(evidence)


def check_published(evidence: Evidence) -> None:
    """REVIEW -> PR: the round has published its PR; before that, REVIEW exits to FINAL."""
    if not evidence.published:
        raise GateError("the PR is not published yet; move to FINAL")


def check_stop_records(evidence: Evidence) -> None:
    """* -> STOPPED: `report.md` holds the stop records."""
    found = dict(sections(_required_file(evidence.dir, "report.md").read_text(encoding="utf-8")))
    if not found.get("Stop records", "").partition("\n")[2].strip():
        raise GateError("report.md lacks a non-empty `## Stop records` section")


def validate_round(round_dir: Path, tileops_repo: Path, head: str = "HEAD", workloads: tuple[str, ...] = (),
                   measured: int = 0, kind: str = "perf", issue: int | None = None,
                   op: str | None = None, benchmark: str | None = None, trailers: bool = True,
                   inventory: tuple[str, ...] = ()) -> dict:
    """Validate only facts a program can decide without interpreting optimization quality.

    Every round binds its records to the commit it ships, records its tests and reviews,
    fills in its PR text, and keeps out of the paths its kind forbids. A perf or new-op
    round also passes the checks of its state edges, the findings and the PR comments:
    a perf round on the queue passes its rows behind as *workloads* and its count of
    measured rows as *measured*; a new-op round passes its admitted *inventory*; any other
    round takes its rows from `provenance.json`.
    Its PR body ends with `Op:`, `Benchmark:` and `Result:` lines that equal *op*,
    *benchmark* and the provenance result where the round records them.
    """
    evidence = Evidence(round_dir.resolve(), tileops_repo.resolve(), kind, issue, workloads, measured, head,
                        op=op, inventory=inventory)
    provenance, base, repo = evidence.provenance, evidence.base, evidence.repo
    if _git(repo, "rev-parse", f"{base}^{{commit}}").stdout.strip() != base:
        raise GateError("provenance tileops_base is not available in the TileOPs repository")
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise GateError("TileOPs worktree must be clean and committed before the PR gate")
    if provenance.get("candidate_commit") != _git(repo, "rev-parse", head).stdout.strip():
        raise GateError("provenance candidate_commit must be the commit the PR ships; measure it again")
    _record(evidence.dir, provenance.get("tests"), "tests", tilefoundry=False)
    for review in _list(provenance, "reviews"):
        _required_file(evidence.dir, review)
    _required_file(evidence.dir, "report.md")
    _validate_production_diff(repo, base, head, kind)
    _validate_pr(evidence.dir, kind, issue)
    if kind in FOUNDRY_KINDS:
        _validate_perf(evidence)
        if trailers:
            _validate_trailers(evidence.dir, {"Op": op, "Benchmark": benchmark, "Result": provenance["result"]})
    return provenance


def _validate_trailers(round_dir: Path, recorded: dict[str, str | None]) -> None:
    """Each trailer is present, and equals what the round records when it records one."""
    body = read_pr(round_dir)[1]
    last = re.split(r"\n[ \t]*\n", body.strip())[-1].splitlines()
    found = trailers("\n".join(last))
    if len(last) != len(recorded) or len(found) != len(recorded):
        raise GateError(f"{PR_FILE} must end with one paragraph of exactly the "
                        f"{', '.join(f'`{key}:`' for key in recorded)} lines")
    for key, value in recorded.items():
        if key not in found:
            raise GateError(f"{PR_FILE} must end with a `{key}:` line")
        if value is not None and found[key] != value:
            raise GateError(f"{PR_FILE} says `{key}: {found[key]}`; the round records `{value}`")


def _validate_perf(evidence: Evidence) -> None:
    """The findings, the checks of every state edge of the kind, and the PR comments of a perf or new-op round."""
    _validate_findings(evidence.dir)
    foundry_base = evidence.provenance.get("tilefoundry_base")
    if not isinstance(foundry_base, str) or re.fullmatch(r"[0-9a-f]{40}", foundry_base) is None:
        raise GateError("provenance tilefoundry_base must be the full TileFoundry commit")
    check_reference(evidence)
    check_floor(evidence)
    if evidence.kind == "new-op":
        check_op(evidence)
    check_implementation(evidence)
    final_hir = _required_file(evidence.dir, evidence.provenance["final_hir"])
    _validate_comments(evidence.dir, evidence.kind, final_hir, evidence.provenance["result"])
