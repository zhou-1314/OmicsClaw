# ADR 0042: Governed candidate-plan execution and skill evolution

- Status: Accepted
- Date: 2026-07-14

## Context

RET-05 introduced a generated compatibility graph and digest-bound confirmation,
but confirmation only allowed ordinary skills to be called one at a time. That
did not preserve whole-plan topology, artifact handoff, failure cascades, or
one-shot authority. The graph also only understood AnnData facts. Separately,
skill runs exposed stderr and exit codes but did not produce typed, privacy-safe
health evidence or a human-gated writeback path.

The compatibility graph is candidate evidence, not execution authority. Skill
evolution may propose changes from real evidence, but must never autonomously
change scientific contracts or narrative guidance.

## Decision

1. Keep graph compilation and plan execution as separate Modules:
   `skill_dag.py` answers “what may connect”; `plan_executor.py` answers “how
   this exact confirmed plan runs”.
2. Represent non-AnnData handoffs as exact semantic artifact contracts
   (`kind + format + relative output path`). Generated edges remain candidates.
   The review overlay records explicit `accepted|rejected` decisions; only
   accepted reviewed dependencies may reach the executor.
3. Bind execution to the canonical SHA-256 of the complete plan. The runtime
   exposes one dedicated `candidate_plan_execute` action, blocks ordinary skill
   and autonomous bypasses while a plan gate exists, and consumes confirmation
   before the first await. One confirmation authorizes one attempt.
4. Execute topological phases concurrently. Propagate only declared artifacts,
   mark missing artifacts as contract failures, let independent siblings finish,
   and skip descendants of failed steps. Unresolved plans fail closed unless a
   non-runtime caller explicitly selects the `independent` strategy. A missing
   declared artifact also produces a `candidate-plan-contract` health event and
   replaces the step's script-level success result with a contract-failure result.
5. Give every `SkillRunResult` a typed `error_kind` without changing its pinned
   legacy dictionary. Append privacy-minimal run events keyed by skill id,
   version/hash, and environment identity; raw stderr, input paths, and secrets
   are represented only by fingerprints and safe structural references.
6. Automated evolution can only create pending proposals with supporting and
   counterexample event ids. Writeback requires a named human approver and all
   three—and only those three—validator classes: representation, execution, and
   retrieval. JSONL writes and proposal state transitions use cross-process file
   locks. Any validator failure restores the exact previous bytes and records a
   rollback.
7. Represent conditional success contracts with `outputs.method_scopes`.
   Scope methods must be canonical `interface.parameters.hints` keys; scoped
   paths remain members of the global output inventory, while scoped AnnData
   fields and semantic artifacts are additional guarantees for those methods.
   The compiler derives `condition_scope.source_methods`; review may accept or
   reject that edge but cannot change the condition. Candidate plans include
   method bindings in their digest, and the executor independently checks each
   conditional edge before forwarding `--method` to the shared runner.
8. Bound phase parallelism (default four active skill runners) with an async
   semaphore. Task cancellation is never classified as a script defect: it
   propagates through the phase gather into `arun_skill`, whose subprocess
   driver terminates and reaps the complete process group.
9. Extend the shared Input Profile with opt-in Content precondition contracts.
   CSV/TSV and compressed variants read only the header; VCF reads bounded
   metadata through `#CHROM`; FASTQ validates the first record and R1/R2 mate
   layout; directory traversal is depth/entry bounded and emits only governed
   Directory signatures. A truncated directory probe cannot prove absence and
   therefore requests preparation instead of hard-blocking. Manifests without
   a matching content declaration preserve their existing execution behavior.
10. Treat `resources.compute` as a strict static admission reservation. A
    Candidate plan carries one complete request per skill in its canonical
    digest; missing or self-reported-unready requests fail closed before output
    allocation. A runtime/event-loop-shared scheduler atomically reserves CPU,
    memory, GPU count, thread count, temporary disk, and a process slot in FIFO
    order. Physical GPU ids and the host/operator budget remain runtime state.
    The runner Adapter accepts only governed GPU/thread/TMPDIR environment
    values, and every lease is released after success, failure, or cancellation.
    This is admission accounting, not cgroup enforcement or a peak-memory
    predictor.

The runtime budget Interface accepts these operator overrides:
`OMICSCLAW_PLAN_CPU_CORES`, `OMICSCLAW_PLAN_MEMORY_MIB`,
`OMICSCLAW_PLAN_GPU_DEVICE_IDS`, `OMICSCLAW_PLAN_THREADS`,
`OMICSCLAW_PLAN_TEMPORARY_DISK_MIB`, and
`OMICSCLAW_PLAN_MAX_PROCESSES`. Without overrides, CPU capacity honors the
process affinity mask before falling back to host CPU count; memory and output
filesystem temporary space use 80% of currently available capacity; GPU ids
come only from the explicit override or `CUDA_VISIBLE_DEVICES`; and active
processes default to `min(4, cpu_cores)`.

## Consequences

- A confirmed plan can no longer silently degrade into unrelated individual
  calls or be replayed from stale confirmation state. After confirmation,
  ordinary skill/autonomous calls and mismatched plan digests are denied rather
  than presented as separately approvable actions.
- Artifact compatibility now covers explicit table/VCF workflows in genomics,
  proteomics, metabolomics, and Bulk RNA, while literature/orchestrator remain
  terminal artifact producers rather than receiving invented scientific edges.
- Health aggregation distinguishes skill defects from dependency/resource
  failures and user cancellation. It supplies an evolution substrate, not an
  autonomous scientific editor.
- Two real manifests now distinguish method guarantees (`sc-velocity`
  dynamical latent time and `spatial-velocity` VELOVI latent time). The current
  library has no truthful consumer requiring those exact guarantees, so the
  production graph intentionally reports zero conditional edges rather than
  inventing one.
- Six real skills now consume bounded non-H5AD facts: `bulkrna-de`,
  `genomics-vcf-operations`, `sc-fastq-qc`, `sc-count`, `sc-velocity-prep`, and
  `spatial-raw-processing`. Approval UI/CLI, proposal-specific patch synthesis,
  and execution net-utility scoring remain future work.
- Six real skills now declare initial compute-reservation baselines:
  `sc-preprocessing`, `sc-clustering`, `sc-count`,
  `genomics-vcf-operations`, `spatial-preprocess`, and `spatial-domains`.
  Skills without a complete declaration intentionally produce resource-unready
  plans rather than inheriting optimistic defaults. These initial values have
  not yet been measured across representative dataset sizes; host reservations
  do not replace OS/container isolation, data-size-aware estimation, or a
  distributed/remote scheduler.

## Forward refinement (2026-07-14)

[ADR 0061](0061-separate-run-dispatch-from-process-local-resource-scheduling.md)
generalizes this Candidate-plan-only Resource Scheduler into the one
process-local capacity authority for every scientific Run path. Candidate
`max_concurrency` becomes a bounded per-Run ready-Step window rather than a
second global process limit; the original complete resource declarations,
digest binding and fail-closed semantics remain current.
