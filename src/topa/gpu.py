from __future__ import annotations

import getpass
from collections import Counter
from dataclasses import dataclass

from topa import sh
from topa.config import Config


@dataclass(frozen=True)
class Choice:
    index: int
    warning: str | None = None


def _query(*fields: str) -> list[list[str]]:
    out = sh.run(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
    return [[cell.strip() for cell in line.split(",")] for line in out.splitlines() if line.strip()]


def apps() -> list[tuple[int, str]]:
    """Each compute process as (card, pid)."""
    by_uuid = {uuid: int(index) for index, uuid in _query("index", "uuid")}
    out = sh.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader"])
    rows = [[part.strip() for part in line.split(",", 1)] for line in out.splitlines() if "," in line]
    return [(by_uuid[uuid], pid) for uuid, pid in rows if uuid in by_uuid]


def unusable(config: Config, running: list[tuple[int, str]]) -> set[int]:
    busy = {index for index, _ in running}
    blocked = {int(index) for index, mode in _query("index", "compute_mode")
               if mode == "Prohibited" or (mode != "Default" and int(index) in busy)}
    return set(config.gpu_exclude) | blocked


def foreign(running: list[tuple[int, str]]) -> set[int]:
    """Cards with a process owned by another user."""
    me = getpass.getuser()
    return {index for index, pid in running
            if (owner := sh.run(["ps", "-o", "user=", "-p", pid], check=False).strip()) and owner != me}


def ours(config: Config, except_container: str | None = None) -> Counter[int]:
    """How many of our other running round containers hold each card."""
    out = sh.docker(config, "ps", "--filter", "label=topa.gpu",
                    "--format", '{{.Names}} {{.Label "topa.gpu"}}', check=False)
    held: Counter[int] = Counter()
    for line in out.splitlines():
        name, _, index = line.partition(" ")
        if name != except_container and index.strip().isdigit():
            held[int(index)] += 1
    return held


def choose(config: Config, container: str | None) -> Choice:
    running = apps()
    bad, theirs = unusable(config, running), foreign(running)
    if config.gpu is not None:
        if config.gpu in bad:
            raise SystemExit(f"gpu={config.gpu} is excluded, prohibited, or exclusive and in use")
        if config.gpu in theirs:
            return Choice(config.gpu, f"gpu {config.gpu} is also in use by another user")
        return Choice(config.gpu)
    mine = ours(config, container)
    memory = {int(index): int(used) for index, used in _query("index", "memory.used")}
    order = sorted((index for index in memory if index not in bad),
                   key=lambda index: (index in theirs, mine[index], memory[index], index))
    if not order:
        raise SystemExit(f"no usable GPU; excluded, prohibited, or exclusive and in use: {sorted(bad)}")
    if order[0] in theirs:
        return Choice(order[0], f"every usable card is in use by another user; sharing gpu {order[0]}")
    return Choice(order[0])
