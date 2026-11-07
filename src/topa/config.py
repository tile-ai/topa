"""Where topa keeps its state, and how it reaches the repositories and machines it drives.

Settings come from `~/.topa/config.toml`; a `TOPA_<NAME>` environment variable
overrides any one of them for a single command.
"""

from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("TOPA_HOME", Path.home() / ".topa")).expanduser()
CONFIG = HOME / "config.toml"

DEFAULTS: dict[str, Any] = {
    "tileops_repo": "~/TileOPs",
    "tilefoundry_repo": "~/TileFoundry",
    "worktree_root": "~/TileOPs-worktrees",
    "runner_image": "ghcr.io/tile-ai/tileops-dev:cuda13.2-torch2.13-tilelang0.1.12",
    "agent_image": "topa:agent",
    "gpu": None,
    "gpu_exclude": [],
    "docker_host": None,
    "docker_bootstrap": None,
    "proxy_host": "10.0.2.2",
    "no_proxy": "localhost,127.0.0.1",
    "upstream": "tile-ai/TileOPs",
    "fork_owner": None,
    # Unset, the round agent runs its own defaults.
    "model": None,
    "effort": None,
    # Runs on the host from the TileOPs worktree; see docs/usage.md.
    "reviewer": None,
    "tilefoundry_commit": None,
    "max_rounds": 2,
}
PATHS = {"tileops_repo", "tilefoundry_repo", "worktree_root"}


@dataclass(frozen=True)
class Config:
    tileops_repo: Path
    tilefoundry_repo: Path
    worktree_root: Path
    runner_image: str
    agent_image: str
    gpu: int | None
    gpu_exclude: tuple[int, ...]
    docker_host: str | None
    docker_bootstrap: str | None
    proxy_host: str
    no_proxy: str
    upstream: str
    fork_owner: str | None
    model: str | None
    effort: str | None
    reviewer: str | None
    tilefoundry_commit: str | None
    max_rounds: int

    @property
    def reviewer_program(self) -> str:
        """The executable the `reviewer` command runs, as written there."""
        if not self.reviewer:
            raise SystemExit(f"set reviewer in {CONFIG}; see docs/usage.md")
        return shlex.split(self.reviewer)[0]

    @property
    def rounds(self) -> Path:
        return HOME / "rounds"

    @property
    def reviews(self) -> Path:
        """One directory per reviewed PR: its review inputs, findings and worktree."""
        return HOME / "reviews"

    @property
    def ssh_config_dir(self) -> Path:
        return HOME / "ssh-config"

    @property
    def ssh_key_root(self) -> Path:
        return HOME / "ssh"

    @property
    def cache(self) -> Path:
        """Long-lived and rebuilt on demand: safe to delete when no round runs."""
        return HOME / "cache"

    @property
    def checkouts(self) -> Path:
        """Per-round TileFoundry checkouts, removed by `topa round stop --rm`."""
        return HOME / "checkouts" / "tilefoundry"


def _coerce(name: str, value: Any) -> Any:
    if value is None or value == "":
        return None if DEFAULTS[name] is None else DEFAULTS[name]
    if name in PATHS:
        return Path(str(value)).expanduser()
    if name in {"gpu", "max_rounds"}:
        return int(value)
    if name == "gpu_exclude":
        if isinstance(value, str):
            value = [part for part in value.split(",") if part.strip()]
        return tuple(int(part) for part in value)
    return str(value)


def load() -> Config:
    values = dict(DEFAULTS)
    if CONFIG.is_file():
        with CONFIG.open("rb") as handle:
            values.update(tomllib.load(handle))
    for name in DEFAULTS:
        override = os.environ.get(f"TOPA_{name.upper()}")
        if override is not None:
            values[name] = override
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"unknown settings in {CONFIG}: {', '.join(sorted(unknown))}")
    return Config(**{name: _coerce(name, values[name]) for name in DEFAULTS})


def default_toml() -> str:
    """A config file naming every setting, the unset ones commented out."""
    lines = ["# topa settings. TOPA_<NAME> in the environment overrides one per command.", ""]
    for name, value in DEFAULTS.items():
        if value is None:
            lines.append(f"# {name} = ")
        elif isinstance(value, list):
            lines.append(f"{name} = [{', '.join(str(v) for v in value)}]")
        elif isinstance(value, int):
            lines.append(f"{name} = {value}")
        else:
            lines.append(f'{name} = "{value}"')
    return "\n".join(lines) + "\n"
