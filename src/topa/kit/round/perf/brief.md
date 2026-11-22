## The bar

Optimize the op with [TileFoundry](https://github.com/tile-ai/TileFoundry) toward this bar.

| Result | When | Do |
| --- | --- | --- |
| `bar met` | Every row of the op, behind or not, beats its fastest comparable comparator in the median and in every repeat, or ties it within one timer step or at the DRAM floor. | Open the PR. |
| `below bar` | The bar is not met, and the candidate beats main on a row of the op: candidate / main below 0.99. | Open the PR; list the unmet rows in Result And Limitations. |
| none | Neither, or a person must decide. | Follow [Ending without a PR](#ending-without-a-pr). |

{{FOUNDRY_BAR}}

## Perf rules

{{FOUNDRY_RULES}}
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

{{FOUNDRY_STATES}}

## Stages

{{FOUNDRY_COMMANDS}}

{{FOUNDRY_MEASURE}}

### Requirement and comparators

1. Locate the gap (kernel body, tuning pick, launch count or host idle time) with CUPTI
   per-kernel times and `ncu`.
2. Run each comparator through the repository benchmark with the settings its
   documentation recommends. Record any setting the benchmark cannot apply.
3. Classify each (workload, comparator) pair:

{{FOUNDRY_CLASSES}}

4. List the last two classes in Result And Limitations.
5. Write `bench`, and run `topa round measure "base" --base`.

Exit: every pair is classified, and each row behind has a comparable comparator. If one has
none, follow [Ending without a PR](#ending-without-a-pr).

{{FOUNDRY_HIR}}

### Implementation rounds

{{FOUNDRY_ROUNDS}}

### Cleanup, review and PR

{{FOUNDRY_FINISH}}

## PR files

Title: `[Perf][foundry][<Family>] <imperative description>`.

| File | Becomes | Holds |
| --- | --- | --- |
| `pr-template.md` | the title (line 1, same as the commit) and the body, which becomes the squash commit's message | Summary alone: one-line bullets of the kernel change and the gap's root cause, then the `Op:`, `Benchmark:` and `Result:` lines, which later rounds search |
| `pr-comments.md` | one comment per `## ` section | the sections below |

{{FOUNDRY_COMMENTS}}
