# How a round works

One round is one agent making one [TileOPs](https://github.com/tile-ai/TileOPs) op reach
its fastest comparator:

1. The TileOPs nightly writes a Perf Tracker page to the TileOPs wiki: the ops behind, in
   the order to work them, with each op's rows. `topa hunt` reads the newest page and shows
   what already works each op.
2. `topa round start <Op>` creates a TileOPs worktree and branch, a TileFoundry checkout,
   a container with one GPU, and a round directory with the brief. The brief carries the
   op's rows. Then it starts Claude Code in a herdr pane and tells it to read the brief.
3. The agent runs on the host. A PreToolUse hook sends each shell line that runs anything
   but `git`, `gh`, `codex`, `topa`, a file operation or a text tool into the round's
   container over SSH.
4. The agent follows the seven stages of the
   [perf brief](../src/topa/kit/round/perf/brief.md), from classifying the comparators to
   opening the PR with `topa pr`. That command checks the evidence, opens the PR from your
   fork, and posts the test plan and the evidence as five comments.
5. TileFoundry problems the agent hits go into one TileFoundry issue.
6. A round that misses the bar on some rows still opens its PR when it beats main and
   slows no row; the unmet rows go under Result And Limitations. Otherwise it opens no PR
   and writes its stop records in `report.md`.
7. `topa round stop <slug> [--rm]` stops the agent, archives its transcripts, and with
   `--rm` releases the container, the worktree and the checkout.

```mermaid
flowchart LR
    N["nightly benchmark"] -- "Perf Tracker page" --> Q["wiki queue"]
    Q -- "topa round start" --> A["agent + GPU container"]
    A -- "topa pr" --> P["TileOPs PR"]
    A -- "findings" --> F["TileFoundry issue"]
    P --> N
    F -- "TileFoundry fix" --> A
```

A merged PR moves the nightly. A TileFoundry fix reaches the next round.

## Issue rounds

`topa round start --issue <N>` works TileOPs issue N instead of a queue op. The issue body
is the requirement, and the start assigns the issue to you.

| Issue title | Contract | PR links the issue with |
| --- | --- | --- |
| starts with `[Perf]` | the seven stages and the bar above, on the workloads the issue lists | `Refs #N`; the nightly confirms the result |
| anything else | understand, write a failing test, implement, verify, review, clean up, PR | `Closes #N` |

`topa gate` runs the shared checks on every round: the commit binding, the recorded tests
and reviews, and the PR text. Only a perf round has the forbidden paths, the Performance
table, the TileFoundry records and `findings.json` checked.
