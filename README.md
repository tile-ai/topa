<p align="center">
  <img src="assets/readme-banner.svg" alt="TOPA: TileFoundry and TileOps, a Path to Autonomous kernel engineering" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
</p>

Agents build [TileOPs](https://github.com/tile-ai/TileOPs) kernels with
[TileFoundry](https://github.com/tile-ai/TileFoundry). Every kernel improves TileFoundry,
and every TileFoundry improvement speeds up the next kernel.

## Quick start

```bash
git clone https://github.com/tile-ai/topa ~/topa && cd ~/topa
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
export PATH="$HOME/topa/.venv/bin:$PATH"
mkdir -p ~/.topa && topa config --default > ~/.topa/config.toml   # then set the repo paths
topa setup
herdr server &             # agents run in herdr panes

topa hunt                  # ops TileOPs is behind on, from the newest nightly
topa issue new <Op>        # file or claim the op's issue
topa round start <issue>   # one agent, one GPU, one issue
topa round status          # every round, its agent and its PR
topa review next <PR>      # a review round of a TileOPs PR, driven by the topa-review skill
```

[Getting started](docs/usage.md) covers the prerequisites and each step.

## Docs

- [Getting started](docs/usage.md): from a new machine to a first round
- [How a round works](docs/workflow.md): the loop, from the nightly benchmark to a PR
- [Commands](docs/commands.md): every `topa` command
- [Layout](docs/layout.md): the repository, and what topa keeps on the machine
