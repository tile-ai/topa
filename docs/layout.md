# Layout

## Repository

- `src/topa/kit/round/brief.md` is the round contract. `round start` fills its
  placeholders, and an unfilled one is an error.
- `src/topa/knowledge/tilelang.md` holds TileLang and ptxas facts that earlier rounds
  confirmed. Rounds read it. People add to it from round reports.
- `src/topa/kit/container/` holds the agent image: the TileOPs runner image plus an
  entrypoint that sets up git, gh and `topa`. The container mounts the host's Claude Code
  binary, `~/.claude` (its code-carrying parts read-only), herdr's socket, and topa's
  source, config and ledger.
- `src/topa/kit/round/pr-template.md` is the PR a round fills in; `topa pr` checks it.
- `src/topa/kit/stage.py` runs in the agent image, once per image and dependency list. It stages, for offline
  install, the wheels TileFoundry needs beyond the runner and the build backends of
  TileFoundry and TileOPs.
- `src/topa/gate.py` is the evidence gate that runs before a PR.
- `src/topa/grade.py` follows `scripts/gen_bench_pages.py` in tile-ai/TileOPs.github.io.
  Change both together.
- `.claude/skills/topa-hunt` and `topa-round` drive these commands when you work in this
  repository.

## On the machine

```text
~/.topa/
  config.toml                  settings
  ledger.jsonl                 every event of every round; never rewritten
  rounds/<slug>/               the round's output; topa never deletes it
  cache/                       rebuilt on demand; safe to delete when no round runs
    nightly/                   benchmark snapshots
    stage/<key>/               wheels and build backends a round installs beyond the runner
    tilelang/  triton/         kernel compile caches, shared by all rounds
  checkouts/tilefoundry/<slug>/  the round's TileFoundry checkout
<worktree_root>/tileops-<slug>/  the round's TileOPs worktree, on its own branch
```

What each step keeps:

| | running | `stop` | `stop --rm` |
| --- | --- | --- | --- |
| `rounds/<slug>/`: brief, `bin/claude`, `work/`, `evidence/`, `report.md`, `pr-template.md` | yes | yes, plus `session/` (transcripts) and `foundry.patch` if TileFoundry changed | yes |
| ledger | yes | yes | yes |
| TileOPs worktree and branch | yes | yes | removed; the branch only if merged |
| TileFoundry checkout | yes | yes | removed |
| container and its GPU | running | stopped; GPU free | removed |
| herdr pane and agent | running | closed | closed |

The PR and the TileFoundry issue live on GitHub.
