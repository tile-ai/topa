## The bar

Add the op with a kernel built with [TileFoundry](https://github.com/tile-ai/TileFoundry),
toward this bar.

| Result | When | Do |
| --- | --- | --- |
| `bar met` | Every workload row of the op beats its fastest comparable comparator in the median and in every repeat, or ties it within one timer step or at the DRAM floor. | Open the PR. |
| `below bar` | The bar is not met, the op is implemented, correct and tested, and the three stop records of [Implementation rounds](#implementation-rounds) hold. | Open the PR; list the unmet rows in Result And Limitations. |
| none | Neither, or a person must decide. | Follow [Ending without a PR](#ending-without-a-pr). |

{{FOUNDRY_BAR}}

## New-op rules

{{FOUNDRY_RULES}}
- Change only the `status` line of the op's manifest entry.
- Do not change the benchmark timing or comparators of existing ops.
- Add no op beyond the one the requirement names.
- Do not change `scripts/` or `.github/`; `topa gate` rejects such a diff.

{{FOUNDRY_STATES}}
- In state OP, follow [Op and tests](#op-and-tests). FLOOR exits to OP, and OP to IMPL.
- Also move to STOPPED when the manifest entry is wrong, needs an interface change, or
  changed upstream.

## Stages

{{FOUNDRY_COMMANDS}}

### Requirement and comparators

1. Read the issue when there is one, the op's manifest entry (signature, workloads,
   `ref_api`, roofline), and the TileOPs `docs/design/` pages on adding an op.
2. Check the entry against `ref_api` on small inputs of each case.
3. Decide whether the op extends an existing kernel or needs a new one, and list in
   `report.md` the existing ops whose kernels the change touches.
4. List the comparators: the reference implementation the manifest or the issue names,
   each library the issue names, and torch and `torch.compile` of the reference.
5. Run each comparator on every case with the settings its documentation recommends.
   Record any setting that cannot be applied.
6. Classify each (case, comparator) pair:

{{FOUNDRY_CLASSES}}

7. List the last two classes in Result And Limitations.

Exit: every pair is classified, and every case has a comparable comparator.

{{FOUNDRY_HIR}}

### Op and tests

1. Add the Op, its call spec and its kernel interface as TileOPs `docs/design/` sets.
2. Set the op's manifest `status` to `implemented`.
3. Add the tests TileOPs `docs/design/testing.md` sets for the op.
4. Add the op's benchmark against the comparators of
   [Requirement and comparators](#requirement-and-comparators). Add rows to `workloads/`
   only when the manifest needs them.
5. Run the new tests on the base, where the op does not exist, and record the failing run
   as `base_tests`, `{"command": "<pytest>", "report": "<r>"}`, in `provenance.json`.

Exit: the Op, its tests and its benchmark exist, and the failing run on the base is
recorded.

### Implementation rounds

The first round has no current kernel: at step 2 it continues with the placement
[Floor and placement](#floor-and-placement) chose.

{{FOUNDRY_ROUNDS}}

### Cleanup, review and PR

{{FOUNDRY_FINISH}}

## PR files

Title: `[Feat][foundry][<Family>] <imperative description>`. Why: `[foundry]` makes TileOPs
add the `foundry` label.

| File | Becomes | Holds |
| --- | --- | --- |
| `pr-template.md` | the title (line 1, same as the commit) and the body, which becomes the squash commit's message | Summary alone: one-line bullets of the op, its kernel's placement and its comparators, then the `Op:`, `Benchmark:` and `Result:` lines, which later rounds search |
| `pr-comments.md` | one comment per `## ` section | the sections below |

{{FOUNDRY_COMMENTS}}

- A new op may wrap an existing kernel: `production_kernel` need not change, and the twin
  check still runs.
- Name each `floor` record's `workload`, and the first cell of each Performance row, by its
  case exactly as the requirement lists it, such as `llama3-8b-prefill {T: float16}`.
  `topa round transition` and `topa gate` require one of each per case.
