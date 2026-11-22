<!-- The TileFoundry text the perf, new-op and extend briefs share. Each `<!-- NAME -->` line starts
the text that fills `{{FOUNDRY_NAME}}` in a kind's brief. -->

<!-- BAR -->
- Both `bar met` and `below bar` also require that no row of a Performance table with a main
  column is slower than main: main / candidate below 0.99, the nightly's threshold for a row behind.
- The other-ops table holds every op that shares a kernel the round changes.
- `topa gate` checks this rule on the Performance medians; the review checks the repeats.

<!-- RULES -->
- Keep main's cache hints and launch policy exactly in each kernel the round changes
  (`evict_first`, `evict_last`, cache modifiers, L2 persistence, PDL).
- Do not defer writes, add GPU sleeps, or take a win only the L2 flush state gives.
- Derive each dispatch rule from hardware and call facts, sweep it over the workloads, and
  cover it with a test.

<!-- STATES -->
## States

The round moves along `{{STATE_PATH}}`. STOPPED ends it without a PR.

| State | Follow |
| --- | --- |
| REQ | [Requirement and comparators](#requirement-and-comparators) |
| HIR | [HIR reference](#hir-reference) |
| FLOOR | [Floor and placement](#floor-and-placement) |
| IMPL | [Implementation rounds](#implementation-rounds) |
| CLEAN | [Cleanup](#cleanup) |
| REVIEW | [Review](#review) |
| FINAL | [Final review](#final-review) |
| PR | [PR](#pr) steps 1-3 and 7-14; skip steps 4-6, which CLEAN, REVIEW and FINAL did |
| CI | [PR](#pr) steps 15-17 |
| STOPPED | [Ending without a PR](#ending-without-a-pr) |

- At each exit, run `{{TOPA}} round transition <state> --round {{SLUG}} --reason "<why>"`.
- The command refuses a move the state graph lacks, and a move whose records `topa gate`
  would reject; fix what it names and run it again.
- Run `{{TOPA}} pr` only in state PR. Move to CI once the PR is published.
- When a check fails, move back to the state that owns what the fix changes (HIR, FLOOR,
  OP, IMPL or CLEAN), or stay in the current state.
- When the rebase in PR step 1 changes the commits, or CI needs a change, move to CLEAN.
  Once the PR is published, REVIEW exits to PR.
- When a `topa round say` message changes the requirement, move to REQ.
- Move to STOPPED, after writing `## Stop records`, when a state cannot exit: a case that
  needs a comparable comparator has none, correctness is not reached, a row of a table with a
  main column is slower than main, the directions are exhausted with no publishable
  result, a dispute survives two reviews, a blocking fault survives final review pass 2, a
  TileFoundry finding blocks a required record, or a person says stop.

<!-- COMMANDS -->
- Find TileFoundry's commands with `tilefoundry --help`, each subcommand's `--help`,
  `tilefoundry tutorial` and `tilefoundry spec`, never from memory.
- Read every tutorial page before the first stage, and `{{KNOWLEDGE}}` before
  [Floor and placement](#floor-and-placement).
- Cite `tilefoundry --version` and the target with every analysis number.

<!-- MEASURE -->
- Name every row `<Op> <case_id>`: the op, then the parametrized part of its benchmark test
  name, such as `SoftmaxFwdOp attn-weights-4k-float16`. Records, tables and `workloads` use
  this name.
- Write `bench` in `provenance.json`: for each op, the pytest nodes of its benchmark, as
  `{"<Op>": ["benchmarks/ops/<file>.py::<test>"]}`.
- Run `{{TOPA}} round measure "<title>" --round {{SLUG}}` to time those nodes on HEAD;
  `--base` times the TileOPs base. It records every row and prints each row's DRAM floor.
- Only these measurements enter the round's record; time anything else as you need.

<!-- CLASSES -->
| Class | Timed | Counts toward the fastest comparator |
| --- | --- | --- |
| comparable (passes the shared case reference) | yes | yes |
| not semantically comparable; reason recorded | yes | no |
| cannot express the case; reason recorded | no | no |

<!-- HIR -->
### HIR reference

1. Write the op's math in HIR.
2. Check it with TileFoundry against the reference's outputs on the same inputs, on every
   output of every workload.
3. Derive each tolerance of that check from the dtype and the serial roundings, and record
   the derivation. Never loosen one for a single workload.

Exit: the HIR matches the reference on every output of every workload.

### Floor and placement

1. Establish each workload's floor and bottleneck with TileFoundry's analysis.
2. Take each workload's DRAM floor from `topa round measure`: the benchmark's byte count
   over the target's HBM bandwidth.
3. Check the analysis's flops and traffic against that byte count.
4. Where the hand count and the analysis disagree, fix the HIR or the count, or record a
   TileFoundry finding.
5. List placements, including the incumbent's when the op has one and the fastest
   comparator's, read from `ncu` and, where it exists, the comparator's generated source.
6. Compare the placements' predicted time, waves and storage with TileFoundry, choose one,
   and record why.

Exit: every workload has a checked floor, and the chosen placement has a recorded reason.

<!-- ROUNDS -->
Run each round in this order, and add it to `iterations` in `provenance.json`:

1. Change the HIR's placement or schedule, and predict its time with TileFoundry.
2. Continue if the prediction beats the current kernel's placement on the targeted rows.
   Otherwise continue only with a named effect HIR cannot express and the measurement that
   would show it.
3. Derive the schedule (instructions, buffer depths, layouts) with TileFoundry, and lower
   it to TIR.
4. Write the TileLang kernel from that TIR.
5. Recheck the HIR against the reference if the math, outputs or shapes changed.
6. Check with TileFoundry that the runtime twin, which calls the production kernel, matches
   the HIR.
7. Run the tests of the op and of every op that shares a changed kernel.
8. Run `{{TOPA}} round measure "<the change>" --round {{SLUG}}` on the candidate commit,
   then measure by the rules below, and record measured / floor, measured / predicted and
   comparator / measured.
9. Record the cause of a prediction error, or that it is unresolved, in TileFoundry
   Findings.

Measure this way:

- Snapshot the base and each candidate commit with
  `git -C {{TILEOPS_WORKTREE}} worktree add --detach {{ROUND_DIR}}/work/<name> <commit>`.
- Time each snapshot with its `src` first on `PYTHONPATH`.
- Run at least three repeats; a repeat is one cycle of main, candidate and comparators on
  the round's GPU, with the L2 flush. Time main only on the rows of a table with a main column.
- Keep each repeat's timings, and report the median of `device_busy_ms`.
- Before timing an other-ops row, compare the generated kernel program (the CUDA source) of
  main and the candidate on it. Time only the rows whose program differs. Write `unchanged`
  in both time cells of a row whose program is main's byte for byte, and record the
  comparison that shows it as `program_diff`.

Write the Performance section of `pr-comments.md` from the last measurement.

Exit: the bar is met. Otherwise, exit when all three stop records hold, and write them
under `## Stop records` with the rows below the bar and their last measured numbers; mark
each record that does not apply, with the reason.

| Record | Holds when |
| --- | --- |
| (1) Gap attribution | Each workload's measured / floor is listed, and each gap is tied to a bottleneck with `ncu` or TileFoundry data. |
| (2) Directions exhausted | Each direction on the recorded list for each bottleneck was measured or shown infeasible. |
| (3) TileFoundry consulted | Alternative placements were compared, the tutorial and spec pages on each bottleneck were read, and each direction they gave is on the list. |

<!-- FINISH -->
Follow [Cleanup](#cleanup), [Review](#review) and [PR](#pr), with these additions:

- Give the reviewer the rows, the Performance table with its raw timings, the timer resolution and
  the floors, and ask for the comparator classes, claimed ties and regressions against main.
- Keep a cleanup change only when the result of the bar is unchanged.
- Re-read the final HIR with the evidence before `topa pr --reviewed`.

## Verification

Run the affected tests on the GPU, `scripts/validate_manifest.py`, the benchmark and the
[Floor and placement](#floor-and-placement) analyses. Measure by the rules of
[Implementation rounds](#implementation-rounds), and set `result` in `provenance.json`.

<!-- COMMENTS -->
- Test Plan: the `scripts/test_node_delta.py` output and the path each new case guards.
- TileFoundry Description: `work/final_hir.py` verbatim.
- Performance: one row per measured workload of the op, named `<Op> <case_id>`, with the DRAM
  floor `topa round measure` printed for it; and in the other-ops table the other rows checked
  against main. Delete that table when there are none.
- Result And Limitations: opens with the result.
- TileFoundry Findings.
- Keep every `## ` heading and every column.

## TileFoundry findings

- Record a TileFoundry bug that blocks you as a finding.
- Continue with the next stage the finding does not block; follow
  [Ending without a PR](#ending-without-a-pr) when it blocks the rest of the round.
- Keep its reproducer as it fails, with the command and raw output, in
  `work/blocked/<finding-id>/`.
- Before the PR or the stop report, open one issue on `{{FINDINGS_REPO}}` for all
  findings, in the form `{{TILEFOUNDRY_SOURCE}}/CONTRIBUTING.md` sets.
- Open no issue on `tile-ai/TileFoundry`. A scheduled job forwards findings from
  `{{FINDINGS_REPO}}`.
- Title the issue `<type>(<scope>,...): <summary>`, for example
  `fix(analysis,schedule): price a grid's waves once`.
  - `<type>` is `fix` when TileFoundry gets a case wrong and `feat` when it lacks one.
  - Each `<scope>` is a TileFoundry component the findings touch, among {{FINDING_SCOPES}}.
    Separate scopes with a comma and no space.
- Label the issue with one `scope:<scope>` per title scope.
- Add the label `type:feat` when `<type>` is `feat`. A `fix` takes no type label.
- Retitle and relabel the issue when a later finding adds a scope.
- Give each finding a section with its command, minimal HIR or TIR and raw output.
- Add a later finding to the same issue. Open no issue without a finding.

## TileFoundry records

`provenance.json` also holds these fields. Each `<tilefoundry command>` starts with
`tilefoundry`.

```json
{
  "tilefoundry_base": "<40-hex>", "result": "bar met | below bar",
  "final_hir": "work/final_hir.py", "runtime_twin": "work/runtime_twin.py",
  "production_kernel": "src/tileops/<path>.py:<Symbol>",
  "reference_check": {"command": "<tilefoundry command>", "report": "<r>"},
  "bench": {"<Op>": ["benchmarks/ops/<file>.py::<test>"]},
  "floor": [{"workload": "<Op> <case_id>", "command": "<tilefoundry command>", "report": "<r>"}],
  "placements": [{"placement": "<tiling>", "chosen": true, "command": "<tilefoundry command>", "report": "<r>"}],
  "schedule": {"command": "<tilefoundry command>", "report": "<the TIR>"},
  "twin_check": {"command": "<tilefoundry command>", "report": "<r>"},
  "iterations": [{"change": "<what>", "basis": "<fact>", "predicted_us": 0.0, "measured_us": 0.0}],
  "benchmark": {"command": "<bench>", "report": "<r>"},
  "program_diff": {"command": "<the command that compares each other-ops row's program>", "report": "<r>"}
}
```

- Give `floor` one entry per workload, and `placements` at least two, exactly one chosen.
- Record `program_diff` when an other-ops row is `unchanged`.
- Write `findings.json` as `{"findings": [], "issue": null}`, or with the issue URL, its
  `title` and `labels` as they stand on GitHub, and per finding `id`, `classification`,
  `command`, `expected`, `actual`, `workaround_cost`, `public_surface`, `affected_workloads`
  and `reproducer`.
