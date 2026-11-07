# Layout

## Repository

- `src/topa/kit/round/brief.md` is the round contract. `round start` fills its
  placeholders, and an unfilled one is an error.
- `src/topa/knowledge/tilelang.md` holds TileLang and ptxas facts that earlier rounds
  confirmed. Rounds read it. People add to it from round reports.
- `src/topa/checklists/` holds what a review checks beyond TileOPs's own docs and CI.
  `topa review next` names the ones the changed paths need.
- `src/topa/kit/container/` holds the agent image: the TileOPs runner image plus sshd.
- `src/topa/kit/stage.py` runs in the agent image, once per image and dependency list. It stages, for offline
  install, the wheels TileFoundry needs beyond the runner and the build backends of
  TileFoundry and TileOPs.
- `src/topa/gate.py` is the evidence gate that runs before a PR.
- `src/topa/grade.py` follows `scripts/gen_bench_pages.py` in tile-ai/TileOPs.github.io.
  Change both together.
- `.claude/skills/topa-hunt`, `topa-round` and `topa-review` drive these commands when you
  work in this repository.

## On the machine

```text
~/.topa/
  config.toml                  settings
  ledger.jsonl                 every event of every round; never rewritten
  rounds/<slug>/               the round's output; topa never deletes it
  reviews/<owner>/<repo>/<PR>/  a reviewed PR: state.json, round-<NN>/ inputs and findings,
                               and worktree/ at the PR head until the PR closes
  cache/                       rebuilt on demand; safe to delete when no round runs
    nightly/                   benchmark snapshots
    stage/<key>/               wheels and build backends a round installs beyond the runner
    tilelang/  triton/         kernel compile caches, shared by all rounds
  checkouts/tilefoundry/<slug>/  the round's TileFoundry checkout
  ssh/<slug>/  ssh-config/     the round's SSH key and host entry
<worktree_root>/tileops-<slug>/  the round's TileOPs worktree, on its own branch
```

What each step keeps:

| | running | `stop` | `stop --rm` |
| --- | --- | --- | --- |
| `rounds/<slug>/`: brief, `work/`, `evidence/`, `report.md`, PR text | yes | yes, plus `session/` (transcripts) and `foundry.patch` if TileFoundry changed | yes |
| TileOPs worktree and branch | yes | yes | removed; the branch only if merged |
| TileFoundry checkout, SSH key and entry | yes | yes | removed |
| container and its GPU | running | stopped; GPU free | removed |
| herdr pane and agent | running | closed | closed |

The PR and the TileFoundry issue live on GitHub.
