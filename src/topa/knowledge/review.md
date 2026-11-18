# Review knowledge

## What earns an entry

A person adds an entry from the `## Review knowledge` section of a review round's
`report.md`. An entry holds:

- the fault as one pass/fail sentence, with the `When` condition that scopes it;
- each PR where an author fixed it in code, as links to the topa comment and the fixing commit;
- the existing checklist rows, TileOPs docs and CI checks that do not catch it.

Each entry carries one tag: **[1 PR]** or **[n PRs]**, the number of PRs in its evidence.

## Becoming a checklist row

An entry is ready for assessment when its evidence spans two PRs or more. A person then
checks it against the bar under `Adding a row` in `kit/review/checklists/general.md`,
writes the row with `When`, `Pass` and `Fail`, and removes the entry in the same change.

## Candidates

None yet.

## Do not flag

Findings a person confirmed as false positives, each with the PR link and why it was wrong.

None yet.
