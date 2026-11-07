# Every PR

Apply to every TileOPs PR. Rules TileOPs documents or checks in CI are cited, not restated.

## Where the rules live

| Rule | Source |
| --- | --- |
| the design doc each area is reviewed against | `CONTRIBUTING.md` § Design first |
| title, branch and issue names | `CONTRIBUTING.md` § Naming, CI job `validate-pr-title` |
| code rules a reader applies | `docs/code-style.md` |
| public API compatibility | `docs/stability.md` § Compatibility, `scripts/ci/check_api_compat.py` in CI job `compile-contract-gate` |
| lint, format, secrets, manifest validation | `.pre-commit-config.yaml` and the `Preflight Checks` workflow |

- Do not comment on what a CI check of the PR reports.
- Cite the source row when a finding breaks a rule from this table.
- Skip a row whose Pass cell starts with `When` unless the diff does what that clause names.
- Name the failing criterion in the comment.

## Correctness

| Criterion | Pass | Fail |
| --- | --- | --- |
| Oracle | When the diff adds or changes an expected value: it comes from the workload's `ref_program`, PyTorch, NumPy or a closed form | a literal computed outside the test is the expected value |
| Dispatch and coverage | each path the diff adds outside `tests/` and `benchmarks/tests/` (function, branch, implementation, schedule) runs on some call of a public op, a benchmark or a script (an implementation or schedule: selected by dispatch on some manifest workload row on an architecture it supports), and some test reaches it on an input where a fault in that path changes the asserted output; every path still present stays reached by some case after the cases the diff removes | a path runs only when a test calls it directly, or never; no row selects an added implementation or schedule; no test reaches the path; or a removed case leaves a remaining path, dtype or boundary reached by no case |

## Code

| Criterion | Pass | Fail |
| --- | --- | --- |
| One copy | When the diff adds a run of five or more statements: no copy elsewhere in the repository differs from it only in names and literals | the diff adds such a copy instead of a shared helper or `@T.macro`; cite both copies |
| Positional order | When the diff adds a parameter to an existing function: it goes after the existing positional parameters, or is keyword-only | it shifts an existing positional parameter |
| Reason in comments | When the diff adds a comment beside a chosen value (a tile size, a threshold, a stage count): it states why the value holds | it gives only a measurement, a date or the history of the choice; a value without a comment is not a failure |

## PR body

- Check each claim in `## Summary` against the diff, and that it names each change to what an existing call returns or raises, or to the call or timing of an existing benchmark row.
- Apply the `FIXME(staged-rollout)` rule of `docs/code-style.md` to each `xfail`, `skip` or loosened assertion the diff adds.
