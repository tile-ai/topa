"""`topa round trace` and `topa trace publish`: a round's records as one time-ordered trace, read-only.

The full trace, a `schema.Trace`, holds every entry: the round's phases from the ledger, the
TileFoundry analyses `provenance.json` records, the measurements of `topa round measure`, and the
agent's thinking and tool calls from the archived session. It holds one op.

The published trace, a `schema.PublishedTrace`, is made from a full trace: it keeps the phases,
analyses and measurements, and counts what it leaves out.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from topa import ledger, measure, schema
from topa.gate import FOUNDRY_KINDS

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
        case_id = str(record.get("workload") or "").partition(" ")[2]
        found.append({"ts": _local(report.stat().st_mtime), "t": "foundry", "kind": kind,
                      "case_id": case_id or None, "command": record.get("command"),
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


def export(round_dir: Path, view: dict[str, Any], transcripts: Path) -> dict[str, Any]:
    """The round's full trace; *view* is its ledger view, *transcripts* the folder of its session transcripts."""
    meta = {**view, **_json(round_dir / "round.json")}
    provenance = _json(round_dir / "provenance.json")
    slug = meta["round"]
    # An issue's perf round learns its op from the issue, so `bench` names it.
    ops = sorted({*meta.get("ops", ()), *filter(None, [meta.get("op")])} or set((provenance.get("bench") or {}).keys()))
    if len(ops) != 1:
        raise SystemExit(f"a trace holds one op; {slug} works {', '.join(ops) or 'none'}")
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
    entries += [{"ts": r["ts"], "t": "measure", "title": r["title"], "commit": r["commit"], "base": r["base"],
                 "busy_card": r["busy_card"], "hbm_bandwidth": r["hbm_bandwidth"],
                 "cases": [{k: v for k, v in case.items() if k != "op"} for case in r["cases"]]} for r in found]
    session, tokens = _session(transcripts)
    entries += session
    entries.sort(key=lambda entry: _instant(entry["ts"]))
    start = next((r for r in found if r["base"]), None)
    issue, wiki = meta.get("issue"), meta.get("wiki")
    return schema.checked(schema.Trace, {
        "schema": schema.TRACE, "round": slug, "item": issue or ops[0], "op": ops[0],
        "source": {"kind": "issue", "ref": meta.get("issue_url")} if issue else {"kind": "wiki", "ref": wiki} if wiki
        else {"kind": "none", "ref": None},
        "method": "foundry" if meta.get("kind") in FOUNDRY_KINDS else meta.get("kind"),
        "model": meta.get("model"), "effort": meta.get("effort"),
        "gpu": next((r["gpu"] for r in reversed(found) if r.get("gpu")), None),
        "base": (start or {}).get("commit") or meta.get("tileops_base"), "pr": view.get("pr"),
        "tokens": tokens, "entries": entries,
    })


def publish(full: dict[str, Any]) -> dict[str, Any]:
    """The published trace of the full trace *full*.

    It keeps the phases, analyses and measurements, and counts the session it leaves out. A phase's
    reason and an analysis's command are the agent's own text, which can hold local paths, so they
    go; a measurement's title stays, since it says what the measured commit changed.
    """
    trace = schema.checked(schema.Trace, full)
    entries = trace.pop("entries")
    tokens = trace.pop("tokens")
    return schema.checked(schema.PublishedTrace, {
        **trace,
        "activity": {"think_n": sum(e["t"] == "think" for e in entries),
                     "tool_n": sum(e["t"] == "tool" for e in entries), "tokens": tokens},
        "entries": [{k: v for k, v in e.items() if k not in ("reason", "command")}
                    for e in entries if e["t"] in PUBLISHED],
    })


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
