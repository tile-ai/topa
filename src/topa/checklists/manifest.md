# Manifest

Apply when the diff touches `src/tileops/manifest/`. The rules are `docs/design/manifest.md`; CI job `validate-manifest` checks each entry's structure and each `implemented` entry against its class.

- Check each changed contract field (signature, dtypes, shape rules, workloads, roofline) against the reference the entry follows, as `manifest.md` § Layer Boundaries requires.
- On a change to `status: implemented`, grep `tests/` and `benchmarks/` for the op and flag each `skip`, `xfail` or loosened assertion that hides a non-conformance with the entry; a device or capability exclusion that `docs/design/testing.md` § Tests allows is not one.
- On a change to `status: spec-only`, check that the PR names the non-conformance `manifest.md` § Layer Boundaries requires for it.
