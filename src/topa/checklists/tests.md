# Tests

Apply when the diff touches `tests/` or `workloads/`. The rules are `docs/design/testing.md` § Tests and § Test case policy, and `docs/design/layer-boundaries.md` § Test.

1. Read `## Test node delta` in the PR body. When the worktree's Python imports `torch` and `tilelang`, rerun `python scripts/test_node_delta.py --base <base>` there, `<base>` being `base` in `pr.json`, and use its numbers instead.
2. Give each added or changed test case one verdict:

   | Verdict | When | Comment |
   | --- | --- | --- |
   | `keep` | it reaches a code path, dtype, feature or regression no other case reaches | none |
   | `shrink` | its parameters cross axes whose cells reach the same code paths | name the axis to fold; keep the boundary cells and one interior cell |
   | `delete` | another case already fails on the same fault | name the case it duplicates |

3. Ask the author for the purpose of a case you cannot give a verdict; it blocks like `shrink`.
4. Never ask to remove the last case on an input that changes the output: a tile boundary, an unaligned vector width, a size-1 dimension, or a dispatch branch.

Done when every added or changed case has a verdict and every `shrink`, `delete` and unknown case has a comment.
