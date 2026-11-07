# Kernel programs

Apply when the diff touches `src/tileops/_kernels/` or `src/tileops/csrc/`. Read each helper the diff calls.

| Criterion | Pass | Fail |
| --- | --- | --- |
| Shared-memory budget | When a shared buffer is added or resized: the budget `refusal` or the config choice checks counts it | the buffer is left out of the budget |
| Index width | When an offset multiplies unbounded dimensions: it is 64-bit, or `refusal` bounds it below 2^31 | a legal call overflows a 32-bit offset |
| Split offsets | When K is split: each split reads its own K range and scales, and the splits cover K once | a K range or its scales are read by the wrong split, twice or never |
| Element ownership | When data is exchanged or stored: writer and reader use the same thread-to-element mapping, and each output element has one writer | the mappings differ, or an element is stored twice or never |
| Barrier contract | When a barrier is added or changed: arrive count, expected bytes and wait parity match its uses | a count, byte total or parity is off |
| Cluster access | When a CTA touches a peer's shared memory or barrier: the peer's barrier is initialized and visible first, the reader waits for the writer's signal, and no CTA exits while a peer may still access its memory | an access can precede init or the payload, or outlive the peer |
| Buffer reuse | When a shared buffer is reused: every reader of the old contents finishes before the next write | a write can overtake a reader |
| TMA | When a TMA copy is added or changed: box, strides and swizzle match the tile and tensor; a store's source is fenced before issue and completed before reuse or exit | a mismatch, an unfenced source, or a buffer reused before the store completes |
| Tails | When a tile can cross a tensor edge: out-of-bounds elements are masked or filled with a value that leaves the result unchanged | an out-of-bounds element is read as data or written |
