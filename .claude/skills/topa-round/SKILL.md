---
name: topa-round
description: Operate running topa rounds — check status, message an agent, read its progress, review its PR, stop or clean up a round. Use when the user asks about a round, its agent, its PR, or wants to stop or remove one.
---

# topa round

Run `topa` from `PATH`. State lives in `~/.topa/`:
`ledger.jsonl` (every event), `rounds/<slug>/` (brief, evidence, `round.json`,
`environment.md`), `config.toml`.

## What to run

| The user wants | Run |
| --- | --- |
| an overview | `topa round status` (`--all` includes removed rounds) |
| one round's history | `topa round log <slug>` |
| what the agent is doing | `herdr agent read <pane>` (pane from status) |
| to tell the agent something | `topa round say <slug> "<text>"` |
| a new round | `topa round start <Op>` or `topa round start --issue <N>` (`--dry-run` first if unsure) |
| to stop the agent, keep everything | `topa round stop <slug>` |
| to release everything | `topa round stop <slug> --rm` |
| to run a command against a TileOPs branch outside a round | `cd <worktree> && topa run <cmd>` |

## Rules

- A message to an agent is one instruction in plain text, at most 4000 characters.
  Put long material in a file in the round directory and name the file.
- Change the brief templates (`~/topa/src/topa/kit/round/`) for future rounds,
  never a running round's `brief.md`. To reach a running round, use `say`.
- `stop` without `--rm` stops the agent and the container (the GPU is free) and archives
  the agent's session transcripts into `rounds/<slug>/session/`. The worktree, the branch and the
  round directory stay.
- `stop --rm` also removes the container, the TileOPs worktree, the TileFoundry checkout
  and the merged branch. Ask the user before `--rm` on a round whose PR is open.
- If `rounds/<slug>/foundry.patch` exists after a stop, the round changed TileFoundry
  source, which the brief forbids. Tell the user.
- TileLang facts a round found are in its `report.md` under `## TileLang knowledge`.
  Promoting them into `~/topa/src/topa/knowledge/tilelang.md` is a human decision:
  show them, do not merge them unasked.

## Reviewing a round's PR

Read `rounds/<slug>/report.md`, `provenance.json`, the PR and its comments.
`topa gate --round <slug>` rechecks the evidence. For a perf round, report the result
(`bar met` or `below bar`), the rows still behind their fastest comparator and any row
slower than main. For any round, report whether the PR resolves its issue and any
TileFoundry issue the round filed.
