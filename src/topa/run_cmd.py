"""`topa run`: one command against a TileOPs worktree, in a throwaway container.

This is for work outside a round: reviewing a branch, reproducing a reported
failure, checking a fix. The worktree is mounted at its own host path, so a
traceback names a file you can open. Nothing is installed into it: the sources
are reached through PYTHONPATH, and the checkout stays as git left it. The runtime
caches outlive the container, so a second run does not recompile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from topa import gpu, sh
from topa.config import Config


def run(config: Config, worktree: Path, command: list[str]) -> int:
    worktree = worktree.resolve()
    if not sh.docker_ok(config, "image", "inspect", config.agent_image):
        raise SystemExit(f"missing {config.agent_image}; run `topa setup` first")
    choice = gpu.choose(config, container=None)
    if choice.warning:
        sh.note(f"warning: {choice.warning}")

    mounts = ["--volume", f"{worktree}:{worktree}"]
    # A linked worktree keeps its objects in the main checkout, so git inside the
    # container needs that directory at the path .git points at.
    common = sh.run(["git", "-C", worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"], check=False)
    if common and not Path(common).is_relative_to(worktree):
        mounts += ["--volume", f"{common}:{common}"]
    caches = config.cache
    for name in ("tilelang", "triton"):
        (caches / name).mkdir(parents=True, exist_ok=True)
        mounts += ["--volume", f"{caches / name}:/ci-cache/{name}"]

    tty = ["--interactive", "--tty"] if sys.stdin.isatty() and sys.stdout.isatty() else []
    argv = [
        "docker", "run", "--rm", "--init", *tty,
        "--runtime=nvidia", "--ipc=host", "--shm-size=16g",
        *mounts, "--workdir", str(worktree),
        "--env", f"NVIDIA_VISIBLE_DEVICES={choice.index}",
        "--env", "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
        "--env", "CUDA_VISIBLE_DEVICES=0",
        "--env", f"PYTHONPATH={worktree}/src:{worktree}",
        "--env", "TILELANG_CACHE_DIR=/ci-cache/tilelang",
        "--env", "TRITON_CACHE_DIR=/ci-cache/triton",
        # The image's entrypoint keeps a round's container up; a one-off command needs none.
        "--entrypoint", "",
        config.agent_image, *(command or ["bash"]),
    ]
    os.execvpe("docker", argv, sh.docker_env(config))
