# {{SLUG}}: {{TITLE}}

Change [tile-ai/TileOPs](https://github.com/tile-ai/TileOPs) to meet the requirement below.
The code, the PR and any issue you open are public.

## Requirement

{{REQUIREMENT}}

- Write production code only in the worktree.
- Keep experiments, evidence and records in the round directory, `{{ROUND_DIR}}`.

| Environment | Value |
| --- | --- |
| branch | `{{BRANCH}}` |
| TileOPs base | `{{TILEOPS_BASE}}` |
| TileOPs worktree | `{{TILEOPS_WORKTREE}}` |
| TileFoundry base | `{{TILEFOUNDRY_BASE}}` |
| TileFoundry source (read-only) | `{{TILEFOUNDRY_SOURCE}}` |

## Rules

- Do not loosen the repository's tolerances or checks, skip a case, or add test-only logic.
- Do not branch on a shape.
- Add no compatibility shim.
- Migrate every caller of a changed interface in the same PR.
- Fix shared code with one rule for every op, never a list of ops or shapes.
- Fix a bug in main that the changed call paths hit in this PR, with a test. Report other
  bugs in `report.md`.
- Prove each new test by mutation in a snapshot no timing run compiles from. Why: TileLang
  re-reads kernel source at compile time.
- Follow `docs/design/` and `docs/code-style.md` of TileOPs.
- Never change the TileFoundry source or open a PR on `tile-ai/TileFoundry`.

## Where commands run

- Lines that run only `git`, `gh`, the reviewer, `topa`, file commands or text tools run on
  the host; every other line runs in the round container, at the same paths.
- Do not write `ssh` or `docker` commands.

{{KIND}}

## Cleanup

1. Inline single-caller helpers.
2. Merge repeated logic.
3. Move measured numbers, what was tried and unresolved causes out of comments and docstrings
   into the PR comments; a comment beside a chosen value keeps why the value holds.
4. Delete tests that guard no shipped behavior, and cases whose inputs and assertions another
   case already covers.
5. Pass `pre-commit run --all-files`.
6. Run the [Verification](#verification) on the candidate commit.

Exit: the verification passes.

## Review

1. Run the reviewer read-only on the worktree:

   ```bash
   cd {{TILEOPS_WORKTREE}} && {{REVIEWER}} "<prompt>" < /dev/null > {{ROUND_DIR}}/evidence/review-<n>.md 2>&1
   ```

2. Give it the diff against the current `tileops_base`, this brief, and the paths of TileOPs
   `docs/design/` and `docs/code-style.md`. Withhold your self-review.
3. Ask for correctness (edge shapes, dtypes, tails), the real call path, branch coverage,
   whether the diff meets the requirement and adds nothing it does not ask for, and hotfixes
   (a loosened tolerance or check, a skipped case, test-only logic, a shape branch), and
   bloat (a comment carrying a measured number, what was tried or an unresolved cause, a
   single-use helper, repeated logic, a case whose inputs and assertions another case covers).
4. Fix each finding you agree with.
5. Answer each other finding with a test, a measurement or a rule.
6. Rerun the reviewer on the revised diff. Rerun a review that ends in an error.

Exit: no blocking or major finding is left. A blocking or major finding still disputed
after two reviews ends the round: follow [Ending without a PR](#ending-without-a-pr).

## PR

1. Rebase onto the newest `{{UPSTREAM}}` main.
2. Set `tileops_base` in `provenance.json` to the new base.
3. Title the commit as [PR files](#pr-files) says, with no authorship trailer.
4. Run cleanup steps 5-6.
5. Run the review on the final diff and evidence.
6. Set `candidate_commit` in `provenance.json` to HEAD.
7. Fill in the files [PR files](#pr-files) lists, where they are. {{LINK}}
8. Replace every `<...>`, and add no `## ` section to the body.
9. Leave out of the PR body and comments: local paths, raw logs, and the order things
   were tried.
10. Run `{{TOPA}} pr`.
11. Re-read the evidence and the diff.
12. Run `{{TOPA}} pr --reviewed`. Never use `gh pr create`, `gh pr edit` or merge.
13. Report the PR link.
14. Fix each CI failure and each review comment you agree with in this session.
15. Answer each other review comment with a test, a measurement or a rule.
16. After a fix or a base move, repeat steps 1-12.

Exit: the PR is open, CI passes, and every review comment is fixed or answered.

## Ending without a PR

1. Write `## Stop records` in `report.md`: what the requirement still lacks, and the
   evidence that the round cannot supply it.
2. When a disputed finding or a missing fact stopped the round, write the decision a person
   must make and the evidence on each side.
3. Close any PR this round opened, with a comment saying why.
4. Report the stop, and the decision of step 2 when there is one.

## Records

`{{TOPA}} gate` checks the structure of these records. Paths are relative to the round
directory. Each `command` is the exact command that wrote its non-empty `report`.

```json
{
  "tileops_base": "<40-hex>", "candidate_commit": "<40-hex HEAD>",
  "tests": {"command": "<pytest>", "report": "<r>"},
  "reviews": ["evidence/review-1.md"]
}
```

## Report

Write `report.md`. Under `## TileLang knowledge`, record each TileLang or ptxas fact the
next round would otherwise rediscover: what you did, the result, the build. Do not edit
`{{KNOWLEDGE}}`.
