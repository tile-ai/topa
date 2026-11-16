"""The append-only record of every round: `~/.topa/ledger.jsonl`.

One JSON object per line, each an event with a timestamp. A round's state is what
its events say in order; nothing is rewritten. Writers take an exclusive lock, so
several topa commands can append at once.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterator

from topa.config import HOME

LEDGER = HOME / "ledger.jsonl"

# The event that leaves a round in each state; any other event keeps the state.
# `create` is written before the first resource exists, so `topa round stop --rm`
# can find what a start that failed half way left behind. A perf or new-op round
# also has a `phase` on its state graph: REQ from `create`, then each `transition`'s `to`.
STATES = {"create": "starting", "start": "running", "fail": "failed",
          "stop": "stopped", "remove": "removed"}
LIVE = {"starting", "running"}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def append(event: str, path: Path | None = None, **fields: Any) -> dict[str, Any]:
    path = path or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _now(), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return record


@contextlib.contextmanager
def claim() -> Iterator[None]:
    """Hold while checking the ledger and appending what the check allows, such as a new round."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with (LEDGER.parent / "claim.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def events(path: Path | None = None) -> Iterator[dict[str, Any]]:
    path = path or LEDGER
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            lines = handle.readlines()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    for number, line in enumerate(lines, 1):
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number}: not JSON: {error}") from error


def rounds(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every round's current view, folded from its events in order."""
    folded: dict[str, dict[str, Any]] = {}
    for record in events(path):
        name = record.get("round")
        if not name:
            continue
        view = folded.setdefault(name, {"round": name, "state": "planned", "history": []})
        view["history"].append(record["event"])
        view["updated"] = record["ts"]
        view["state"] = STATES.get(record["event"], view["state"])
        if record["event"] == "transition":
            view["phase"] = record["to"]
            continue
        for key, value in record.items():
            if key not in {"ts", "event", "text"}:
                view[key] = value
    return folded

