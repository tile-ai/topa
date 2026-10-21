# Install

```bash
git clone https://github.com/tile-ai/topa ~/topa
cd ~/topa
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/topa setup        # writes ~/.topa/config.toml, builds the agent image
```

Edit `~/.topa/config.toml` (paths of the TileOPs and TileFoundry checkouts, Docker
host, GPU), then run `topa setup` again. `topa config` prints the settings in effect.
`TOPA_<NAME>` in the environment overrides one setting for one command.

It needs `claude` (logged in; rounds reuse its login), `docker` with the NVIDIA runtime,
`gh` (logged in), `git`, `herdr`, `nvidia-smi` and `uv`.
