"""`topa setup`: the one-time machine setup a round relies on.

It checks the tools, starts the rootless Docker daemon if one is configured,
builds the agent image on the TileOPs runner image, and lets Claude Code trust the
round directories.
"""

from __future__ import annotations

import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path

from topa import config as config_module
from topa import sh
from topa.config import Config


def setup(config: Config) -> list[str]:
    done = []
    sh.require("claude", "docker", "gh", "git", "herdr", "nvidia-smi")
    for repo in (config.tileops_repo, config.tilefoundry_repo):
        if not (repo / ".git").exists():
            raise SystemExit(f"not a git checkout: {repo}")
    for path in (config_module.HOME, config.rounds, config.worktree_root):
        path.mkdir(parents=True, exist_ok=True)
    if not config_module.CONFIG.exists():
        config_module.CONFIG.write_text(config_module.default_toml())
        done.append(f"wrote {config_module.CONFIG}")

    sh.bootstrap_docker(config)
    if not sh.docker_ok(config, "image", "inspect", config.runner_image):
        sh.note(f"pulling {config.runner_image}")
        sh.docker(config, "pull", config.runner_image, capture=False)
    context = Path(str(files("topa") / "kit" / "container"))
    sh.docker(config, "build", "--quiet", "--build-arg", f"BASE_IMAGE={config.runner_image}",
              "--tag", config.agent_image, context)
    done.append(f"built {config.agent_image} on {config.runner_image}")

    if trust_rounds(config.rounds):
        done.append(f"marked {config.rounds} as trusted in ~/.claude.json")
    return done


def trust_rounds(rounds: Path) -> bool:
    """Accept Claude Code's folder-trust dialog once for every round directory.

    Each round starts Claude Code in a new directory, and the dialog defaults to
    "No, exit". Claude Code accepts a folder whose parent is trusted, so trusting
    the rounds root covers every round.
    """
    path = Path.home() / ".claude.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    entry = data.setdefault("projects", {}).setdefault(str(rounds), {})
    if entry.get("hasTrustDialogAccepted"):
        return False
    entry["hasTrustDialogAccepted"] = True
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".claude.json.")
    with os.fdopen(handle, "w") as out:
        json.dump(data, out, indent=2)
    os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    os.replace(tmp, path)
    return True
