# Every PR

Apply to every TileOPs PR. Rules TileOPs documents or checks in CI are cited, not restated.

## Where the rules live

| Rule | Source |
| --- | --- |
| the design doc each area is reviewed against | `CONTRIBUTING.md` § Design first |
| title, branch and issue names | `CONTRIBUTING.md` § Naming, CI job `validate-pr-title` |
| PR body sections | `.github/PULL_REQUEST_TEMPLATE.md` |
| code rules a reader applies | `docs/code-style.md` |
| benchmark files | `docs/design/testing.md` § Benchmarks |
| lint, format, secrets, manifest validation | `.pre-commit-config.yaml` and the `Preflight Checks` workflow |

- Do not comment on what a CI check in `checks.json` reports.
- Cite the source row when a finding breaks a rule from this table.

## Correctness

| Criterion | Pass | Fail |
| --- | --- | --- |
| Oracle | the expected value comes from the workload's `ref_program`, PyTorch, NumPy or a closed form | a literal computed outside the test is the expected value |
| Coverage set | the dtypes, shapes and modes the tests reach stay the same or grow | a case is removed while the code path it reaches stays |
| New paths | each code path the diff adds is reached by a test whose input makes its output differ from every other path | an added path has no such test |

- Name the failing criterion in the comment.
- A path no input can tell apart from another passes `New paths` without a test.

## PR body

- Check that the body has each section `.github/PULL_REQUEST_TEMPLATE.md` requires for what the diff touches.
- Check each claim in `## Summary` against the diff.
- Apply the `FIXME(staged-rollout)` rule of `docs/code-style.md` to each `xfail`, `skip` or loosened assertion the diff adds.
