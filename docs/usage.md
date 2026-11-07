# Getting started

You need x86_64 Linux with an NVIDIA driver for CUDA 13.2 or newer, Docker with the NVIDIA
runtime, Claude Code and the coding agent CLI that `reviewer` in `~/.topa/config.toml`
names, both logged in, `gh`, `git`, `uv` and the [herdr TUI](https://herdr.dev/tui).

## 1. Set up Git and GitHub

The agent commits, pushes and comments as you. The gh token must live in
`~/.config/gh/hosts.yml`, because the round container reads it from there.

```bash
git config --global user.name "<Your Name>"
git config --global user.email "<you@example.com>"
gh auth login --insecure-storage
gh auth setup-git
gh repo fork tile-ai/TileOPs --clone=false
```

## 2. Clone and install

```bash
git clone https://github.com/tile-ai/TileOPs ~/TileOPs
git -C ~/TileOPs remote add fork https://github.com/<you>/TileOPs
git clone https://github.com/tile-ai/TileFoundry ~/TileFoundry

git clone https://github.com/tile-ai/topa ~/topa && cd ~/topa
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
export PATH="$HOME/topa/.venv/bin:$PATH"
```

## 3. Configure

```bash
mkdir -p ~/.topa && topa config --default > ~/.topa/config.toml
```

Set at least `tileops_repo`, `tilefoundry_repo`, `worktree_root` and `reviewer` in that
file; a `TOPA_<NAME>` environment variable overrides one setting for one command.

| Setting | What it sets | Unset |
| --- | --- | --- |
| `tileops_repo` | The TileOPs checkout rounds branch from | `~/TileOPs` |
| `tilefoundry_repo` | The TileFoundry checkout rounds read | `~/TileFoundry` |
| `worktree_root` | Where round worktrees go | `~/TileOPs-worktrees` |
| `runner_image` | The TileOPs runner image the agent image builds on | the TileOPs CI image |
| `agent_image` | The tag `topa setup` builds | `topa:agent` |
| `gpu` | The one card every round uses | topa picks a free card |
| `gpu_exclude` | Cards topa never uses | none |
| `docker_host` | `DOCKER_HOST` for topa's Docker calls | Docker's default |
| `docker_bootstrap` | A command run before Docker is used, such as one starting a rootless daemon | none |
| `proxy_host` | The host as the container reaches it; replaces a loopback address in the host's proxy variables | the variables pass unchanged |
| `no_proxy` | The container's proxy bypass list | the host's `NO_PROXY` |
| `upstream` | The repository PRs open against | `tile-ai/TileOPs` |
| `fork_owner` | The fork a round pushes to | the `gh` login |
| `model`, `effort` | The round agent's model and effort | the agent's own defaults |
| `reviewer` | The coding agent command a round reviews its diff with, run on the host from the TileOPs worktree: a fresh session apart from the round agent that cannot write, takes the prompt as its last argument and prints the review | required |
| `tilefoundry_commit` | The TileFoundry commit new rounds read | the newest main |
| `max_rounds` | How many rounds may be live at once | 2 |

A config file from an older topa fails to load until you delete its `tracker` line. Then:

```bash
topa setup       # pulls the runner image and builds topa:agent
herdr server     # keep it running in its own terminal
```

## 4. Run a round

```bash
topa hunt                  # the queue of ops behind, with their rows and rounds
topa round start <Op>      # start an agent on one op of the queue
```

`topa hunt` reads the newest `Perf-Tracker-<date>` page of the
[TileOPs wiki](https://github.com/tile-ai/TileOPs/wiki/Perf-Tracker), which the nightly
writes. The op's rows on that page, with its manifest, workloads, tests and benchmark, are
the round's requirement; topa files no issue for it.

The agent works on its own. It either opens a PR from your fork or writes its stop
records in `report.md`; [How a round works](workflow.md) says when it does which.
Follow it with `topa round status`, read its screen with `herdr agent read <pane-id>`, and
send it instructions with `topa round say <slug> "..."`.

When the PR is merged or closed, or `report.md` holds stop records, end the round:

```bash
topa round stop <slug> --rm
```

`--rm` deletes the round's worktree, including uncommitted changes.

Other commands are in [Commands](commands.md); what topa keeps on disk is in
[Layout](layout.md).
