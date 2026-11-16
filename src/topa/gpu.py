"""Choosing the one GPU a round runs on.

Never usable: the cards in `gpu_exclude`, a card in the Prohibited compute mode, and an
exclusive card while a process computes on it. Avoided when there is a choice: a card
another of our rounds holds -- between kernels it shows no memory and no process -- and
a card another user computes on. A round never waits for a card: its early phases need
no GPU, so when every usable card is shared it takes one and says so, and the timings
show whether that mattered.
"""

from __future__ import annotations

import getpass
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


def ours(config: Config, except_container: str | None = None) -> set[int]:
    """Cards our other running round containers hold."""
    out = sh.docker(config, "ps", "--filter", "label=topa.gpu",
                    "--format", '{{.Names}} {{.Label "topa.gpu"}}', check=False)
    held = set()
    for line in out.splitlines():
        name, _, index = line.partition(" ")
        if name != except_container and index.strip().isdigit():
            held.add(int(index))
    return held


def choose(config: Config, container: str | None) -> Choice:
    running = apps()
    bad, theirs, mine = unusable(config, running), foreign(running), ours(config, container)
    if config.gpu is not None:
        if config.gpu in bad:
            raise SystemExit(f"gpu={config.gpu} is excluded, prohibited, or exclusive and in use")
        if config.gpu in mine:
            return Choice(config.gpu, f"gpu {config.gpu} is also held by another running round")
        if config.gpu in theirs:
            return Choice(config.gpu, f"gpu {config.gpu} is also in use by another user")
        return Choice(config.gpu)
    by_memory = sorted(_query("index", "memory.used"), key=lambda row: (int(row[1]), int(row[0])))
    order = [int(index) for index, _ in by_memory if int(index) not in bad]
    for index in order:
        if index not in mine and index not in theirs:
            return Choice(index)
    for index in order:
        if index not in mine:
            return Choice(index, f"every free card is in use by another user; sharing gpu {index}")
    if order:
        return Choice(order[0], f"every usable card already holds a round; sharing gpu {order[0]}")
    raise SystemExit(f"no usable GPU; excluded, prohibited, or exclusive and in use: {sorted(bad)}")
