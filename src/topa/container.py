"""The per-round container: the CUDA stack, one GPU, and an SSH door for commands.

The agent runs on the host. Only execution needs the container, so the round
directory, the TileOPs worktree and the TileFoundry source are mounted at the paths
they have on the host, and a command carries over unchanged.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from topa import gpu, sh
from topa.config import Config

if TYPE_CHECKING:
    from topa.foundry import Prepared

PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@dataclass(frozen=True)
class Started:
    name: str
    gpu: int
    gpu_warning: str | None
    image: str
    port: int
    alias: str
    key: Path


def _sha(*parts: object) -> str:
    return hashlib.sha256("\n".join(str(part) for part in parts).encode()).hexdigest()


def name(slug: str) -> str:
    return f"topa-{slug}"


def alias(slug: str) -> str:
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


def _spellings(*paths: Path) -> list[Path]:
    """Each path, then its resolved form where a symlink makes the two differ.

    Claude Code reports the resolved working directory, so on a host where the home
    directory is a symlink the agent names the resolved form as often as the written one.
    """
    out = []
    for path in paths:
        out.append(path)
        if path.resolve() != path:
            out.append(path.resolve())
    return out


def proxy_env(config: Config) -> list[str]:
    """The host's proxy settings, with a loopback proxy address replaced by `proxy_host`,
    the host as the container reaches it."""
    env = []
    for var in PROXY_NAMES:
        value = os.environ.get(var)
        if value and config.proxy_host:
            value = value.replace("127.0.0.1", config.proxy_host).replace("localhost", config.proxy_host)
        if value:
            env += ["--env", f"{var}={value}"]
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

    key_dir = config.ssh_key_root / slug
    key_dir.mkdir(parents=True, exist_ok=True)
    key_dir.chmod(0o700)
    key = key_dir / "id_ed25519"
    if not key.exists():
        sh.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key])
    public_key = key.with_suffix(".pub").read_text().strip()

    tilelang_cache = config.cache / "tilelang"
    triton_cache = config.cache / "triton"
    for cache in (tilelang_cache, triton_cache):
        cache.mkdir(parents=True, exist_ok=True)

    auth_mounts, auth_env, auth_parts = _auth(worktree_git)
    proxy_env_args = proxy_env(config)
    auth_key = _sha(foundry_git, *auth_parts, *proxy_env_args)

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
            "--publish", "127.0.0.1::22",
            *[arg for host in (worktree, round_dir, foundry_source, foundry_git)
              for path in _spellings(host) for arg in ("--volume", f"{host}:{path}")],
            "--volume", f"{foundry.stage / 'wheels'}:/opt/topa/wheels:ro",
            "--volume", f"{foundry.stage / 'build-tools'}:/opt/topa/build-tools:ro",
            "--volume", f"{tilelang_cache}:/ci-cache/tilelang",
            "--volume", f"{triton_cache}:/ci-cache/triton",
            *auth_mounts,
            "--workdir", str(round_dir),
            "--env", f"TILEOPS_SSH_PUBLIC_KEY={public_key}",
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

    ports = sh.docker(config, "port", container, "22/tcp")
    port = int(ports.splitlines()[0].rsplit(":", 1)[1])
    host_alias = alias(slug)
    config.ssh_config_dir.mkdir(parents=True, exist_ok=True)
    entry = config.ssh_config_dir / host_alias
    entry.write_text(
        f"Host {host_alias}\n"
        "    HostName 127.0.0.1\n"
        "    User root\n"
        f"    Port {port}\n"
        f"    IdentityFile {key}\n"
        "    IdentitiesOnly yes\n"
        "    BatchMode yes\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n"
        "    LogLevel ERROR\n"
    )
    entry.chmod(0o600)

    probe = ('test "$CUDA_HOME" = /usr/local/cuda && test "$(command -v nvcc)" = /usr/local/cuda/bin/nvcc '
             f'&& test "$TILELANG_CACHE_DIR" = /ci-cache/tilelang && git -C {worktree} status --short >/dev/null')
    for _ in range(100):
        if sh.ok(["ssh", host_alias, probe]):
            break
        time.sleep(0.1)
    else:
        raise SystemExit(f"the SSH environment of {container} did not become ready")

    if shutil.which("gh") and (Path.home() / ".config" / "gh").is_dir():
        check = ('gh auth status --hostname github.com >/dev/null 2>&1 && printf "protocol=https\\n'
                 'host=github.com\\n\\n" | git credential fill | grep -q "^password="')
        if not sh.ok(["ssh", host_alias, check]):
            raise SystemExit("the mounted GitHub credentials are not usable inside the container")

    return Started(container, choice.index, choice.warning, image, port, host_alias, key)


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
    # TileFoundry ships its NVIDIA target as a second package in the same checkout. It
    # builds with TileFoundry's backend and depends only on tilefoundry.
    nvidia = foundry_source / "targets" / "nvidia"
    projects = [("tilefoundry", foundry_source), ("tileops", worktree)]
    checks = [("tilefoundry", foundry_source), ("tileops", worktree)]
    if (nvidia / "pyproject.toml").is_file():
        projects.insert(1, ("tilefoundry", nvidia))
        checks.append(("tilefoundry_nvidia", nvidia))
    for backend, path in projects:
        sh.docker(config, "exec", "--env", f"PYTHONPATH=/opt/topa/build-tools/{backend}",
                  "--workdir", str(path), container, "bash", "-c",
                  f"{pip} --no-index --no-build-isolation --editable .")
    imports = ", ".join(module for module, _ in checks)
    pairs = ", ".join(f"({module}, '{root}/')" for module, root in checks)
    sh.docker(config, "exec", container, "python", "-c",
              f"import pathlib, {imports}\n"
              f"for module, root in ({pairs},):\n"
              "    path = str(pathlib.Path(module.__file__).resolve())\n"
              "    assert path.startswith(root), path")


def foreign_files(paths: list[Path]) -> bool:
    """Whether any file under *paths* belongs to another user than the one running topa."""
    me = os.getuid()
    for root in paths:
        if not root.exists():
            continue
        if root.lstat().st_uid != me:
            return True
        for path in root.rglob("*"):
            try:
                if path.lstat().st_uid != me:
                    return True
            except OSError:
                continue
    return False


def give_back(config: Config, container: str, paths: list[Path]) -> bool:
    """Chown what the container wrote under *paths* back to the user running topa.

    The container runs as root. Under rootful Docker its files belong to root on the
    host, and `round stop --rm` cannot remove the worktree. Under rootless Docker the
    container's root is this user, nothing is foreign, and this does nothing.
    """
    paths = [path for path in paths if path.exists()]
    if not paths or not foreign_files(paths):
        return False
    if not sh.docker_ok(config, "container", "inspect", container):
        return False
    # A round stopped earlier has a stopped container; exec needs it running.
    sh.docker(config, "start", container, check=False)
    owner = f"{os.getuid()}:{os.getgid()}"
    sh.docker(config, "exec", container, "chown", "-R", owner, *map(str, paths), check=False)
    return True


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
