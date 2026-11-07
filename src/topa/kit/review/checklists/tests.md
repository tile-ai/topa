# Tests

Apply when the diff touches `tests/`, `benchmarks/tests/` or `workloads/`. The rules are `docs/design/testing.md` § Tests and § Test case policy, and `docs/design/layer-boundaries.md` § Test and § Workloads layer. Skip a row whose `When` clause the diff does not meet.

## Cases

| Criterion | Pass | Fail |
| --- | --- | --- |
| Case verdict | When the diff adds or changes a test case: it reaches a code path, dtype, boundary or regression no other case reaches (`keep`) | its parameters cross axes whose cells reach the same paths and boundaries (`shrink`: name the axis to fold, keep the boundary cells and one interior cell), or another case already fails on the same fault (`delete`: name that case) |
| One behavior per function | When the diff adds or changes a test function: every parameter cell runs the same assertions | an `if` on a parameter value adds or skips assertions for some cells (`testing.md` § Growth) |
| Smoke size | When the diff adds a `smoke` case: it uses a dtype the op's smoke cases do not yet cover, or a shape they already use for it | it adds a second smoke shape for a dtype of the op, in any test function (`testing.md` § Test case policy, Dtype) |
| Architecture mark | When the diff adds or moves an architecture mark, or changes dispatch for a marked case: it sits on a case whose path dispatch selects only on that architecture | the marked path is also selected on another architecture the suite runs on (`testing.md` § Tests, Reachability) |
| Special values | When the diff adds or documents a special-value behavior (NaN, `inf`, `-inf`, an empty or fully masked row, a sentinel such as `-1`): a case passes that value | the value appears in no case |

- Count a boundary as its own path: a tile or scale-group boundary, a tail, an unaligned vector width, a size-1 dimension, an accumulation length, a dispatch branch. Never ask to remove the last case on one.
- Write one comment per test function with a `shrink` or `delete` verdict; write nothing for `keep` or for a case whose purpose is unclear.
- Give an issue's acceptance criterion no weight: a case it asks for gets the same verdict as any other.

## Workloads

| Criterion | Pass | Fail |
| --- | --- | --- |
| Measured bound | When the diff loosens a bound: it names the measured error it covers, and applies only to rows or dtypes whose measured error exceeds the old bound | the bound is loosened without a measurement, or also for rows whose measured error fits the old bound |
| Rejecting verification | When the diff changes a `verification()`: a test or the PR shows it rejects an output the op must not return | no wrong output is shown to fail the changed verification |
