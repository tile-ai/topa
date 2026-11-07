# Layout

## Repository

- `src/topa/kit/round/brief.md` is the round contract shared by every round.
  `perf/brief.md` and `general/brief.md` hold the stages of each kind.
- `round start` joins the shared brief with the round's kind and fills the placeholders;
  an unfilled one is an error.
- Each kind folder also holds the PR text the agent fills in:
  - `pr-template.md`: the title and a Summary-only body;
  - `pr-comments.md`: one comment per section, Test Plan and, for perf, the four evidence
    comments.
- `src/topa/knowledge/tilelang.md` holds TileLang and ptxas facts that earlier rounds
  confirmed. Rounds read it. People add to it from round reports.
- `src/topa/kit/review/checklists/` holds what a review checks beyond TileOPs's own docs and CI.
  `topa review prepare` names the ones the changed paths need.
- `src/topa/kit/container/` holds the agent image: the TileOPs runner image plus sshd.
- `src/topa/kit/stage.py` runs in the agent image, once per image and dependency list. It stages, for offline
  install, the wheels TileFoundry needs beyond the runner and the build backends of
  TileFoundry and TileOPs.
- `src/topa/gate.py` is the evidence gate that runs before a PR.
- `src/topa/wiki.py` reads the Perf Tracker page that `scripts/perf_tracker.py` in
  tile-ai/TileOPs writes. Change both together.
- `.claude/skills/topa-hunt`, `topa-round` and `topa-review` drive these commands when you
  work in this repository.

## On the machine

```text
~/.topa/
  config.toml                  settings
  ledger.jsonl                 every event of every round; never rewritten
  claim.lock                   held while a round claims its op
  rounds/<slug>/               the round's output; topa never deletes it
  reviews/<owner>/<repo>/<PR>/  a reviewed PR: its inputs, findings.json, and worktree/ at
                               the PR head until `topa review post`
  cache/                       rebuilt on demand; safe to delete when no round runs
    wiki/                      the TileOPs wiki clone that holds the Perf Tracker
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
