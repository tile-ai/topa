# TileLang Notes

Facts about TileLang, ptxas and the CUDA stack that earlier rounds reproduced on a named
build. Read this before writing a production kernel.

- This file is not about the TileFoundry surface. Ask `tilefoundry` for that.
- Check behavior that is not here with a command in the round container.

## Builds

Each entry carries one tag:

- **[0.1.11]**: confirmed on the round build: TileLang `0.1.11+cu132.gitafcebed1`,
  CUDA 13.2, driver 595.71.05, H200.
- **[0.1.12]**: seen on TileLang `0.1.12` with torch `2.13.0+cu130`, in an earlier prefill
  megakernel prototype. Treat it as a hypothesis on the round build until one compile
  confirms it. Write the result in `report.md` (see the last section).
- **[0.1.12, refuted on 0.1.11]**: seen on 0.1.12, and did not reproduce on the round build.
- **[0.1.12 round]**: confirmed on the round build: TileLang `0.1.12`, torch `2.13.0+cu132`,
  CUDA 13.2, driver 595.71.05, H200.

## 1. Principle: write with the low-level forms

Why: on SM90, earlier rounds measured TileLang's high-level forms, which compile to
correct code, behind hand-written kernels such as DeepGEMM, FlashMLA and FA3.

- Write the production kernel with the low-level forms by default.
- Keep a high-level form only when a measurement shows that it is not slower. Keep that
  comparison in the round evidence.

Replace each high-level form with its low-level form:

- `T.gemm` → explicit WGMMA: `T.wgmma_gemm`, or a csrc helper that issues
  `wgmma.mma_async` with an instruction shape you choose.
- `T.copy` from global to shared memory → `T.tma_copy` with a full/empty mbarrier ring
  from `T.alloc_barrier`.
- Automatic pipelining and warp specialization in `T.Pipelined` → a hand-written
  producer/consumer in `T.ws(0)` / `T.ws(1)`, with the rings and barriers declared at
  kernel-body level.
- Automatic layout inference → an explicit `T.annotate_layout`.
- `T.alloc_fragment` used as a per-thread array → `T.alloc_local`.
- Registers left to the compiler → `T.set_max_nreg` for the producer and the consumer.

The low-level forms have limits. TileOPs lint checks them:

- Call a CUDA function through its `T.*` intrinsic when TileLang has one. Do not use
  `T.call_extern` for it (TileOPs #2507, `tilelang_idioms_lint.py`).
- When there is no intrinsic, write a helper in `namespace tileops` under
  `src/tileops/csrc/` (TileOPs #2502). Include it with `csrc_include` and call it with
  `T.call_extern("...", "tileops::<name>", ...)`. Do not put C++/CUDA source strings in
  a `.py` file.
- The instructions, buffer depths and operand layouts still come from the `tf.schedule`
  choices of the final HIR and the TIR that `finalize` writes. Do not invent them.

## 2. WGMMA issue

### Descriptors need R2UR before each issue [0.1.11]

- **Source**: TileOPs #2504. The reproducer is `DSADecodeWSKernel` in
  `src/tileops/kernels/attention/dsa/decode_ws.py` as merged by that PR.
- **Symptom**: SASS has an `R2UR` before every HGMMA/QGMMA.
- **Cause**:
  - A WGMMA descriptor must be in a uniform register (UR, one per warp).
  - When ptxas cannot prove that a value is the same across the warp, it computes the
    value in a normal register and moves it with `R2UR`. Each issue then costs extra
    instructions while the tensor core waits.
  - Neither PTX nor CUDA C++ can force a UR. The only fix is to let ptxas prove that the
    value is uniform.

Cause 1: a descriptor that lives across the loop spills to local memory.

- Consumer 0 issued the first QK pair before the loop. The descriptor stayed live through
  the whole loop, spilled (75 LDL and 75 STL per thread), and every reload needed `R2UR`.
- **Fix**: move the first issue into the loop, in a `pair == 0` branch.
- Every path through the loop must end its wgmma group the same way. If not, ptxas
  serializes every wgmma: warning C7518, then C7514, and SASS has as many DEPBAR as HGMMA.
- **Result**: 124 HGMMA, 8 DEPBAR, no LDL/STL.

Cause 2: the warp-specialized role branch tests `threadIdx.x`.

- TileLang emits `if (((int)threadIdx.x) < 128) {`. ptxas cannot prove that this is uniform
  across the warp, so the descriptors in the branch stay in normal registers.
- TileLang up to v0.1.15 (main) has no warp-uniform branch form. These DSL attempts failed:
  - A plain `if` on the result of `__shfl_sync` does not compile. TileLang then infers the
    wgmma layout for 384 threads (`warp_col_tiles ... got 21`).
  - A uniform `if` nested in `T.ws` has no effect (1.098 against 1.102 ms).
- **Fix**: rewrite the generated CUDA.
  1. Put a marker statement in the kernel:
     `T.evaluate(T.call_extern("int32", "tileops::canonical_warp_group_idx"))`.
  2. Register a postprocessor with `tilelang.register_cuda_postproc`. It replaces the
     marker with `const int warpgroup_role = tileops::canonical_warp_group_idx();` and
     replaces the three `threadIdx.x` branches, text for text, with
     `if (warpgroup_role == k) {`.
  3. `canonical_warp_group_idx()` is in `src/tileops/csrc/warp_uniform.h`. Include it with
     `csrc_include`. It returns `__shfl_sync(0xffffffff, threadIdx.x / 128, 0)`, like the
     CUTLASS function of the same name.
  4. TileLang has one CUDA postprocessor slot. Get the registered callback with
     `tvm_ffi.get_global_func("tilelang_callback_cuda_postproc", allow_missing=True)` and
     call it first from the new callback.
  5. Change only kernels that carry the marker. Return the text unchanged when there is
     not exactly one marker, or when each branch text does not occur exactly once. If
     TileLang changes its branch form, the result stays correct and only loses the
     uniform branch.
- **Result**: `R2UR` from 128 to 5, about 4% faster.

Both fixes together:

- 1.38x geometric mean against the kernel before them.
- Nsight Compute, one d576 shape: 1.39 ms to 0.893 ms, IPC 0.94 to 1.22.
- **Verify in SASS after each change**: count `R2UR`, `LDL`/`STL`, HGMMA and DEPBAR, and
  look for ptxas C75xx warnings. The time alone does not show which change worked.

### Touching the accumulator through memory serializes every WGMMA [0.1.12 round]

- Source: gqavarlenfwd-r1.
- A `"memory"` clobber or a type-punned read (`*(uint*)&acc`) of a WGMMA accumulator
  anywhere in the kernel makes ptxas serialize every WGMMA (warning C7514).
- **Fix**: store the f32 values through a csrc helper with plain stores.

### WGMMA N is split by gcd [0.1.11]

- Source: gemm-fp8-1d2d-r1, TileOPs #2451.
- `tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py:153` uses
  `inst_n = gcd(warp_col_tiles, 256)`. N=192 becomes three `QGMMA.64x64x32`.
- TileLang 0.1.15 uses `select_wgmma_inst_n` instead.
- **Fix**: a csrc helper that issues `wgmma m64n192k32`, called with `T.call_extern`.

### The descriptor offset of `T.wgmma_gemm` is not uniform [0.1.11]

- Source: gemm-fp8-1d2d-r1, TileOPs #2451.
- The generated code selects the warpgroup's 64 rows with `((threadIdx.x >> 7) + 1) & 1`.
  ptxas cannot prove that this is uniform, so every issue costs `IADD3 + MOV + R2UR`
  (3.70M `R2UR` on mlp-up).
- **Fix**: broadcast the warpgroup index from lane 0 with
  `__shfl_sync(0xffffffff, threadIdx.x >> 7, 0)` in a csrc helper such as
  `tileops::canonical_warp_group_idx()`.
- **Result**: no `R2UR`, mlp-up 4.7% faster.

### 128 threads do not keep the wgmma pipeline full [0.1.11]

- Source: kda-fwd-r1, TileOPs #2365.
- KDA chunk-prepare at 128/256/512 threads: 0.60/0.42/0.62 ms. Chunk-scan:
  0.22/0.12/0.14 ms.
- **Fix**: use 256 threads for chained gemms on 64x64 or 64x128 tiles. At 512 threads
  each thread has too few registers.

### `T.gemm` takes N from the last dimension of B, not from C [0.1.11]

- Source: kda-fwd-r1, TileOPs #2365.
- Reusing a `(BT, K)` shared buffer as `(BT, BV)` for `T.gemm` fails with
  `T.gemm N shape check failed: N_B = 128, N_C = 64`.
- **Fix**: give each width its own buffer.

## 3. Hand-written warp specialization

- **Two sequential `T.ws` regions deadlock** [0.1.12]. `ThreadSync` emits a
  `__syncthreads()` that the producer never reaches.
  - Fix: put both regions in the two arms of one `T.If`. The pass treats that as one
    statement. This form works on 0.1.11. The sequential form was not tried again there.
- **`threads=384` hangs** [0.1.12]. Two consumer warp groups make `T.gemm` need a
  block-wide sync inside a thread-dependent branch.
  - Fix: one producer and one consumer at 256 threads. Confirmed on 0.1.11.
- **The register budget inside `T.ws` is applied** [0.1.11]. The CUDA contains
  `tl::warpgroup_reg_dealloc<24>()` and `tl::warpgroup_reg_alloc<240>()`.
  - Search for those wrappers, not for `setmaxnreg`, which no kernel contains.
- **`T.Parallel` binds to the kernel's thread extent, not to the enclosing guard**
  [0.1.12]. `T.If(tx >= 128)` masks half of the threads and leaves half of the work
  uncomputed. There is no error. The symptom is `'!!!!!!'` in the generated text.
  - Fix: scope with `T.ws(i)`, not with a thread guard.
- **An allocation inside a guarded stage is not visible outside it** [0.1.12].
  - Fix: declare every ring and barrier set at kernel-body level, before the stages.
- **A wrong barrier arrive count hangs the kernel and kills the CUDA context** [0.1.12].
  The error appears at the next allocation.
  - A hang whose traceback points to an unrelated `torch.empty` is this.

### Barrier convention

Read from the CUDA that the automatic pass generates:

```text
full  barriers: init(1)    -- completed by the TMA transaction count
empty barriers: init(128)  -- all 128 consumer threads arrive
producer waits: empty[k % stages].wait(((k % (2*stages)) / stages) ^ 1)
consumer waits: full[k % stages].wait((k % (2*stages)) / stages)
```

- Confirmed [0.1.11]. `T.alloc_barrier([N] * stages)` emits `init(N)` as written.
- `N` must equal the number of threads that arrive: 1 when TileLang elects one thread for
  the TMA, 128 when the whole guarded region arrives.
- `(t // stages) % 2` for the consumer, and the same `^ 1` for the producer, generate the
  same phase.

## 4. Registers and per-thread storage

- **A 1-D `T.alloc_fragment` is not thread-private storage** [0.1.11]. Source: kda-fwd-r1.
  - `col = T.alloc_fragment([K], "float32")` followed by
    `for i in T.serial(K): col[i] = ...` writes a fragment distributed across threads,
    not K values per thread. The KDA decode prototype gave wrong output from this.
  - Fix: use `T.alloc_local([K], dtype)` for a per-thread array.
- **`int32` modulo costs four extra instructions** [0.1.11]. Source: gemm-fp8-1d2d-r1.
  - `x % 3` on `int` is generated as floormod, with four instructions to correct negative
    values.
  - Fix: make the counter `T.alloc_var("uint32")` and cast the result with
    `T.cast(..., "int32")`. A macro argument must be `int32`; otherwise the error is
    `substituting _i:int32 -> buf:uint32`.
- **Spills near 240 registers** [0.1.11]. Source: gemm-fp8-1d2d-r1.
  - A 128x192 tile (96+96 accumulators per thread) with ring counters and stage addresses
    in normal registers gave 1.0M LDL and 0.93M STL, and was 20% slower.
  - Fix: broadcast `slot` and `buf` from lane 0 in a csrc helper, such as `fp8_uniform()`
    in `fp8_1d2d_helper.h`. Then there is no spill.
- **A reduction by one thread is slow** [0.1.11]. Source: kda-fwd-r1.
  - `if tx == 0: for i in T.serial(128): ...` for two l2 norms took batch-1 decode from
    4.3 us to 9.6 us.
  - Fix: a block-level reduction with `T.alloc_fragment([1, K])` and
    `T.reduce_sum(..., dim=1)`.

## 5. Shared memory and TMA

- **A TMA destination in a small shared ring must be 128 B aligned in size** [0.1.11].
  Source: gemm-fp8-1d2d-r1.
  - A `scale_b` ring with 8 or 16 fp32 per buffer fails with
    `CUDA error: misaligned address`. 32 fp32 (128 B) is correct.
  - Fix: make each TMA destination buffer a multiple of 128 B.
- **There is no op for TMA prefetch to L2** [0.1.11]. Source: gemm-fp8-1d2d-r1.
  - Fix: issue `cp.async.bulk.prefetch.tensor` from a csrc helper, with
    `T.create_tma_descriptor`. q-b was 2.8% faster.
- **One pool for all stages is large** [0.1.12].
  - One pool cost 194 KB. One staging pair per stage packed to 98 KB at 3 stages.
  - With the automatic pass, two or more pipelined loops asked for 288 KB against a
    227 KB limit.
  - Fix: declare a pair per stage and let `MergeSharedMemoryAllocations` pack them.
- **Shared memory is not monotonic in ring depth** [0.1.12].
  - `RQ=2` needed 328704 B, `RQ=8` needed 263168 B. The depth sets the live range.
  - Fix: do not make a ring shallower to save memory. Measure both ends.
- **The allocator does not know that branches exclude each other** [0.1.12].
  - The arms of a `T.If` get separate addresses that add up (visible as `Ws4` / `Ws4_1`).
    `TL_ENABLE_AGGRESSIVE_SHARED_MEMORY_MERGE` did not fix it.
  - A kernel with two arms cannot use the depths that each arm can use alone.
- **A swizzled shared buffer cannot sit at an offset in a larger allocation** [0.1.12].
  All five forms tried fail for TMA destinations:
  - `T.Buffer(data=..., elem_offset=...)`: the alias must cover the whole parent.
  - `T.view`: no offset. On a swizzle-annotated buffer it is also a new allocation, not an
    alias [0.1.12 round, gqavarlenfwd-r1].
  - `T.reshape(pool[a:b])`: no dtype.
  - `T.match_buffer(pool[a:b])`: rank rule, then an unbound data variable.
  - `match_buffer` with `view`: the same.
  - Fix: give each ring its own buffer.
- **A leading stage axis on the allocation works** [0.1.11].
  - `T.alloc_shared([stages, block_N, dim])` with
    `T.annotate_layout({Ks: make_swizzled_layout(Ks)})` and
    `T.tma_copy(..., Ks[st, :, :], barrier=...)` compiles and is correct.
  - Use this instead of offsets into one pool.
- **`MergeSharedMemoryAllocations` is optimal on a straight-line body** [0.1.12].
  - An independent packer got the same result (231424 B from 574208 B). Hand placement
    gave 375296 B.
  - Fix: give it a straight-line body. Do not place buffers by hand.

## 6. Layout

- **An annotated fragment layout uses absolute thread numbers** [0.1.11].
  Source: gemm-fp8-1d2d-r1.
  - The Fragment thread number of `T.annotate_layout` is the absolute `threadIdx`. A layout
    derived from it adds the offset again in a restricted thread scope. The `stmatrix`
    epilogue computes the warp as `(threadIdx.x & 255) >> 5`. With the consumer on threads
    128..383 the output was all wrong.
  - Fix: put the consumer on threads 0..255 and the producer on 256..383.
- **Layout inference uses the total thread count, not the consumer subset** [0.1.12].
  - `threads=384` fails with `warp_col_tiles must be divisible by 8, got 21`.
    `threads=512` finds no layout.
  - One kernel cannot give stages different effective thread shapes. Consider this before
    you fuse stages.
- **Read a `T.reduce_*` destination at its own extent** [0.1.11].
  - Reading index 0 of a `[block_M]` fragment from a `T.Parallel` of another extent forces
    a replicated layout that conflicts:
    `Layout may conflict with ReduceOp ... replicate_size=128`.
  - Fix: run each epilogue loop over the fragment's own extent, with `if i == 0` where
    only row 0 is needed.

## 7. Compile and diagnostics

- **`@tilelang.jit` on a kernel with `T.If` or atomics** [0.1.12, refuted on 0.1.11].
  - On 0.1.12: "`<last statement>` is not a callable object".
  - On 0.1.11, `@tilelang.jit` compiled a body with two `T.ws` in one `if`/`else`, and a
    body with `T.atomic_add(return_prev=True)` and a `T.call_extern` of `__threadfence`.
  - `tilelang.compile(prim, pass_configs=...)` is still the fallback.
- **Buffer assignment works only in the traced function** [0.1.11].
  - `reg[0] += T.shfl_xor(reg[0], off)` in a plain Python helper fails with
    `TypeError: 'Buffer' object does not support item assignment`.
  - Fix: write shuffle chains and buffer changes in the traced function or in a
    `@T.macro`.
- **A barrier element passed into a `T.macro` arrives as a `Var`** [0.1.12 round].
  Source: gqavarlenfwd-r1.
  - `kfree[s]` as a macro argument is no longer a barrier.
  - Fix: pass the barrier buffer and the index, and index inside the macro.
- **`Immutable value 't6' is re-bound`** means that a stage body was emitted twice
  [0.1.12].
- **`asm` as a buffer name breaks codegen without an error** [0.1.12]. It is a C++
  keyword.
- **The compiled object reports no resource use** [0.1.12].
  - `resource_usage` is `{}`, `n_regs` and `n_spills` are `None`, and there is no
    `dynamic_smem_bytes`.
  - Get the shared memory size from a launch failure, or search the generated CUDA for
    `buf_dyn_shmem + N`.

## 8. Launch

- **CUDA graph capture fails about one run in three** [0.1.12]: "Capture must end on the
  same stream it began on". A TileLang kernel does not always launch on the stream it is
  called under.
  - Fix: `torch.cuda.graph(g, stream=side)`.
- **Any card other than the default runs 26x slower, with correct output** [0.1.12]. The
  launch stream comes from `torch.cuda.current_device()`, not from the device of the
  tensors.
  - Fix: pin the card with `CUDA_VISIBLE_DEVICES`. The round container does this already
    (it sees its card as 0). Do not change it.

## 9. Confirmed expressible

- `T.tma_copy` with mbarrier full/empty pairs: a hand-written producer/consumer ring.
- `T.ws(0)` / `T.ws(1)` scoping `T.Parallel` to a warp group.
- `T.sync_grid()` in a CUDA graph.
- A grid barrier for consumers only, from `T.macro`, `T.atomic_add(return_prev=True)`,
  `T.atomic_load` and `T.While` (validated on 132 of 132 blocks).
- Control of each pass with `tilelang.compile(pass_configs=...)`.

## Adding an entry

- A round records a new fact under `## TileLang knowledge` in its `report.md`, as its
  brief says, and does not change this file.
- A person adds the entry after reading the report, only when the fact:
  - reproduced on a named build, and carries its tag and source;
  - does not follow from TileLang's documentation or general CUDA and ptxas knowledge;
  - comes from an action a kernel author would naturally take again;
  - states its symptom and its fix;
  - is not already said by an entry here; a partial overlap extends that entry;
  - holds beyond the kernel it was seen in, without that kernel's numbers;
  - saves more debugging than it costs every round to read.
