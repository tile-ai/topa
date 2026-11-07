# Tests

Apply when the diff touches `tests/`, `benchmarks/tests/` or `workloads/`. The rules are `docs/design/testing.md` § Tests and § Test case policy, and `docs/design/layer-boundaries.md` § Test and § Workloads layer.

## Cases

| Criterion | Pass | Fail |
| --- | --- | --- |
| Case verdict | When a case is added or changed: it reaches a path, dtype, boundary or regression no other case reaches | its cells reach the same paths, dtypes and boundaries (`shrink`: name the axis), or another case catches the same fault (`delete`: name it) |
| One behavior per function | When a test function is added or changed: every cell runs the same assertions | an `if` on a parameter changes the assertions (`testing.md` § Growth) |
| Smoke size | When a `smoke` case is added: it adds a new dtype, or reuses a smoke shape | it adds a second smoke shape for a dtype (`testing.md` § Test case policy) |
| Architecture mark | When an architecture mark is added or moved: dispatch selects that path only on that architecture | another architecture also selects it (`testing.md` § Tests) |
| Refusal gap | When the architectures or inputs an implementation serves change: each input in the signature that every implementation now refuses has one test asserting the refusal | no test asserts it (`testing.md` § Tests) |
| Special values | When a special value (NaN, `inf`, an empty row, a sentinel) is handled or documented: a case passes it | no case does |

- A boundary counts as a path: a tile or scale-group edge, a tail, an unaligned width, a size-1 dimension, a dispatch branch. Never ask to remove its last case.
- Comment only on `shrink` and `delete`, once per test function.
- An issue's acceptance criterion gets no extra weight.

## Workloads

| Criterion | Pass | Fail |
| --- | --- | --- |
| Measured bound | When a bound is loosened: it names the measured error, for the rows that need it only | no measurement, or rows that fit the old bound are loosened too |
| Rejecting verification | When `verification()` changes: a test or the PR shows it rejects a wrong output | no wrong output is shown to fail |
