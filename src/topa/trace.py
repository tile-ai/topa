"""`topa round trace`: a round's records as one time-ordered trace, read-only.

The full trace holds every entry: the round's phases from the ledger, the TileFoundry
analyses `provenance.json` records, the measurements of `topa round measure`, and the
agent's thinking and tool calls from the archived session. The published trace keeps the
phases, analyses and measurements, and counts what it leaves out.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from topa import ledger, measure
from topa.gate import FOUNDRY_KINDS

SCHEMA = 1
ROOFLINE = re.compile(r"roofline ideal-ns=(\d+) bound-by=(\w+)")
PUBLISHED = ("phase", "foundry", "measure")


def _local(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _instant(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _roofline(report: Path) -> dict[str, Any]:
    """`ideal_ns` and `bound_by` of a TileFoundry analysis report, in its JSON or text form."""
    text = report.read_text(encoding="utf-8", errors="replace")
    try:
        functions = json.loads(text).get("functions", [])
        found = functions[0]["function_records"]["roofline"] if functions else {}
        return {"ideal_ns": found.get("ideal_ns"), "bound_by": found.get("bound_by")}
    except (ValueError, AttributeError, KeyError, IndexError, TypeError):
        match = ROOFLINE.search(text)
        return {"ideal_ns": int(match[1]), "bound_by": match[2]} if match else {"ideal_ns": None, "bound_by": None}


def _foundry(round_dir: Path, provenance: dict[str, Any]) -> list[dict[str, Any]]:
    """The reference check and floor analyses, timed by their report files, which is when each was last written."""
    found = []
    records = [("reference", provenance.get("reference_check"))]
    records += [("floor", record) for record in provenance.get("floor") or []]
    for kind, record in records:
        report = round_dir / str((record or {}).get("report") or "")
        if not isinstance(record, dict) or not report.is_file():
            continue
        op, _, case_id = str(record.get("workload") or "").partition(" ")
        found.append({"ts": _local(report.stat().st_mtime), "t": "foundry", "kind": kind,
                      "op": op or None, "case_id": case_id or None, "command": record.get("command"),
                      **_roofline(report), "ts_source": "report-file"})
    return found


def _session(transcripts: Path) -> tuple[list[dict[str, Any]], int | None]:
    """The agent's thinking and tool calls, and the tokens it generated, from every transcript under *transcripts*."""
    entries, tokens, seen = [], 0, set()
    for path in sorted(transcripts.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "assistant" or not record.get("timestamp"):
                continue
            message = record.get("message") or {}
            # A message is written once per content block, each copy with the whole message's usage.
            if message.get("id") not in seen:
                seen.add(message.get("id"))
                tokens += (message.get("usage") or {}).get("output_tokens") or 0
            ts = _instant(record["timestamp"]).astimezone().isoformat(timespec="seconds")
            for block in message.get("content") or []:
                if block.get("type") == "thinking":
                    entries.append({"ts": ts, "t": "think", "text": block.get("thinking", "")})
                elif block.get("type") == "tool_use":
                    entries.append({"ts": ts, "t": "tool", "name": block.get("name"),
                                    "arg": json.dumps(block.get("input"), ensure_ascii=False)})
    return entries, tokens if seen else None


def export(round_dir: Path, view: dict[str, Any], transcripts: Path, *, published: bool = False) -> dict[str, Any]:
    """The round's trace; *view* is its ledger view, *transcripts* the folder of its session transcripts."""
    meta = {**view, **_json(round_dir / "round.json")}
    provenance = _json(round_dir / "provenance.json")
    slug = meta["round"]
    entries = []
    for record in ledger.events():
        if record.get("round") != slug:
            continue
        if record["event"] == "create" and record.get("phase"):
            entries.append({"ts": record["ts"], "t": "phase", "to": record["phase"], "reason": None})
        elif record["event"] == "transition":
            entries.append({"ts": record["ts"], "t": "phase", "to": record["to"], "reason": record.get("reason")})
    entries += _foundry(round_dir, provenance)
    found = measure.records(round_dir)
    entries += [{"ts": r["ts"], "t": "measure", "title": r["title"], "commit": r["commit"],
                 "busy_card": r["busy_card"], "rows": r["rows"]} for r in found]
    session, tokens = _session(transcripts)
    entries += session
    entries.sort(key=lambda entry: _instant(entry["ts"]))
    start = next((r for r in found if r["base"]), None)
    floors = measure.floors(found)
    refs = [{**row, "dram_floor_us": floors.get(measure.name(row["op"], row["case_id"]))}
            for row in (start or {}).get("rows", [])]
    # A row the base cannot run, such as a new op's, is unavailable there.
    listed = {measure.name(row["op"], row["case_id"]) for row in refs}
    for record in found:
        for row in record["rows"]:
            key = measure.name(row["op"], row["case_id"])
            if key not in listed:
                listed.add(key)
                refs.append({"op": row["op"], "case_id": row["case_id"], "status": "unavailable", "us": None,
                             "dram_floor_us": floors.get(key)})
    issue, wiki = meta.get("issue"), meta.get("wiki")
    trace = {
        "schema": SCHEMA, "round": slug,
        "source": {"kind": "issue", "ref": issue} if issue else {"kind": "wiki", "ref": wiki} if wiki
        else {"kind": "none", "ref": None},
        "ops": sorted({*meta.get("ops", ()), *filter(None, [meta.get("op")])}
                      or set((provenance.get("bench") or {}).keys())),
        "method": "foundry" if meta.get("kind") in FOUNDRY_KINDS else meta.get("kind"),
        "model": meta.get("model"), "effort": meta.get("effort"),
        "gpu": next((r["gpu"] for r in reversed(found) if r.get("gpu")), None),
        "base": (start or {}).get("commit") or meta.get("tileops_base"), "pr": view.get("pr"),
        "refs": refs, "entries": entries,
    }
    if published:
        # Commands, reasons and titles are the agent's own text, which can hold local paths.
        trace["entries"] = [{k: v for k, v in entry.items() if k not in ("command", "reason", "title")}
                            for entry in entries if entry["t"] in PUBLISHED]
        trace["activity"] = {"think_n": sum(e["t"] == "think" for e in session),
                             "tool_n": sum(e["t"] == "tool" for e in session), "tokens": tokens}
    return trace


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
