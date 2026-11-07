# Ops and kernels

Apply when the diff touches `src/tileops/_ops/`, `src/tileops/_kernels/`, `src/tileops/_utils/`, `src/tileops/csrc/` or `src/tileops/backend/`. The rules are `docs/design/ops-design.md`, `docs/design/op-slot-rules.md` and `docs/code-style.md`; CI job `validate-manifest` checks each implemented op's `__init__` and `forward` against its entry.

## Performance

| Criterion | Pass | Fail |
| --- | --- | --- |
| No slower row | When the dispatch or kernel of an existing row changes: the PR gives base and head medians on one GPU, and no head median exceeds the slowest base run | a row is missing or slower |

## Selection and build

| Criterion | Pass | Fail |
| --- | --- | --- |
| Coupled values | When a value another value must agree with is added or changed: they share one name, or one is computed from the other | two literals must change together |
| Device facts | When a device property is read: it comes from the call spec (`ops-design.md` § Kernel selection) | it comes from the current device |
| Cache identity | When a cache key names a tensor by `data_ptr()` or `id()`: the cache holds that tensor | a new tensor can repeat the key |
| Dynamic dimensions | When dynamic dimensions or the entry identity change: no dynamic dimension is in the identity | each new value rebuilds or retunes |

## Ops

| Criterion | Pass | Fail |
| --- | --- | --- |
| Selection in implementations | When an op's route to its implementations changes: each `refusal` decides its calls (`ops-design.md` § Kernel selection) | op code picks an implementation by shape, dtype or architecture |
| Manifest shapes | When the tensors an op passes to a kernel change: they keep the manifest's shapes (`op-slot-rules.md` § Slot S16) | the op reshapes, pads or flattens them |
| Docstring holds | When a class docstring changes: each claim holds on every implementation of `forward` | a claim holds only for some implementation |
