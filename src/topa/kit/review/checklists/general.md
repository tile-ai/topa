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
- Name the failing criterion in the comment.

## Correctness

| Criterion | Pass | Fail |
| --- | --- | --- |
| Oracle | When an expected value is added or changed: it comes from `ref_program`, PyTorch, NumPy or a closed form | it is a literal computed elsewhere |
| Reachable | each added function, branch or schedule runs on some call of a public op, a benchmark or a script | it runs only from a test, or never |
| Tested | a test reaches each added path on an input where a fault there changes the asserted output | no test does |
| Coverage kept | When a case is removed: every path, dtype and boundary still in the code stays reached by some case | one loses its last case |

## Code

| Criterion | Pass | Fail |
| --- | --- | --- |
| One copy | When five or more statements, or a call of ten or more arguments, are added: no copy differing only in names and literals exists elsewhere | a copy is added instead of a shared helper, `@T.macro` or argument tuple; cite both |
| Single use | When an internal helper, parameter or flag is added: calls in the repository need it and together use each option it offers | no call needs it, or an option is never used; one caller alone is fine |
| Scope | When a module-level constant is added: more than one implementation uses it | it serves one implementation; move it to its use site or its class |
| Positional order | When a parameter is added to an existing function: it is last or keyword-only | it shifts an existing positional parameter |
| Reason in comments | When a comment sits beside a chosen value: it says why the value holds | it gives only a measurement, a date or history |

## PR body

- Check each claim in `## Summary` against the diff.
- Check that `## Summary` names each change to what an existing call returns, raises or times.
- Apply the `FIXME(staged-rollout)` rule of `docs/code-style.md` to each `xfail`, `skip` or loosened assertion the diff adds.
