"""The per-round container: the CUDA stack, one GPU, and the agent itself.

Claude Code runs inside the container, so every command it runs has the CUDA stack, the
GPU and the installed tilefoundry and tileops. The round directory, the TileOPs worktree
and the TileFoundry source are mounted at the paths they have on the host, and so is the
user's Claude Code state: the agent reuses this machine's login and settings, and its
transcripts land where they land for any other session.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from topa import gpu, ledger, sh
from topa.config import CONFIG, Config

if TYPE_CHECKING:
    from topa.foundry import Prepared

PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
# What in ~/.claude carries code the host runs (hooks, plugins, a status line, skills):
# the agent reads it from the container but cannot change it.
CLAUDE_READ_ONLY = ("settings.json", "hooks", "plugins", "skills", "agents", "commands", "CLAUDE.md")
# herdr's pane identity, which its Claude Code hook reports back through the socket.
HERDR_ENV = ("HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")


@dataclass(frozen=True)
class Started:
    name: str
    gpu: int
    gpu_warning: str | None
    image: str


def _sha(*parts: object) -> str:
    return hashlib.sha256("\n".join(str(part) for part in parts).encode()).hexdigest()


def name(slug: str) -> str:
    return f"topa-{slug}"


def _auth(worktree_git: Path) -> tuple[list[str], list[str], tuple[object, ...]]:
    """Read-only host credentials for git, gh and SSH, so the round can push and open PRs."""
    git_config = Path.home() / ".gitconfig"
    gh_config = Path.home() / ".config" / "gh"
    gh_bin = shutil.which("gh")
    ssh_dir = Path.home() / ".ssh"
    mounts = ["--volume", f"{worktree_git}:{worktree_git}"]
    env = ["--env", "GIT_CONFIG_GLOBAL=/root/.gitconfig"]
    if git_config.is_file():
        mounts += ["--volume", f"{git_config}:/opt/tileops-host-auth/gitconfig:ro"]
    if gh_config.is_dir() and gh_bin:
        mounts += ["--volume", f"{gh_config}:/opt/tileops-host-auth/gh:ro",
                   "--volume", f"{gh_bin}:/usr/local/bin/gh:ro"]
        env += ["--env", "GH_CONFIG_DIR=/opt/tileops-host-auth/gh"]
    if ssh_dir.is_dir():
        mounts += ["--volume", f"{ssh_dir}:{ssh_dir}:ro"]
        env += ["--env", f"TILEOPS_HOST_SSH_DIR={ssh_dir}"]
    return mounts, env, (worktree_git, git_config, gh_config, gh_bin, ssh_dir)


def _agent(home: Path) -> tuple[list[str], list[str], tuple[object, ...]]:
    """The host's Claude Code with its login and settings, herdr's socket, and topa.

    `~/.claude` is shared, not copied: a token one session refreshes is the token every
    session reads, as for two sessions on the host. topa runs from its own source with the
    host's config and ledger, so `topa pr` works from inside the round.
    """
    claude_dir = home / ".claude"
    claude_bin = shutil.which("claude")
    if not claude_dir.is_dir() or not claude_bin:
        raise SystemExit("Claude Code is not set up on this host: run `claude` once and log in")
    mounts = ["--volume", f"{claude_dir}:{claude_dir}"]
    for name in CLAUDE_READ_ONLY:
        if (claude_dir / name).exists():
            mounts += ["--volume", f"{claude_dir / name}:{claude_dir / name}:ro"]
    mounts += ["--volume", f"{Path(claude_bin).resolve()}:/usr/local/bin/claude:ro"]
    socket = Path(os.environ.get("HERDR_SOCKET_PATH") or home / ".config" / "herdr" / "herdr.sock")
    if socket.exists():
        mounts += ["--volume", f"{socket.resolve()}:{socket}"]
    topa_source = Path(str(files("topa"))).parent
    mounts += ["--volume", f"{topa_source}:{topa_source}:ro",
               "--volume", f"{CONFIG}:{CONFIG}:ro",
               "--volume", f"{ledger.LEDGER}:{ledger.LEDGER}"]
    env = ["--env", f"HOME={home}", "--env", f"TOPA_SOURCE={topa_source}",
           # Claude Code refuses to skip permissions as root unless it is told it is sandboxed.
           "--env", "IS_SANDBOX=1",
           # The binary is the host's, mounted read-only; the host updates it.
           "--env", "DISABLE_AUTOUPDATER=1"]
    return mounts, env, (claude_dir, claude_bin, socket, topa_source)


def agent_wrapper(config: Config, container: str) -> str:
    """A `claude` for the round's pane, which runs Claude Code inside *container*.

    herdr tells which agent a pane runs from its foreground process, so this script stays
    that process, named claude, and runs `docker exec` as a child rather than exec-ing it.
    """
    lines = ["#!/bin/bash", "# Claude Code inside the round container; written by topa round start."]
    if config.docker_host:
        lines.append(f"export DOCKER_HOST={shlex.quote(config.docker_host)}")
    passed = " ".join(f"--env {name}" for name in HERDR_ENV)
    lines.append(f'docker exec --interactive --tty {passed} --workdir "$PWD" {container} claude "$@"')
    return "\n".join(lines) + "\n"


def _spellings(*paths: Path) -> list[Path]:
    """Each path, then its resolved form where a symlink makes the two differ.

    herdr and git report the resolved form of a path under a symlinked home, so a path
    the agent is handed may take either form.
    """
    out = []
    for path in paths:
        out.append(path)
        if path.resolve() != path:
            out.append(path.resolve())
    return out


def proxy_env(config: Config) -> list[str]:
    """The host proxy listens on loopback; rootless Docker reaches it as `proxy_host`."""
    env = []
    for var in PROXY_NAMES:
        value = os.environ.get(var)
        if value:
            value = value.replace("127.0.0.1", config.proxy_host).replace("localhost", config.proxy_host)
            env += ["--env", f"{var}={value}"]
    # These reach this machine faster without the proxy: 54 MB/s direct against
    # 1.4 MB/s proxied for files.pythonhosted.org.
    for var in ("NO_PROXY", "no_proxy"):
        env += ["--env", f"{var}={config.no_proxy}"]
    return env


def _label(config: Config, container: str, key: str) -> str:
    return sh.docker(config, "inspect", "--format", f'{{{{index .Config.Labels "topa.{key}"}}}}', container)


def start(
    config: Config,
    *,
    slug: str,
    worktree: Path,
    round_dir: Path,
    foundry: Prepared,
    foundry_source: Path,
    foundry_git: Path,
    foundry_base: str,
) -> Started:
    container = name(slug)
    image = sh.docker(config, "image", "inspect", config.agent_image, "--format", "{{.Id}}")
    worktree_git = Path(sh.run(["git", "-C", worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"]))

    tilelang_cache = config.cache / "tilelang"
    triton_cache = config.cache / "triton"
    for cache in (tilelang_cache, triton_cache):
        cache.mkdir(parents=True, exist_ok=True)

    home = Path.home()
    auth_mounts, auth_env, auth_parts = _auth(worktree_git)
    agent_mounts, agent_env, agent_parts = _agent(home)
    proxy_env_args = proxy_env(config)
    auth_key = _sha(foundry_git, *auth_parts, *agent_parts, *proxy_env_args)

    choice: gpu.Choice | None = None
    exists = sh.docker_ok(config, "container", "inspect", container)
    if exists:
        expected = {"worktree": str(worktree), "round": str(round_dir), "foundry": foundry_base,
                    "image": image, "auth": auth_key}
        stale = any(_label(config, container, key) != value for key, value in expected.items())
        held = int(_label(config, container, "gpu"))
        if stale or (config.gpu is not None and held != config.gpu):
            sh.docker(config, "rm", "-f", container)
            exists = False
        else:
            choice = gpu.Choice(held)

    if not exists:
        choice = chosen = gpu.choose(config, container)
        sh.docker(
            config, "run", "--detach", "--init", "--name", container,
            "--label", f"topa.round={slug}",
            "--label", f"topa.worktree={worktree}",
            "--label", f"topa.round_dir={round_dir}",
            "--label", f"topa.foundry={foundry_base}",
            "--label", f"topa.image={image}",
            "--label", f"topa.auth={auth_key}",
            "--label", f"topa.gpu={chosen.index}",
            "--add-host", "host.docker.internal:host-gateway",
            "--runtime=nvidia", "--ipc=host", "--shm-size=16g",
            *[arg for host in (worktree, round_dir, foundry_source, foundry_git)
              for path in _spellings(host) for arg in ("--volume", f"{host}:{path}")],
            "--volume", f"{foundry.stage / 'wheels'}:/opt/topa/wheels:ro",
            "--volume", f"{foundry.stage / 'build-tools'}:/opt/topa/build-tools:ro",
            "--volume", f"{tilelang_cache}:/ci-cache/tilelang",
            "--volume", f"{triton_cache}:/ci-cache/triton",
            *auth_mounts,
            *agent_mounts,
            "--workdir", str(round_dir),
            "--env", f"NVIDIA_VISIBLE_DEVICES={chosen.index}",
            "--env", "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            "--env", "CUDA_VISIBLE_DEVICES=0",
            "--env", "GIT_OPTIONAL_LOCKS=0",
            "--env", "PYTHONUNBUFFERED=1",
            "--env", f"TILEOPS_PHYSICAL_GPU={chosen.index}",
            "--env", "TILEOPS_SAFE_DIRS=" + ":".join(
                str(path) for path in _spellings(worktree, round_dir, foundry_source)),
            "--env", "TILELANG_CACHE_DIR=/ci-cache/tilelang",
            "--env", "TILELANG_TMP_DIR=/ci-cache/tilelang/tmp",
            "--env", "TRITON_CACHE_DIR=/ci-cache/triton",
            *auth_env,
            *agent_env,
            *proxy_env_args,
            config.agent_image,
        )
    assert choice is not None
    if sh.docker(config, "inspect", "--format", "{{.State.Running}}", container) != "true":
        sh.docker(config, "start", container)

    marker = f"/var/lib/topa-foundry-{foundry_base}-{foundry.stage.name}"
    if not sh.docker_ok(config, "exec", container, "test", "-f", marker):
        _install(config, container, worktree, foundry_source)
        sh.docker(config, "exec", container, "touch", marker)

    # Claude Code's own state file is per container: it holds the folders a session
    # trusted and other UI state, and the host's copy already trusts the rounds root.
    claude_state = home / ".claude.json"
    if claude_state.is_file():
        sh.docker(config, "cp", claude_state, f"{container}:{claude_state}")

    probe = ('test "$CUDA_HOME" = /usr/local/cuda && test "$(command -v nvcc)" = /usr/local/cuda/bin/nvcc '
             f'&& test "$TILELANG_CACHE_DIR" = /ci-cache/tilelang && git -C {worktree} status --short >/dev/null '
             '&& claude --version >/dev/null && topa --help >/dev/null')
    for _ in range(100):
        if sh.docker_ok(config, "exec", container, "bash", "-c", probe):
            break
        time.sleep(0.1)
    else:
        raise SystemExit(f"the environment of {container} did not become ready")

    if shutil.which("gh") and (home / ".config" / "gh").is_dir():
        check = ('gh auth status --hostname github.com >/dev/null 2>&1 && printf "protocol=https\\n'
                 'host=github.com\\n\\n" | git credential fill | grep -q "^password="')
        if not sh.docker_ok(config, "exec", container, "bash", "-c", check):
            raise SystemExit("the mounted GitHub credentials are not usable inside the container")

    return Started(container, choice.index, choice.warning, image)


def _install(config: Config, container: str, worktree: Path, foundry_source: Path) -> None:
    """Install TileFoundry and TileOPs editable, offline, without changing the runner's stack.

    Each project builds with its own staged backend (`--no-build-isolation` and
    PYTHONPATH). Neither installs dependencies: the staged wheels are what the runner
    lacks for TileFoundry, and the runner is what TileOPs runs on.
    """
    pip = "python -m pip --quiet --disable-pip-version-check install --root-user-action=ignore --no-deps"
    sh.docker(config, "exec", container, "bash", "-c",
              f"set -e; shopt -s nullglob; wheels=(/opt/topa/wheels/*.whl); "
              f'if (( ${{#wheels[@]}} )); then {pip} --no-index "${{wheels[@]}}"; fi')
    for name, path in (("tilefoundry", foundry_source), ("tileops", worktree)):
        sh.docker(config, "exec", "--env", f"PYTHONPATH=/opt/topa/build-tools/{name}",
                  "--workdir", str(path), container, "bash", "-c",
                  f"{pip} --no-index --no-build-isolation --editable .")
    sh.docker(config, "exec", container, "python", "-c",
              "import pathlib, tilefoundry, tileops\n"
              f"for module, root in ((tilefoundry, '{foundry_source}/'), (tileops, '{worktree}/')):\n"
              "    path = str(pathlib.Path(module.__file__).resolve())\n"
              "    assert path.startswith(root), path")


def remove(config: Config, slug: str) -> list[str]:
    """Remove every container of this round, waiting out one already being removed."""
    ids = sh.docker(config, "ps", "-aq", "--filter", f"label=topa.round={slug}", check=False).split()
    for container in ids:
        deadline = time.monotonic() + 60
        while sh.docker_ok(config, "container", "inspect", container):
            try:
                sh.docker(config, "rm", "-f", container)
            except sh.CommandError as error:
                if not any(word in error.output.lower() for word in ("in progress", "no such container")):
                    raise
                if time.monotonic() > deadline:
                    raise SystemExit(f"timed out removing container {container}") from error
                time.sleep(0.5)
    return ids
