# Ops and kernels

Apply when the diff touches `src/tileops/_ops/`, `src/tileops/_kernels/`, `src/tileops/_utils/`, `src/tileops/csrc/` or `src/tileops/backend/`. The rules are `docs/design/ops-design.md`, `docs/design/op-slot-rules.md` and `docs/code-style.md`; CI job `validate-manifest` checks each implemented op's `__init__` and `forward` against its entry. Skip a row whose `When` clause the diff does not meet.

## Performance

| Criterion | Pass | Fail |
| --- | --- | --- |
| No slower row | When the diff changes the dispatch or kernel of a manifest row that exists on base: the PR gives base and head medians on one GPU for each such row, and no head median exceeds base by more than the spread of repeated base runs | a row is missing, or one is slower by more than that spread |

## Selection and build

| Criterion | Pass | Fail |
| --- | --- | --- |
| Coupled values | When the diff adds or changes a value another value must agree with: values that must agree (a tile size and the threshold, buffer size or launch shape derived from it) are one name, or one is computed from the other | two literals must be changed together to stay correct |
| Device facts | When the diff changes code that reads a device property: selection and build read it from the call spec (`ops-design.md` § Kernel selection) | they read the current device, such as `torch.cuda.current_device()` or `get_device_properties()` without the call's device |
| Cache identity | When the diff adds or changes a cache key that names a tensor by `data_ptr()` or `id()`: the cache holds that tensor while the key lives | the cache does not hold the tensor, so a new tensor can repeat the key |
| Dynamic dimensions | When the diff changes which dimensions a program takes as dynamic, or the entry identity: each dynamic dimension is left out of the identity | the identity holds it, so each new value builds or tunes again |

## Ops

| Criterion | Pass | Fail |
| --- | --- | --- |
| Selection in implementations | When the diff changes how an op reaches its implementations: each implementation's `refusal` decides the calls it serves (`ops-design.md` § Kernel selection) | op code picks an implementation by shape, dtype or architecture |
| Manifest shapes to the kernel | When the diff changes the tensors an op passes to a kernel: they keep the shapes the manifest declares (`op-slot-rules.md` § Slot S16) | the op reshapes, pads or flattens a tensor for the kernel |
| Docstring holds | When the diff changes a class docstring: each added claim holds for that class's `forward` on every implementation that serves it | a claim holds only for some implementation, or describes a feature the class lacks |
