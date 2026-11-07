"""Earlier attempts at an op, collected when a queue round starts and rendered into its brief.

Two sources: the rounds under `rounds` on this machine that worked the same op, and the
`[Perf]` PRs of the upstream repository. A PR whose body has an `Op:` line (see
`topa.gate.trailers`) matches when that line names the op; any other PR matches when its
title or body names the op, its benchmark module or one of its workloads, and is a candidate.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from topa import gate, sh

SECTIONS = ("Stop records", "TileLang knowledge")
DTYPE = re.compile(r"-(?:b?float\d+|float8\w*|u?int\d+)$")
MAX_PRS = 8
MAX_LOCAL = 4
MAX_EXCERPT = 800
MAX_REASONS = 3
MAX_SECTION = 6000
# GitHub search returns at most this many results.
SEARCH_CAP = 1000


def _section(text: str, title: str) -> str:
    """The body of the `## title` section of *text*, without its heading."""
    match = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match[1].strip() if match else ""


def _clip(text: str, limit: int = MAX_EXCERPT) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def _read_json(path: Path) -> dict[str, Any]:
    """The JSON object in *path*, or `{}` when it is missing, unreadable or not an object."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def local_attempts(rounds: Path, op: str, upstream: str, exclude: str) -> list[dict[str, Any]]:
    """The rounds under *rounds* that worked *op* of *upstream*, newest first, except *exclude*."""
    found = []
    for meta_file in rounds.glob("*/round.json") if rounds.is_dir() else ():
        meta = _read_json(meta_file)
        if meta.get("op") != op or meta.get("upstream", upstream) != upstream or meta.get("round") == exclude:
            continue
        folder = meta_file.parent
        report = folder / "report.md"
        try:
            text = report.read_text() if report.is_file() else ""
            mtime = meta_file.stat().st_mtime
        except (OSError, UnicodeError):
            continue
        found.append({
            "round": meta.get("round", folder.name), "dir": str(folder), "branch": meta.get("branch"),
            "tileops_base": meta.get("tileops_base"), "tilefoundry_base": meta.get("tilefoundry_base"),
            "result": _clip(str(_read_json(folder / "provenance.json").get("result") or ""), 80) or None,
            "tilefoundry_issue": _clip(str(_read_json(folder / "findings.json").get("issue") or ""), 200) or None,
            "report": str(report) if text else None,
            "excerpts": {title: _clip(body) for title in SECTIONS if (body := _section(text, title))},
            "mtime": mtime,
        })
    found.sort(key=lambda attempt: attempt["mtime"], reverse=True)
    return found


def _terms(op: str, benchmark: str, workloads: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    names = sorted({DTYPE.sub("", w) for w in workloads})
    terms = [("op", re.compile(rf"\b{re.escape(op)}\b")),
             ("benchmark", re.compile(rf"\b{re.escape(benchmark.rsplit('.', 1)[-1])}\b"))]
    return terms + [(f"workload {name}", re.compile(rf"\b{re.escape(name)}\b")) for name in names]


def matching_prs(prs: list[dict[str, Any]], op: str, benchmark: str, workloads: list[str]) -> list[dict[str, Any]]:
    """The `[Perf]` PRs of *prs* that name *op*, its benchmark module or a workload, newest first."""
    terms = _terms(op, benchmark, workloads)
    found = []
    for pr in prs:
        if not pr["title"].startswith("[Perf]"):
            continue
        lines = gate.trailers(pr.get("body"))
        if "Op" in lines:
            basis = ["Op line"] if lines["Op"] == op else []
        else:
            text = f"{pr['title']}\n{pr.get('body') or ''}"
            basis = [name for name, pattern in terms if pattern.search(text)]
        if basis:
            found.append({key: pr.get(key) for key in ("number", "title", "state", "url", "createdAt",
                                                        "mergedAt", "closedAt")}
                         | {"matched": basis, "result": lines.get("Result")})
    found.sort(key=lambda pr: pr["number"], reverse=True)
    return found


def collect(config: Any, op: str, benchmark: str, workloads: list[str], exclude: str) -> dict[str, Any]:
    """Everything the brief's prior attempts section is rendered from; written to `prior-attempts.json`."""
    history: dict[str, Any] = {
        "op": op, "benchmark": benchmark, "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "local": local_attempts(config.rounds, op, config.upstream, exclude),
        "local_scope": f"rounds under {config.rounds} on this machine",
        "pull_requests": [], "search_error": None, "search_truncated": False,
    }
    try:
        out = sh.run(["gh", "pr", "list", "--repo", config.upstream, "--state", "all",
                      "--limit", str(SEARCH_CAP), "--search", '"[Perf]" in:title sort:created-desc',
                      "--json", "number,title,state,url,body,createdAt,mergedAt,closedAt"], timeout=180)
        prs = json.loads(out or "[]")
        history["search_truncated"] = len(prs) >= SEARCH_CAP
        history["pull_requests"] = matching_prs(prs, op, benchmark, workloads)
    except (sh.CommandError, subprocess.TimeoutExpired, ValueError) as error:
        lines = str(error).strip().splitlines()
        history["search_error"] = (lines[-1] if lines else type(error).__name__)[:300]
    return history


def render(history: dict[str, Any]) -> str:
    """The brief's section: a bounded index of *history*, the full data in `prior-attempts.json`."""
    lines = []
    prs = history["pull_requests"]
    if history["search_error"]:
        lines.append(f"PR search unavailable: {history['search_error']}. Search the upstream PRs yourself.")
    elif history.get("search_truncated"):
        lines.append(f"The PR search stopped at GitHub's {SEARCH_CAP} results, so older PRs were not read. "
                     "Search the upstream PRs yourself for earlier attempts.")
    if not history["search_error"] and not prs:
        lines.append(f"No fetched `[Perf]` PR names `{history['op']}`, its benchmark or its workloads.")
    elif prs:
        lines += ["`[Perf]` PRs that name the op, its benchmark or a workload (candidates, newest first):", ""]
        for pr in prs[:MAX_PRS]:
            reasons = pr["matched"][:MAX_REASONS]
            if len(pr["matched"]) > MAX_REASONS:
                reasons.append(f"{len(pr['matched']) - MAX_REASONS} more")
            result = f", {_clip(pr['result'], 40)}" if pr.get("result") else ""
            lines.append(f"- [#{pr['number']}]({pr['url']}) {pr['state'].lower()}{result}: "
                         f"{_clip(pr['title'], 200)} (matched: {', '.join(reasons)})")
        if len(prs) > MAX_PRS:
            more = len(prs) - MAX_PRS
            lines.append(f"- {more} older {'match is' if more == 1 else 'matches are'} in `prior-attempts.json`.")
    lines.append("")
    local = history["local"]
    if not local:
        lines.append(f"No earlier round of this op in {history['local_scope']}.")
    else:
        lines += [f"Earlier rounds of this op in {history['local_scope']}, newest first:", ""]
        for attempt in local[:MAX_LOCAL]:
            state = attempt["result"] or ("no result recorded" if attempt["report"] else "incomplete: no report.md")
            issue = f", TileFoundry issue {attempt['tilefoundry_issue']}" if attempt["tilefoundry_issue"] else ""
            lines.append(f"- `{attempt['round']}` ({state}{issue}), report `{attempt['report'] or attempt['dir']}`")
            for title, excerpt in attempt["excerpts"].items():
                lines.append(f"  - {title}: " + " ".join(excerpt.split()))
        if len(local) > MAX_LOCAL:
            more = len(local) - MAX_LOCAL
            lines.append(f"- {more} older {'round is' if more == 1 else 'rounds are'} in `prior-attempts.json`.")
    text = "\n".join(lines).strip()
    if len(text) > MAX_SECTION:
        text = text[:MAX_SECTION].rstrip() + "\n\n[cut here; the rest is in `prior-attempts.json`]"
    return text
