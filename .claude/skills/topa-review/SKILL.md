---
name: topa-review
description: Review a TileOPs PR once, or on every change until it settles. Use when the user asks to review a TileOPs PR.
---

# topa review

Run `topa` from the topa virtual environment. The PR is a number in the `upstream`
repository of `topa config`. Set `GH_CONFIG_DIR` to post as another `gh` account.

To review a PR on every change until it settles, run `topa round review <PR>` instead.

## 1. Prepare

Run `topa review prepare <PR>`. It prints `dir`, `worktree`, `checklists` and
`instructions`.

## 2. Review

Follow `instructions` and write `<dir>/findings.json`.

## 3. Post

Run `topa review post <PR>`. It posts one COMMENT review on the prepared head, skips a
comment topa already posted on the same file, and removes the worktree. If it names
comments off the diff, move them onto a diff line or into the summary and rerun it.

## 4. Report

Give the PR link, the number of comments posted and the threads still open. Propose at
most three one-sentence, pass/fail checklist bullets for `src/topa/kit/review/checklists/` that would
have caught a finding earlier. Do not edit the review checklists unasked.
