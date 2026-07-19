# Reserve one governed resource envelope for dynamic Runs

## Status

Accepted (2026-07-15).

Refines
[ADR 0042](0042-governed-candidate-plan-execution-and-skill-evolution.md)
and
[ADR 0061](0061-separate-run-dispatch-from-process-local-resource-scheduling.md).

ADR 0061 remains authoritative for the Run Dispatcher, the one global
Execution Resource Scheduler, strict FIFO admission, Assignment fencing, and
process-lifetime accounting. This ADR replaces only its rule that every later
dynamic child Step submits another global scheduler ticket.

## Implementation

Not implemented. The current `ExecutionResourceScheduler` is used by Candidate
plans and the canonical Simple Skill Runtime reached by the Desktop tracer and
exact prompt-toolkit `/run <canonical-skill> --demo` Adapter. That is ordinary
per-process admission, not the aggregate parent envelope and child
suballocation selected by this ADR. Only 6 of the 95 registered Skills
currently have a complete static `resources.compute` baseline; those six are
not yet calibrated with representative datasets. Autonomous execution does not
yet reserve or suballocate a governed envelope.

## Context

ADR 0061 intentionally selected one strict-FIFO scheduler as the only global
capacity authority. A persistent Autonomous kernel, however, may itself hold a
Resource Lease while synchronously asking a child Skill to acquire another
Lease from that same scheduler.

That is a nested acquisition. If the child ticket reaches the FIFO head but
cannot fit while the parent Lease is held, the child waits for the parent and
the parent waits for the child. Strict FIFO then prevents unrelated later work
from progressing as well. Cancellation cannot safely break the cycle by
releasing capacity still used by the kernel.

Fixed Workflows and Candidate plans do not require this nesting: their
orchestrator is control logic, and each scientific process can acquire one
global Lease immediately before it starts. Dynamic execution needs a different
shape because its live kernel and future child Steps share one accepted but not
fully enumerated execution plan.

## Decision

### The Skill resource contract remains one integer process-unit request

The canonical Skill `resources.compute` contract remains exactly these five
non-negative integer dimensions:

```text
ExecutionResourceRequestV1
  cpu_cores: UInt32
  memory_mib: UInt64
  gpu_devices: UInt32
  threads: UInt32
  temporary_disk_mib: UInt64
```

One request represents one scientific process slot implicitly. Runtime budgets
also contain `max_processes`, but ordinary Skill metadata does not repeat a
`process_slots` field. Execution timeout is execution policy, not a capacity
dimension, and does not belong in this resource request.

Missing or uncalibrated declarations do not receive optimistic defaults. A
canonical Run path that requires such a Skill rejects novel admission with
`resource_contract_missing`. Legacy paths are implementation drift until they
migrate; their continued existence is not evidence that the target contract is
optional.

### A dynamic Run declares one aggregate governed envelope

An Autonomous or otherwise dynamically expanded Run declares this complete
static contract before acceptance:

```text
GovernedResourceEnvelopeV1
  aggregate:
    max_processes: UInt32
    cpu_cores: UInt32
    memory_mib: UInt64
    gpu_devices: UInt32
    threads: UInt32
    temporary_disk_mib: UInt64
  per_step_max: ExecutionResourceRequestV1
  max_parallel_steps: UInt32
  ready_step_window: UInt32
```

The aggregate is the maximum simultaneous capacity reserved for the live
kernel plus all of its child scientific processes. `per_step_max` bounds each
child, `max_parallel_steps` bounds live child concurrency, and
`ready_step_window` bounds not-yet-running local allocations. All fields enter
the Run Fingerprint and immutable Manifest header; current availability and
physical GPU identifiers do not.

The contract must prove that the kernel and every allowed child combination fit
the aggregate and that the aggregate can fit the configured global budget.
Dynamic expansion may choose less work, but it cannot expand the envelope.

### The global scheduler grants the envelope; the Run suballocates it

Before the first process of a governed Run starts, the Dispatcher acquires one
global Resource Lease for the entire aggregate envelope. Only then may the sole
Execution Assignment commit and the executor start.

Inside that Lease, one Run-local allocator suballocates process slots, CPU,
memory, the already assigned GPU identifiers, threads, and temporary disk to
the kernel and child Steps. These are **Run-local allocations**, not Resource
Leases and not another global capacity authority.

A governed executor MUST NOT submit a global resource ticket while it holds any
part of its envelope. This no-nested-global-acquisition invariant is enforced
at the executor seam and tested with a scheduler-owned Run correlation token.
Every child process receives a local allocation before spawn and releases it
only after that process is confirmed stopped.

The aggregate Resource Lease is released only when no kernel or child process
uses it. A governed Run may release the whole envelope during a dependency,
approval, or user-input wait only after every process has stopped; later work
reacquires the same immutable aggregate through the global FIFO under the same
Assignment. A merely paused live kernel still consumes its allocation and
keeps the envelope.

### Fixed plans continue to use one global Lease per process

A simple Skill obtains one global Resource Lease. A fixed Workflow or
Candidate plan submits one global request per dependency-ready scientific Step
and holds no parent Lease while doing so. Its bounded ready-Step window limits
pending tickets but does not reserve capacity.

The Run-local allocator exists only where a live dynamic parent would otherwise
perform nested acquisition. It is not a general second scheduler and cannot
lend unused envelope capacity to another Run.

## Consequences

- A dynamic Run cannot deadlock the strict-FIFO global scheduler by waiting on
  capacity already held by its own kernel.
- Global capacity remains conserved because the aggregate is reserved once and
  every child allocation is bounded inside it.
- Dynamic Runs may reserve idle capacity while a live kernel is between child
  processes. This deterministic utilization cost is accepted in v1.
- Governed envelopes require realistic aggregate calibration, not only a
  per-Step maximum.
- Fixed Workflows and Candidate plans keep finer-grained global utilization and
  do not pay the aggregate-reservation cost.
- Tests must cover nested-acquisition rejection, aggregate conservation, GPU
  suballocation, cancellation, process death, full-envelope reacquisition, and
  release only after all processes stop.

## Rejected alternatives

- **Allow reentrant tickets in the strict-FIFO global scheduler.** Rejected
  because a reentrant bypass silently creates a second fairness policy and can
  overcommit dimensions held by the parent.
- **Release the kernel Lease while it remains alive.** Rejected because
  accounting would claim capacity is free while a real process still uses it.
- **Give each child an ordinary global ticket and rely on large capacity.**
  Rejected because capacity-dependent avoidance is not a deadlock invariant.
- **Use only `per_step_max * max_parallel_steps`.** Rejected because the live
  kernel and non-multiplicative GPU/process constraints require an explicit
  aggregate.
- **Give Autonomous execution a private unaccounted pool.** Rejected because
  every scientific process must remain inside the one global capacity budget.
