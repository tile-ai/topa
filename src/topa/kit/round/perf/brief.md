## The bar

Optimize the op with [TileFoundry](https://github.com/tile-ai/TileFoundry) toward this bar.

| Result | When | Do |
| --- | --- | --- |
| `bar met` | Every row of the op, behind or not, beats its fastest comparable comparator in the median and in every repeat, or ties it within one timer step or at the DRAM floor. | Open the PR. |
| `below bar` | The bar is not met, and the candidate beats main on a row of the op: candidate / main below 0.99. | Open the PR; list the unmet rows in Result And Limitations. |
| none | Neither, or a person must decide. | Follow [Ending without a PR](#ending-without-a-pr). |

- Both `bar met` and `below bar` also require that no row of any op is slower than main:
  main / candidate below 0.99, the nightly's threshold for a row behind.
- "Any op" includes every op that shares a kernel the round changes.
- `topa gate` checks this rule on the Performance medians; the review checks the repeats.

## Perf rules

- Keep main's cache hints and launch policy exactly (`evict_first`, `evict_last`, cache
  modifiers, L2 persistence, PDL).
- Do not defer writes, add GPU sleeps, or take a win only the L2 flush state gives.
- Derive each dispatch rule from hardware and call facts, sweep it over the workloads, and
  cover it with a test.
- Do not change the benchmark, its timing, the comparators, the workloads or the public Op.
- Add no op.
- Do not change `benchmarks/`, `workloads/`, `src/tileops/manifest/`, `scripts/` or
  `.github/`; `topa gate` rejects such a diff.

## Prior attempts

{{PRIOR_ATTEMPTS}}

- Read the attempts that bear on the rows before choosing experiments; `prior-attempts.json`
  holds the full list when topa collected one. Check an earlier conclusion against this
  round's TileOPs, TileFoundry and GPU before relying on it.
- Under `## Prior attempts used` in `report.md`, cite each attempt that changes your plan
  and say whether you reuse, skip or retest it; for a retest, name the condition that
  changed. Otherwise write `none relevant` or `history unavailable`.

## Stages

- Find TileFoundry's commands with `tilefoundry --help`, each subcommand's `--help`,
  `tilefoundry tutorial` and `tilefoundry spec`, never from memory.
- Read every tutorial page before stage 1, and `{{KNOWLEDGE}}` before stage 3.
- Cite `tilefoundry --version` and the target with every analysis number.

### 1. Requirement and comparators

1. Locate the gap (kernel body, tuning pick, launch count or host idle time) with CUPTI
   per-kernel times and `ncu`.
2. Run each comparator through the repository benchmark with the settings its
   documentation recommends. Record any setting the benchmark cannot apply.
3. Classify each (workload, comparator) pair:

| Class | Timed | Counts toward the fastest comparator |
| --- | --- | --- |
| comparable (passes the shared case reference) | yes | yes |
| not semantically comparable; reason recorded | yes | no |
| cannot express the case; reason recorded | no | no |

4. List the last two classes in Result And Limitations.

Exit: every pair is classified, and each row behind has a comparable comparator. If one has
none, follow [Ending without a PR](#ending-without-a-pr).

### 2. HIR reference

1. Write the op's math in HIR.
2. Check it with TileFoundry against the repository reference's outputs on the same inputs,
   on every output of every workload.
3. Derive each tolerance of that check from the dtype and the serial roundings, and record
   the derivation. Never loosen one for a single workload.

Exit: the HIR matches the reference on every output of every workload.

### 3. Floor and placement

1. Establish each workload's floor and bottleneck with TileFoundry's analysis.
2. Record each workload's DRAM floor: the bytes it must move over the target's DRAM
   bandwidth.
3. Check the flops and traffic by hand: each input read once, each output written once.
4. Where the hand count and the analysis disagree, fix the HIR or the count, or record a
   TileFoundry finding.
5. List placements, including the incumbent's and the fastest comparator's, read from `ncu`
   and, where it exists, the comparator's generated source.
6. Compare the placements' predicted time, waves and storage with TileFoundry, choose one,
   and record why.

Exit: every workload has a checked floor, and the chosen placement has a recorded reason.

### 4. Implementation rounds

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
8. Measure (rules below), and record measured / floor, measured / predicted and
   comparator / measured.
9. Record the cause of a prediction error, or that it is unresolved, in TileFoundry
   Findings.

Measure this way:

- Snapshot the base and each candidate commit with
  `git -C {{TILEOPS_WORKTREE}} worktree add --detach {{ROUND_DIR}}/work/<name> <commit>`.
- Time each snapshot with its `src` first on `PYTHONPATH`.
- Run at least three repeats; a repeat is one cycle of main, candidate and comparators on
  the round's GPU, with the L2 flush.
- Keep each repeat's timings, and report the median of `device_busy_ms`.

Exit: the bar is met. Otherwise, exit when all three stop records hold, and write them
under `## Stop records` with the rows still behind and their last measured numbers; mark
each record that does not apply, with the reason.

| Record | Holds when |
| --- | --- |
| (1) Gap attribution | Each workload's measured / floor is listed, and each gap is tied to a bottleneck with `ncu` or TileFoundry data. |
| (2) Directions exhausted | Each direction on the recorded list for each bottleneck was measured or shown infeasible. |
| (3) TileFoundry consulted | Alternative placements were compared, the tutorial and spec pages on each bottleneck were read, and each direction they gave is on the list. |

### 5-7. Cleanup, review and PR

Follow [Cleanup](#cleanup), [Review](#review) and [PR](#pr), with these additions:

- Give the reviewer the rows, the Performance table with its raw timings, the timer resolution and
  the floors, and ask for the comparator classes, claimed ties and regressions against main.
- Keep a cleanup change only when the result of the bar is unchanged.
- Re-read the final HIR with the evidence before `topa pr --reviewed`.

## Verification

Run the affected tests on the GPU, `scripts/validate_manifest.py`, the benchmark and the
stage 3 analyses. Measure by the stage 4 rules, and set `result` in `provenance.json`.

## PR files

Title: `[Perf][foundry][<Family>] <imperative description>`.

| File | Becomes | Holds |
| --- | --- | --- |
| `pr-template.md` | the title (line 1, same as the commit) and the body, which becomes the squash commit's message | Summary alone: one-line bullets of the kernel change and the gap's root cause, then the `Op:`, `Benchmark:` and `Result:` lines, which later rounds search |
| `pr-comments.md` | one comment per `## ` section | the sections below |

- Test Plan: the `scripts/test_node_delta.py` output and the path each new case guards.
- TileFoundry Description: `work/final_hir.py` verbatim.
- Performance: one row per measured workload of the op, and the other ops that share a
  changed kernel. Delete the other-ops table when there are none.
- Result And Limitations: opens with the result.
- TileFoundry Findings.
- Keep every `## ` heading and every column.

## TileFoundry findings

- Record a TileFoundry bug that blocks you as a finding.
- Continue with the next stage the finding does not block; follow
  [Ending without a PR](#ending-without-a-pr) when it blocks the rest of the round.
- Keep its reproducer as it fails, with the command and raw output, in
  `work/blocked/<finding-id>/`.
- Before the PR or the stop report, open one issue on `tile-ai/TileFoundry` for all
  findings, in the form `{{TILEFOUNDRY_SOURCE}}/CONTRIBUTING.md` sets.
- Give each finding a section with its command, minimal HIR or TIR and raw output.
- Add a later finding to the same issue. Open no issue without a finding.

## Perf records

`provenance.json` also holds these fields. Each `<tilefoundry command>` starts with
`tilefoundry`.

```json
{
  "tilefoundry_base": "<40-hex>", "result": "bar met | below bar",
  "final_hir": "work/final_hir.py", "runtime_twin": "work/runtime_twin.py",
  "production_kernel": "src/tileops/<path>.py:<Symbol>",
  "reference_check": {"command": "<tilefoundry command>", "report": "<r>"},
  "floor": [{"workload": "<name>", "command": "<tilefoundry command>", "report": "<r>"}],
  "placements": [{"placement": "<tiling>", "chosen": true, "command": "<tilefoundry command>", "report": "<r>"}],
  "schedule": {"command": "<tilefoundry command>", "report": "<the TIR>"},
  "twin_check": {"command": "<tilefoundry command>", "report": "<r>"},
  "iterations": [{"change": "<what>", "basis": "<fact>", "predicted_us": 0.0, "measured_us": 0.0}],
  "benchmark": {"command": "<bench>", "report": "<r>"}
}
```

- Give `floor` one entry per workload, and `placements` at least two, exactly one chosen.
- Write `findings.json` as `{"findings": [], "issue": null}`, or with the issue URL and per
  finding `id`, `classification`, `command`, `expected`, `actual`, `workaround_cost`,
  `public_surface`, `affected_workloads` and `reproducer`.
