# Docs

Apply when the diff touches `docs/` or a Markdown file.

- Flag a statement that contradicts another doc, the manifest or the code; cite both sides as `path:line`.
- Flag a statement of a target the code does not meet unless the PR changes the code or links a follow-up issue.
- In `docs/design/`, flag content that is not a convention, a module boundary or a contract: file lists, implementation snapshots, history.
- In `docs/design/`, flag a passage that constrains no decision: an example, a rationale or a narration that changes no rule.
- Flag a rule the diff states in a second place; cite the first.
