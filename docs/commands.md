# Commands

| Command | Does |
| --- | --- |
| `topa setup` | check tools, build `topa:agent`, trust the round directories in Claude Code |
| `topa grade` | grade every op of the newest nightly |
| `topa hunt` | the behind ops, their issues, assignees and rounds |
| `topa issue new <Op>...` | file or claim one issue per op |
| `topa round start <issue>` | start a round |
| `topa round say <slug> "..."` | message the round's agent |
| `topa round status` | every round, its agent state and PR |
| `topa round log [<slug>]` | ledger events |
| `topa round stop <slug> [--rm]` | stop, archive, release |
| `topa gate` / `topa pr [--reviewed]` | check the evidence / open the PR (run by the agent) |
| `topa run <cmd>` | one command against a TileOPs worktree in a throwaway container |
