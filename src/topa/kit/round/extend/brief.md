## The bar

Change the ops the requirement names with kernels built with
[TileFoundry](https://github.com/tile-ai/TileFoundry), toward this bar. The rows to serve are
the cases main refuses with `OpNotAvailableError`, and each case the issue names.

| Result | When | Do |
| --- | --- | --- |
| `bar met` | Every row to serve beats its fastest comparable comparator in the median and in every repeat, or ties it within one timer step or at the DRAM floor. | Open the PR. |
| `below bar` | The bar is not met, every row to serve is served, correct and tested, and the three stop records of [Implementation rounds](#implementation-rounds) hold. | Open the PR; list the unmet rows in Result And Limitations. |
| none | Neither, or a person must decide. | Follow [Ending without a PR](#ending-without-a-pr). |

{{FOUNDRY_BAR}}

## Extension rules

{{FOUNDRY_RULES}}
- Change kernels, the op layer where it passes values to a kernel, tests, and `benchmarks/`
  or `workloads/` only where a row to serve needs them.
- Keep each op's public interface. Add no op.
- Do not change the benchmark timing or comparators of rows main serves.
- Do not change `src/tileops/manifest/`, `scripts/` or `.github/`; `topa gate` rejects such a diff.

{{FOUNDRY_STATES}}
- In state OP, follow [Op and tests](#op-and-tests). FLOOR exits to OP, and OP to IMPL.
- Also move to STOPPED when a row to serve needs a manifest change, or a manifest entry
  changed upstream.

## Stages

{{FOUNDRY_COMMANDS}}

{{FOUNDRY_MEASURE}}

### Requirement and comparators

1. Read the issue, the manifest entries (signature, workloads, `ref_api`, roofline), the Op and
   kernel code of each op, and the TileOPs `docs/design/` pages on them.
2. Run each op's benchmark on the base, and list the cases it reports unavailable.
3. Write `bench`, run `topa round measure "base" --base`, and write the rows to serve into
   `workloads` in `provenance.json`, each named as the measurement names it.
4. List in `report.md` the kernels the change touches and every op that calls them.
5. List the comparators: the reference implementation the manifest or the issue names, each
   library the issue names, and torch and `torch.compile` of the reference.
6. Run each comparator on every row to serve with the settings its documentation recommends.
   Record any setting that cannot be applied.
7. Classify each (row, comparator) pair:

{{FOUNDRY_CLASSES}}

8. List the last two classes in Result And Limitations.

Exit: every pair is classified, and every row to serve has a comparable comparator.

{{FOUNDRY_HIR}}

### Op and tests

1. Pass the values of the rows to serve from the Op to its kernel, as TileOPs `docs/design/` sets.
2. Add the tests TileOPs `docs/design/testing.md` sets for those values.
3. Run the new tests on the base, and record the run as `base_tests`,
   `{"command": "<pytest>", "report": "<r>"}`, in `provenance.json`. A test of a row main
   refuses fails there.

Exit: the new tests exist, and their run on the base is recorded.

### Implementation rounds

A row main refuses has no current kernel: at step 2 the first round continues on it with the
placement [Floor and placement](#floor-and-placement) chose.

{{FOUNDRY_ROUNDS}}

### Cleanup, review and PR

{{FOUNDRY_FINISH}}

## PR files

Title: `[Feat][foundry][<Family>] <imperative description>`.
Why: `[foundry]` makes TileOPs add the `foundry` label.

| File | Becomes | Holds |
| --- | --- | --- |
| `pr-template.md` | the title (line 1, same as the commit) and the body, which becomes the squash commit's message | Summary alone: one-line bullets of the rows the ops now serve, the kernel change and the comparators, then the issue link |
| `pr-comments.md` | one comment per `## ` section | the sections below |

{{FOUNDRY_COMMENTS}}

- Name each `floor` record's `workload`, and each row of the results table, as `workloads`
  names it; give the results table the rows to serve alone.
- Give the other-ops table every other case of the requirement, the op in the first column
  and the case id in the second. `topa round transition` and `topa gate` require each case
  the measurement reports in one of the two tables.
