# Commands

| Command | Does |
| --- | --- |
| `topa setup` | check tools, build `topa:agent`, include the round SSH entries |
| `topa config [--default]` | print the settings in effect / print a default `config.toml` |
| `topa hunt [--cached]` | the newest Perf Tracker queue, with each op's rows and rounds |
| `topa round start <Op> [--issue <N>]` | start a perf round on an op of the queue, or a new-op round on a spec-only op |
| `topa round start --issue <N>` | start a perf, new-op or general round on a TileOPs issue and assign it to you |
| `topa round transition <state> --reason "..."` | move a perf or new-op round along its state graph (run by the agent) |
| `topa round say <slug> "..."` | message the round's agent |
| `topa round status` | every round, its state, its agent and its PR |
| `topa round log [<slug>]` | ledger events |
| `topa round stop <slug> [--rm]` | stop, archive, release |
| `topa gate` / `topa pr [--reviewed]` | check the evidence / open the PR (run by the agent; a perf or new-op round must be in state PR) |
| `topa review prepare <PR>` | write the PR's review inputs and check out its head |
| `topa review post <PR>` | post the findings as one COMMENT review |
| `topa run <cmd>` | one command against a TileOPs worktree in a throwaway container |
