---
name: topa-review
description: Review a TileOPs PR once and post the findings as one inline COMMENT review. Use when the user asks to review a TileOPs PR.
---

# topa review

Run `topa` from the topa virtual environment. The PR is a number in the `upstream`
repository of `topa config`. Set `GH_CONFIG_DIR` to post as another `gh` account.

## 1. Prepare

Run `topa review prepare <PR>`. It prints `dir`, `worktree` (a checkout of the PR head)
and `checklists`. `dir` holds `pr.json` (title, body, author, its association, base, head, changed files),
`pr.diff`, `threads.json` (unresolved threads topa started) and `comments.json`
(what others wrote).

## 2. Review

1. Read each changed file in full in `worktree`.
2. Review freely first: logic errors, edge cases, API misuse, races, leaks, broken
   invariants, error handling, performance regressions, dead code, missing tests.
3. Apply every listed checklist; skip a row whose `When` clause the diff does not meet.
   Read a TileOPs doc only when a checklist names it and the diff leaves that rule's
   outcome unclear.
4. For each thread in `threads.json`, check in the code whether the head fixes it. Do not
   raise an open thread's finding again.
5. Answer questions in `comments.json` addressed to the review in the summary.

## 3. Write `<dir>/findings.json`

```json
{"summary": "<text>", "comments": [{"path": "<file>", "line": 1, "side": "RIGHT", "body": "<text>"}]}
```

- Anchor each comment on a line of `pr.diff`: `RIGHT` for an added or context line of the
  new file, `LEFT` for a removed line of the old one.
- Write one comment per finding as `<what is wrong and why> → <what to change>`, naming
  the function, variable or pattern.
- State each comment as a claim that is true or false; hedges such as "consider" fail this.
- Put only what spans files, and which `threads.json` threads the head fixes, in the
  summary; write `No findings.` when there is nothing. Do not repeat inline comments there.
- For an external PR (`association` in `pr.json` is not `OWNER`, `MEMBER` or
  `COLLABORATOR`), follow the External PRs section of `general.md` instead: base-branch
  faults and missing tests go in the summary, and `Approve.` replaces `No findings.`.
- Do not invent a finding on a clean PR. Write in English.

## 4. Post

Run `topa review post <PR>`. It posts one COMMENT review on the prepared head, skips a
comment topa already posted on the same file, and removes the worktree. If it names
comments off the diff, move them onto a diff line or into the summary and rerun it.

## 5. Report

Give the PR link, the number of comments posted and the threads still open. Propose at
most three one-sentence, pass/fail checklist bullets for `src/topa/kit/review/checklists/` that would
have caught a finding earlier. Do not edit the review checklists unasked.
