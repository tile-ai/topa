# Commands

| Command | Does |
| --- | --- |
| `topa setup` | check tools, build `topa:agent`, include the round SSH entries |
| `topa config [--default]` | print the settings in effect / print a default `config.toml` |
| `topa grade` | grade every op of the newest nightly |
| `topa hunt` | the behind ops, their issues, assignees and rounds |
| `topa issue new <Op>...` | file or claim one issue per op |
| `topa round start <issue>` | start a round |
| `topa round say <slug> "..."` | message the round's agent |
| `topa round status` | every round, its agent state and PR |
| `topa round log [<slug>]` | ledger events |
| `topa round stop <slug> [--rm]` | stop, archive, release |
| `topa gate` / `topa pr [--reviewed]` | check the evidence / open the PR (run by the agent) |
| `topa review next <PR> [--wait <s>]` | prepare the PR's next review round, if one is due |
| `topa review post <PR>` | post the round's findings as one review |
| `topa run <cmd>` | one command against a TileOPs worktree in a throwaway container |
