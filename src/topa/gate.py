"""`topa gate`: the facts a program can check before a round opens its PR.

It does not judge the optimization. It checks that the evidence the brief asks for
exists, agrees with itself, and matches the TileOPs diff and the PR text.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path


class GateError(ValueError):
    """The round is missing mechanically verifiable PR evidence."""


ALLOWED_CHANGE_ROOTS = (
    # C++/CUDA a kernel needs beyond what TileLang emits lives here, pulled in with
    # tileops._csrc.csrc_include; TileOPs' native-source-strings-in-csrc hook rejects
    # it inline in a .py file.
    "src/tileops/csrc/",
    "src/tileops/kernels/",
    "src/tileops/manifest/spec/",
    "src/tileops/ops/",
    # A new op's roofline functions join __all__ here when its entry becomes implemented.
    "src/tileops/perf/formulas.py",
    "tests/kernels/",
    "tests/ops/",
)
PR_SECTIONS = (
    "Summary",
    "TileFoundry Description",
    "Performance",
    "Result And Limitations",
)
# The round's PR: `kit/round/pr-template.md`, filled in. Its first line is the title.
PR_FILE = "pr-template.md"
# The rows of each Operator block's environment table, besides the external stack's.
ENVIRONMENT_ROWS = ("image", "digest", "gpu", "driver", "cuda", "torch", "tilelang", "timer")
RATIO_LINE = "Ratio in comparator columns: implementation / candidate."
MARKERS = ("&#x1F7E2;", "&#x1F534;")
# A template placeholder: `<...>` outside code, except the HTML tags a PR body may use.
PLACEHOLDER = re.compile(r"<(?!/?(?:br|b|i|em|strong|code|sub|sup|details|summary)\b)[^<>\n]+>")


def _real_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not value.strip().startswith("<")
    )


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


def _command(record: object, section: str, command: str) -> list[str]:
    if not isinstance(record, dict):
        raise GateError(f"{section} must be an object")
    value = record.get("command")
    if not _real_text(value) or "..." in str(value):
        raise GateError(f"{section} must record the exact command")
    try:
        argv = shlex.split(str(value))
    except ValueError as error:
        raise GateError(f"{section} command cannot be parsed: {error}") from error
    pairs = [
        (Path(argv[index]).name, argv[index + 1]) for index in range(len(argv) - 1)
    ]
    if ("tilefoundry", command) not in pairs:
        raise GateError(f"{section} must run tilefoundry {command}")
    return argv


def _json_report(root: Path, record: dict, section: str) -> dict:
    report = _load_json(_required_file(root, record.get("report")))
    if not report:
        raise GateError(f"{section} report is empty")
    return report


def _git(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=text,
    )
    if process.returncode != 0:
        detail = (
            process.stderr.strip()
            if text
            else process.stderr.decode(errors="replace").strip()
        )
        raise GateError(detail or f"git {' '.join(args)} failed")
    return process


def _changed_paths(repo: Path, base: str, head: str) -> list[str]:
    process = _git(repo, "diff", "--name-only", "-z", base, head, text=False)
    return [
        part.decode(errors="replace") for part in process.stdout.split(b"\0") if part
    ]


def _validate_production_diff(
    repo: Path, base: str, head: str, declaration: str
) -> None:
    changed = _changed_paths(repo, base, head)
    if not changed:
        raise GateError("base-to-head production diff is empty")
    disallowed = [path for path in changed if not path.startswith(ALLOWED_CHANGE_ROOTS)]
    if disallowed:
        raise GateError(
            "production diff changes forbidden paths: " + ", ".join(disallowed)
        )
    try:
        path, symbol = declaration.rsplit(":", 1)
    except ValueError as error:
        raise GateError("production_kernel must be relative-path:symbol") from error
    if (
        not path.startswith("src/tileops/kernels/")
        or not path.endswith(".py")
        or not _real_text(symbol)
    ):
        raise GateError(
            "production_kernel must name a symbol under src/tileops/kernels/"
        )
    if path not in changed:
        raise GateError("the declared production kernel file is unchanged")

    messages = _git(repo, "log", "--format=%B", f"{base}..{head}").stdout
    if re.search(
        r"^(Co-authored-by|Signed-off-by):",
        messages,
        flags=re.MULTILINE | re.IGNORECASE,
    ):
        raise GateError("commit messages contain an extra authorship trailer")


def read_pr(round_dir: Path) -> tuple[str, str]:
    """The title and the body of the round's PR, from its filled-in template."""
    text = _required_file(round_dir, PR_FILE).read_text(encoding="utf-8").lstrip()
    first, _, body = text.partition("\n")
    if not first.startswith("# "):
        raise GateError(f"{PR_FILE} must start with the PR title as a `# ` line")
    return first[2:].strip(), body.strip() + "\n"


def _sections(body: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)$", body, flags=re.MULTILINE)
    return {name.strip(): text for name, text in zip(parts[1::2], parts[2::2])}


def _table_rows(lines: list[str], start: int) -> list[list[str]]:
    """The cells of the Markdown table whose header is lines[start], header first."""
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return [row for row in rows if not all(set(cell) <= set("-: ") for cell in row)]


def _validate_operator(block: str) -> None:
    """One `Operator:` block of the Performance section, as the template lays it out."""
    lines = block.splitlines()
    name = lines[0].strip()
    if re.fullmatch(r"Operator: `[A-Za-z0-9_]+Op`", name) is None:
        raise GateError(f"Performance needs `Operator: `<Op name>`` lines, got {name!r}")
    try:
        environment = _table_rows(lines, next(i for i, line in enumerate(lines)
                                              if line.startswith("| Environment |")))
    except StopIteration:
        raise GateError(f"{name}: the environment table is missing") from None
    values = {row[0]: row[1].strip("`") for row in environment[1:] if len(row) >= 2}
    missing = [row for row in ENVIRONMENT_ROWS if row not in values]
    if missing:
        raise GateError(f"{name}: the environment table lacks {', '.join(missing)}")
    if len(values) <= len(ENVIRONMENT_ROWS):
        raise GateError(f"{name}: the environment table lacks the external stack row")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", values["digest"]) is None:
        raise GateError(f"{name}: the digest must be the full sha256 of the image")
    if not any(line.startswith("Method: ") for line in lines):
        raise GateError(f"{name}: the Method line is missing")
    if not any(line.startswith(RATIO_LINE) for line in lines):
        raise GateError(f"{name}: the ratio line is missing")
    try:
        results = _table_rows(lines, next(i for i, line in enumerate(lines)
                                          if line.startswith("| Workload |")))
    except StopIteration:
        raise GateError(f"{name}: the results table is missing") from None
    if "TileFoundry candidate (ms)" not in results[0]:
        raise GateError(f"{name}: the results table lacks the TileFoundry candidate column")
    if len(results) < 3 or results[-1][0] != "geometric mean":
        raise GateError(f"{name}: the results table must end with a geometric mean row")
    for row in results[1:]:
        if not any(marker in cell for cell in row for marker in MARKERS):
            raise GateError(f"{name}: results row {row[0]!r} has no comparison marker")


def _validate_pr(round_dir: Path, final_hir: Path, classification: str) -> None:
    title, body = read_pr(round_dir)
    prose = re.sub(r"```.*?```", "", title + "\n" + body, flags=re.DOTALL)
    left = PLACEHOLDER.search(prose)
    if left:
        raise GateError(f"{PR_FILE} still has the placeholder {left.group(0)}")
    if not re.fullmatch(r"\[Perf\]\[foundry\]\[[A-Za-z0-9_-]+\] [A-Z`].+", title):
        raise GateError(
            f"the {PR_FILE} title does not follow [Perf][foundry][Scope] Description"
        )
    headings = tuple(re.findall(r"^## (.+)$", body, flags=re.MULTILINE))
    if headings != PR_SECTIONS:
        raise GateError(
            f"{PR_FILE} must contain exactly the four ordered public sections"
        )
    sections = _sections(body)
    match = re.search(r"```python\s*\n(.*?)\n```", sections["TileFoundry Description"],
                      flags=re.DOTALL)
    if not match:
        raise GateError("TileFoundry Description must contain one Python HIR block")
    if match.group(1).strip() != final_hir.read_text(encoding="utf-8").strip():
        raise GateError("TileFoundry Description is not the exact final HIR")
    if "tf.schedule(" not in match.group(1):
        raise GateError("the final HIR must carry its tf.schedule instruction choices")
    blocks = re.split(r"^(?=Operator: )", sections["Performance"], flags=re.MULTILINE)[1:]
    if not blocks:
        raise GateError("Performance needs one Operator block per public Op")
    for block in blocks:
        _validate_operator(block)
    stated = sections["Result And Limitations"].strip().splitlines()[0].strip()
    if stated != f"**{classification}**":
        raise GateError(
            f"Result And Limitations must open with **{classification}**, the "
            "classification of provenance.json"
        )
    if re.search(
        r"(?:/home/|/mnt/|/tmp/|/workspace/|/Users/|file://|[A-Za-z]:\\)", title + body
    ):
        raise GateError(f"{PR_FILE} exposes a local path")


def _validate_findings(round_dir: Path) -> None:
    data = _load_json(_required_file(round_dir, "findings.json"))
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise GateError("findings.json must contain a findings list")
    # A round reports its findings to TileFoundry as one issue, so the link is one
    # field for the whole file rather than one per finding.
    issue = data.get("issue")
    if findings:
        if not isinstance(issue, str) or re.fullmatch(
            r"https://github\.com/tile-ai/TileFoundry/issues/\d+", issue
        ) is None:
            raise GateError(
                "findings.json issue must be the one tile-ai/TileFoundry issue URL "
                "that holds every finding of this round"
            )
    elif issue is not None:
        raise GateError("findings.json issue must be null when there is no finding")
    for finding in findings:
        if not isinstance(finding, dict) or not _real_text(finding.get("id")):
            raise GateError("every finding must have an id")
        if not _real_text(finding.get("classification")):
            raise GateError("every finding must record a classification")
        for field in (
            "command",
            "expected",
            "actual",
            "workaround_cost",
            "public_surface",
        ):
            if not _real_text(finding.get(field)):
                raise GateError(f"every finding must record {field}")
        if not isinstance(finding.get("affected_workloads"), list):
            raise GateError("every finding must record affected_workloads")
        _required_file(round_dir, finding.get("reproducer"))


def validate_round(round_dir: Path, tileops_repo: Path, head: str = "HEAD") -> dict:
    """Validate only facts a program can decide without interpreting optimization quality."""
    round_dir = round_dir.resolve()
    tileops_repo = tileops_repo.resolve()
    provenance = _load_json(_required_file(round_dir, "provenance.json"))
    base = provenance.get("tileops_base")
    if not isinstance(base, str) or re.fullmatch(r"[0-9a-f]{40}", base) is None:
        raise GateError("provenance tileops_base must be the full admitted commit")
    if _git(tileops_repo, "rev-parse", f"{base}^{{commit}}").stdout.strip() != base:
        raise GateError(
            "provenance tileops_base is not available in the TileOPs repository"
        )
    if _git(tileops_repo, "status", "--porcelain").stdout.strip():
        raise GateError("TileOPs worktree must be clean and committed before the PR gate")

    foundry_base = provenance.get("tilefoundry_base")
    if not isinstance(foundry_base, str) or re.fullmatch(r"[0-9a-f]{40}", foundry_base) is None:
        raise GateError("provenance tilefoundry_base must be the full TileFoundry commit")

    classification = provenance.get("classification")
    if classification not in {
        "measured SOTA",
        "improvement without SOTA",
        "no improvement",
    }:
        raise GateError(
            "classification must be measured SOTA, improvement without SOTA, or no improvement"
        )
    if classification == "no improvement":
        raise GateError("no-improvement rounds cannot open or retain a performance PR")

    final_hir_value = provenance.get("final_hir")
    final_hir = _required_file(round_dir, final_hir_value)
    _required_file(round_dir, provenance.get("runtime_twin"))
    production_kernel = provenance.get("production_kernel")
    if not _real_text(production_kernel):
        raise GateError(
            "provenance must declare production_kernel as relative-path:symbol"
        )

    check_record = provenance.get("tilefoundry_check")
    _command(check_record, "tilefoundry_check", "check")
    if _json_report(round_dir, check_record, "tilefoundry_check").get("passed") is not True:
        raise GateError("tilefoundry check report must say passed=true")

    iterations = provenance.get("iterations")
    if not isinstance(iterations, list) or len(iterations) < 2:
        raise GateError("at least two measured HIR iterations are required")
    verdicts: list[str] = []
    kept_hir: str | None = None
    for index, iteration in enumerate(iterations, start=1):
        section = f"iterations[{index}]"
        if not isinstance(iteration, dict):
            raise GateError(f"{section} must be an object")
        for field in ("name", "hypothesis", "placement"):
            if not _real_text(iteration.get(field)):
                raise GateError(f"{section} must record {field}")
        hir_value = iteration.get("hir")
        _required_file(round_dir, hir_value)
        verdict = iteration.get("verdict")
        if verdict not in {"kept", "rejected"}:
            raise GateError(f"{section} verdict must be kept or rejected")
        verdicts.append(str(verdict))
        if verdict == "kept":
            kept_hir = str(hir_value)
        measured_ms = iteration.get("measured_ms")
        if (
            not isinstance(measured_ms, (int, float))
            or isinstance(measured_ms, bool)
            or measured_ms <= 0
        ):
            raise GateError(f"{section} must record a positive measured_ms")

        analysis = iteration.get("analysis")
        analysis_argv = _command(analysis, f"{section}.analysis", "analyze")
        if "--json" not in analysis_argv:
            raise GateError(f"{section}.analysis must record JSON evidence")
        _json_report(round_dir, analysis, f"{section}.analysis")

    if verdicts.count("kept") != 1 or "rejected" not in verdicts:
        raise GateError(
            "iterations must contain exactly one kept result and at least one rejection"
        )
    if kept_hir != final_hir_value:
        raise GateError("the kept iteration must point to final_hir")

    decisions = provenance.get("decisions")
    required_decisions = (
        "analysis_fact",
        "hir_change",
        "kernel_change",
    )
    if not isinstance(decisions, list) or not decisions:
        raise GateError("at least one evidence-to-kernel decision is required")
    for decision in decisions:
        if not isinstance(decision, dict) or any(
            not _real_text(decision.get(field)) for field in required_decisions
        ):
            raise GateError(
                "every decision must record analysis, HIR, and kernel facts"
            )

    for section in ("correctness", "benchmark", "profile"):
        record = provenance.get(section)
        if not isinstance(record, dict):
            raise GateError(f"provenance is missing {section}")
        if not _real_text(record.get("command")) or "..." in str(record.get("command")):
            raise GateError(f"{section} must record the exact command")
        _required_file(round_dir, record.get("report"))

    _required_file(round_dir, "report.md")
    _validate_findings(round_dir)
    _validate_production_diff(tileops_repo, base, head, str(production_kernel))
    _validate_pr(round_dir, final_hir, classification)
    return provenance
