"""`topa round measure`: the round's recorded timing, and the DRAM floor it defines.

A measurement runs the pytest nodes `bench` in `provenance.json` names for each op of the
round, on a snapshot of one commit, and appends what the TileOPs benchmark XML says to
`measures.jsonl` in the round directory. A row is named `<Op> <case_id>`: the op the node
serves and the parametrized part of its test name, which TileOPs defines as the case id.

Each record holds the HBM bandwidth of the TileFoundry device the `target` setting named when it
was taken. A row's DRAM floor is its first measured `tileops_bytes` over the bandwidth of that record.
"""

from __future__ import annotations

import datetime
import decimal
import json
import math
import shlex
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from topa import sh

RECORDS = "measures.jsonl"
SNAPSHOTS = "work/measure"
EVIDENCE = "evidence/measure"
# The XML tags that are not an implementation: the op itself, and the copy of its first comparator.
NOT_COMPARATORS = ("tileops", "baseline")


def name(op: str, case_id: str) -> str:
    return f"{op} {case_id}"


def _us(milliseconds: str) -> float:
    """The XML's milliseconds in microseconds, keeping its decimal digits exactly."""
    return float(decimal.Decimal(milliseconds).scaleb(3))


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None




def read_xml(path: Path, nodes: dict[str, str]) -> list[dict[str, Any]]:
    """The rows of the benchmark XML *path* that the nodes of *nodes*, node to op, ran; a node names a
    test function or one parametrized test.

    A row's status is `measured`, `failed`, `unavailable` (the op refuses the case) or `skipped`.
    The op the XML names must be the op the node serves.
    """
    rows = []
    for case in ET.parse(path).getroot().iter("testcase"):
        test = case.get("name", "")
        node = f"{case.get('classname', '').replace('.', '/')}.py::{test}"
        op = nodes.get(node) or nodes.get(node.split("[", 1)[0])
        if op is None:
            continue
        props = {p.get("name"): p.get("value") for p in case.iter("property")}
        named = props.get("op") or (props.get("ops") or "").split(",")[0]
        if named and named != op:
            raise SystemExit(f"{node} measures {named}, but `bench` lists it under {op}")
        busy = _number(props.get("tileops_device_busy_ms"))
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "unavailable" if "unavailable" in props else "skipped"
        else:
            status = "measured" if busy else "skipped"
        comparators = {}
        for key, value in props.items():
            tag = key.removesuffix("_device_busy_ms")
            if key.endswith("_device_busy_ms") and tag not in NOT_COMPARATORS and _number(value):
                comparators[tag] = {"us": _us(value), "timing": props.get(f"{tag}_timing"),
                                    "l2_flush": props.get(f"{tag}_l2_flush"),
                                    "no_ratio": props.get(f"{tag}_no_ratio") == "True",
                                    "unverified": props.get(f"{tag}_unverified") or None}
        bytes_ = _number(props.get("tileops_bytes"))
        rows.append({"op": op, "case_id": test.partition("[")[2].removesuffix("]") or test, "status": status,
                     "us": _us(props["tileops_device_busy_ms"]) if status == "measured" else None,
                     "timing": props.get("tileops_timing"), "l2_flush": props.get("tileops_l2_flush"),
                     "bytes": int(bytes_) if bytes_ else None, "comparators": comparators})
    return rows


def floor_text(floor: float) -> str:
    """A DRAM floor in microseconds, to three decimals or three significant digits, whichever is finer."""
    return f"{floor:.{max(3, 2 - math.floor(math.log10(floor)))}f}"


def records(round_dir: Path) -> list[dict[str, Any]]:
    path = round_dir / RECORDS
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def floors(found: list[dict[str, Any]]) -> dict[str, float]:
    """Each row's DRAM floor in microseconds, from its first measured byte count."""
    result: dict[str, float] = {}
    for record in found:
        for row in record["rows"]:
            key = name(row["op"], row["case_id"])
            if key not in result and row["status"] == "measured" and row.get("bytes"):
                result[key] = row["bytes"] / record["hbm_bandwidth"] * 1e6
    return result


def names(found: list[dict[str, Any]]) -> tuple[str, ...]:
    """Every row of the latest record, in its order."""
    return tuple(name(row["op"], row["case_id"]) for row in found[-1]["rows"]) if found else ()


def hbm_bandwidth(foundry_source: Path, target: str | None) -> float:
    """The HBM bandwidth, in bytes per second, of the TileFoundry device *target* in *foundry_source*."""
    if not target:
        raise SystemExit("set `target` in the topa config: the TileFoundry device whose HBM bandwidth sets the DRAM floor")
    for path in sorted(foundry_source.glob("targets/**/hardware/*.toml")):
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        if document.get("spec", {}).get("id") == target:
            try:
                return float(document["facts"]["memory"]["hbm"]["bandwidth"]["value"])
            except (KeyError, TypeError, ValueError):
                raise SystemExit(f"{path} states no facts.memory.hbm.bandwidth") from None
    raise SystemExit(f"no TileFoundry hardware document under {foundry_source}/targets has id {target}")


def bench_nodes(provenance: dict[str, Any], ops: set[str]) -> dict[str, str]:
    """`bench` of *provenance* as node to op; it must name the round's ops, when it records them, and only them."""
    bench = provenance.get("bench")
    if not isinstance(bench, dict) or not bench or not all(
            isinstance(v, list) and v and all(isinstance(n, str) and "::" in n for n in v) for v in bench.values()):
        raise SystemExit('provenance.json needs `bench`: {"<Op>": ["<path>.py::<test>", ...]} for each op of the round')
    # An issue's perf round learns its op from the issue, so `bench` names it.
    if ops and set(bench) != ops:
        raise SystemExit(f"`bench` names {', '.join(sorted(bench))}; the round works {', '.join(sorted(ops))}")
    nodes = {node: op for op, listed in bench.items() for node in listed}
    if len(nodes) != sum(map(len, bench.values())):
        raise SystemExit("`bench` lists a node twice; a node serves one op")
    return nodes


def run(meta: dict[str, Any], round_dir: Path, ops: set[str], title: str, target: str | None,
        workloads: tuple[str, ...], *, base: bool = False) -> dict[str, Any]:
    """Measure the base or HEAD of the round's worktree, and append the record; every one of
    *workloads* must be a row of it."""
    bandwidth = hbm_bandwidth(Path(meta["foundry_source"]), target)
    provenance = json.loads((round_dir / "provenance.json").read_text(encoding="utf-8")) \
        if (round_dir / "provenance.json").is_file() else {}
    nodes = bench_nodes(provenance, ops)
    worktree = Path(meta["worktree"])
    commit = sh.run(["git", "-C", worktree, "rev-parse",
                     f"{provenance.get('tileops_base') or meta['tileops_base']}^{{commit}}" if base else "HEAD"])
    snapshot = round_dir / SNAPSHOTS / commit[:12]
    if not snapshot.exists():
        sh.run(["git", "-C", worktree, "worktree", "add", "--quiet", "--detach", snapshot, commit])
    found = records(round_dir)
    xml = round_dir / EVIDENCE / f"{len(found) + 1}-{commit[:12]}.xml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    busy = xml.with_suffix(".busy")
    # A failed attempt leaves its files under the name the retry takes.
    xml.unlink(missing_ok=True)
    busy.unlink(missing_ok=True)
    src = snapshot / "src"
    command = (f"cd {shlex.quote(str(snapshot))} && TOPA_BENCH_BUSY={shlex.quote(str(busy))} "
               f"PYTHONPATH={shlex.quote(f'{src}:{snapshot}')} topa-bench python -m pytest -p no:cacheprovider -q "
               f"{' '.join(shlex.quote(n) for n in nodes)} --junitxml {shlex.quote(str(xml))}; "
               "nvidia-smi --query-gpu=name --format=csv,noheader | head -1")
    # The last line is the card's name, the container's only one; the lines before it are pytest's.
    output = sh.run(["ssh", "-T", meta["alias"], command], check=False).splitlines()
    if not xml.is_file():
        raise SystemExit(f"the benchmark wrote no {xml}:\n" + "\n".join(output[-20:]))
    rows = read_xml(xml, nodes)
    if not rows:
        raise SystemExit(f"{xml} holds no testcase of the `bench` nodes")
    absent = sorted(set(workloads) - {name(row["op"], row["case_id"]) for row in rows})
    if absent:
        raise SystemExit(f"{xml} has no row {', '.join(absent)}; check `bench` and the rows' names")
    record = {"ts": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
              "title": " ".join(title.split()), "commit": commit, "base": base,
              "xml": str(xml.relative_to(round_dir)), "gpu": output[-1] if output else None,
              "busy_card": busy.exists(), "target": target, "hbm_bandwidth": bandwidth, "rows": rows}
    with (round_dir / RECORDS).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def render(record: dict[str, Any], floor: dict[str, float]) -> str:
    """The record as a table, with each row's DRAM floor; the Performance table copies the floors."""
    lines = [f"{record['title']} at {record['commit'][:12]}"
             + (", timed on a busy card" if record["busy_card"] else ""), "",
             "| Workload | status | TileOps (us) | DRAM floor (us) |", "| --- | --- | ---: | ---: |"]
    for row in record["rows"]:
        key = name(row["op"], row["case_id"])
        us = f"{row['us']:.3f}" if row["us"] else ""
        least = floor_text(floor[key]) if key in floor else ""
        lines.append(f"| `{key}` | {row['status']} | {us} | {least} |")
    return "\n".join(lines)
