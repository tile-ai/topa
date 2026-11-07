---
name: topa-review
description: Review a TileOPs PR and keep reviewing it as its author pushes and replies, posting each round's findings as one inline COMMENT review. Use when the user asks to review or watch a TileOPs PR.
---

# topa review

Run `topa` from the topa virtual environment; the README's setup puts it on `PATH`.
The PR is a number in the `upstream` repository of `topa config`. `gh` posts as the
account it is logged in as; set `GH_CONFIG_DIR` to review as another account.

## 1. Get the next round

Run `topa review next <PR>`. It prints JSON; act on `state`:

| `state` | Do |
| --- | --- |
| `review` | go to step 2 |
| `idle` | run `topa review next <PR> --wait 540`; after six `idle` results in a row, go to Finish |
| `merged`, `closed` | go to Finish |
| `limit` | go to Finish and say the PR reached the round limit |

## 2. Read the inputs

`dir` holds the round's inputs; `worktree` is a checkout of the PR head.

| File | Holds |
| --- | --- |
| `pr.json` | title, body, author, base and head commits, changed files |
| `pr.diff` | the whole PR diff at the head |
| `since-last.diff` | the changes since the last reviewed head; absent in round 1 |
| `threads.json` | unresolved review threads with all their comments; `topa: true` marks one topa started |
| `comments.json` | comments and review summaries others wrote since the last round |
| `checks.json` | CI checks |

Read every file listed in `checklists`. Read a TileOPs doc only when a checklist names
it and the diff makes that rule's outcome unclear.

## 3. Review

1. Read each changed file in full in `worktree`.
2. Review freely first: logic errors, edge cases, API misuse, races, leaks, broken
   invariants, error handling, performance regressions, dead code, unclear names,
   missing tests. Most findings come from this step.
3. Apply each checklist after step 2 and add what it finds.
4. From round 2, check whether the head fixes each `topa: true` thread by reading the
   code, not the reply. Do not raise the finding of an open thread again.
5. Drop a finding the author disputed twice with a reason; list it in Finish for a human.
6. Answer a question in `comments.json` that is addressed to the review in the summary.

Done when every changed file is read and every checklist item is applied.

## 4. Write the findings

Write `<dir>/findings.json`. If it exists from an earlier preparation of this round, revise it
against the refreshed inputs.

```json
{"summary": "<text>", "comments": [{"path": "<file>", "line": 1, "side": "RIGHT", "body": "<text>"}]}
```

| Field | Rule |
| --- | --- |
| `path`, `line`, `side` | a line of `pr.diff`: `RIGHT` is the new file's line of an added or context line, `LEFT` the old file's line of a removed one |
| `body` | `<what is wrong and why> → <what to change>`, naming the function, variable or pattern |
| `summary` | only what spans files; `No findings.` when there is nothing |

- Write one comment per finding.
- Give each comment a pointer: `path:line`, a field, a test name, a parametrize axis or a reference URL.
- State each comment as a claim that is true or false; "looks reasonable", "may want to verify" and "consider" fail this.
- Suggest only the fix for the finding.
- Do not repeat an inline comment in the summary.
- Do not comment on code outside the diff.
- Do not invent a finding on a clean PR.
- Write in English.

## 5. Post

Run `topa review post <PR>`. It posts one review with event `COMMENT` on the round's head
and skips a comment topa already posted on the same file with the same text. If it lists
comments off the diff, move each onto a diff line or into the summary and rerun it. Pass
`--request-changes` only when the user asks for it. Then go to step 1.

## Finish

1. Report the PR link, the rounds run, and the `topa: true` threads still open.
2. Name each finding dropped in step 3.5.
3. Propose at most three checklist bullets for `src/topa/checklists/` of this repository that would have
   caught a finding of this PR earlier: each one sentence, pass or fail without
   interpretation, with no file path or function name. Do not edit the checklists unasked.
