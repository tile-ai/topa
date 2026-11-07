# Kernel programs

Apply when the diff touches `src/tileops/_kernels/` or `src/tileops/csrc/`. Each row is decided from the kernel program and the helpers it calls; read a helper in full when the diff calls it. Skip a row whose `When` clause the diff does not meet.

## Resources

| Criterion | Pass | Fail |
| --- | --- | --- |
| Shared-memory budget | When the diff adds or resizes a shared buffer: the budget that `refusal` or the config choice compares against counts every `T.alloc_shared` buffer the program allocates, for each architecture in `supported_archs` | the added or resized buffer is left out of the budget |
| Index width | When the diff adds or changes an offset computed from a product of dimensions the manifest leaves unbounded: it is 64-bit, or `refusal` bounds the product below 2^31 | the offset is 32-bit and a legal call overflows it |

## Concurrency

| Criterion | Pass | Fail |
| --- | --- | --- |
| Split offsets | When the diff splits K across CTAs, splits or warpgroups: each split's K offset derives from that split's index, every scale index it reads derives from that K offset, and the splits cover K once | a split reads another split's K range or scales, or a K range is covered twice or never |
| Element ownership | When the diff adds or changes an exchange (shared memory, distributed shared memory, TMA) or a store: the thread-and-fragment-to-(row, column) mapping the writer uses is the one the reader or output assumes, and each output element has one final writer | the two sides assume different layouts, two splits or threads store the same output element, or an element is never stored |
| Barrier contract | When the diff adds or changes an mbarrier or a cluster barrier: its arrive count equals the threads or CTAs that arrive, its expected transaction bytes equal the bytes delivered to it, and each wait's parity matches the phase of that use | a count or byte total differs, or a reused barrier is waited on a stale parity |
| Cluster visibility | When a CTA arrives on, writes into or reads from a peer CTA's barrier or shared memory: the peer's barrier initialization is fenced and cluster-synchronized before the first remote access, and the reader of each exchanged payload, local or remote, waits on a barrier the writer signals after its payload writes (a transaction count for async copies, a release-acquire arrive or cluster barrier for plain stores) before reading it | a remote access can reach a barrier the peer has not yet initialized, or a reader can read a payload before it is complete and visible |
| Shared-memory lifetime | When a peer CTA may access a CTA's shared memory: the CTA does not exit or overwrite that memory until a cluster barrier or wait shows the peer is done | a CTA can exit, or reuse the buffer, while a peer may still read or write it |
| Buffer reuse | When the diff reuses or aliases a shared buffer (a pipeline stage, a landing buffer, an explicit alias): every reader of the old contents is ordered before the next write by a barrier, a wait or program order of the same thread | a write can land before a reader of the old contents has finished |
| TMA descriptor | When the diff adds or changes a TMA descriptor: its box, strides and swizzle match the shared tile it fills or drains and the global tensor's layout | the box, a stride or the swizzle differs from the tile or tensor, so elements land shifted, permuted or overlapped |
| TMA store order | When the diff adds or changes a TMA store from shared memory, or the code between its source writes and its completion wait: the threads that wrote the source fence the async proxy and synchronize before the store is issued, and the issuing thread waits for the store to complete before its buffer is reused or the CTA exits | the store can read unfenced data, or the buffer is reused or the CTA exits before the store completes |
| Tails | When a tile can cross a tensor edge (a dimension the manifest does not require to be a multiple of the tile): loads and stores are masked or rely on TMA out-of-bounds fill and clipping, and the fill value leaves the result unchanged | an out-of-bounds element is read as data, written past the edge, or filled with a value that changes the result |
