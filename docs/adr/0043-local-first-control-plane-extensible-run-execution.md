# Local-first single-process control plane with an extensible Run execution plane

## Status

Superseded by
[ADR 0044](0044-single-owner-control-plane-and-owner-only-channel-ingress.md)
(2026-07-14).

ADR 0044 retains the single-process control plane and extensible Run execution
plane, but retracts this ADR's multi-user deployment scope.

**Identity terminology clarification (2026-07-14):** Bare "Session" is not a
domain concept. References below to Sessions mean Conversations; existing
`Session` models and `session_id` fields are legacy implementation names. A
future account-login state must be called Authentication Session in full.

**Run-scope refinement (2026-07-14):**
[ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md)
requires the control plane to validate and freeze one
`ProjectScope(project_id)` or `UnassignedScope` before submitting a Run Request
through this ADR's extensible execution seam. Workers execute that resolved
scope and never derive Project identity from `default`, Session, Chat or paths.

**Run-identity and lifecycle refinement (2026-07-14):**
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md)
requires the control plane to generate an opaque Run ID and commit a minimal
queued Run Receipt before handing work through this execution seam. Workers use
that identity and report evidence; local process, remote Job, Worker and
scheduler identifiers remain replaceable Execution References.

**Run-submission and assignment refinement (2026-07-14):**
[ADR 0058](0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md)
requires a durable Run Submission Binding before novel acceptance and permits
exactly one process-bound, Assignment-ID-fenced executor start grant per Run.
This execution seam is not a renewable lease or automatic replay/reassignment
protocol.

## Implementation

Partially implemented. Surface ingress, chat dispatch, typed Events, and durable
control state currently live in one backend process. Skills already execute in
local subprocesses or through remote execution paths, but one replaceable Run
Executor Seam has not yet been established. No cross-process chat queue is
implemented or authorized by this decision.

## Context

ADR 0006 rejected a CellClaw-style Redis queue, separate chat Worker, and
cross-process EventBus when OmicsClaw was described as a single-user local
research tool. ADR 0040 later made multi-user isolation and restart resilience a
backend requirement while still deferring cross-process sharing. That left the
deployment model ambiguous: multi-user durability does not by itself require a
distributed control plane, while OmicsClaw's expensive work is primarily
environment- and data-local Skill execution.

## Decision

OmicsClaw v1 is a **local-first modular monolith with a single-process control
plane and an extensible Run execution plane**.

The control plane owns Surface ingress, identity and isolation, Session and
Project resolution, chat context, in-process dispatch, the typed Event stream,
policy, and durable control state. One backend process may serve multiple
logically isolated users, Sessions, and Projects, and must recover committed
state after restart.

Skill, Workflow, and autonomous-analysis Runs belong to the execution plane.
They may execute through a local subprocess or an existing remote execution
path. Their invocation should converge behind one replaceable Run Executor
Seam, so a future Worker implementation can be introduced without replacing
the chat control plane.

OmicsClaw v1 does not introduce a persistent cross-process queue for every chat
turn, a separate chat Worker, a cross-process EventBus, or horizontal backend
scaling. Multi-process execution will be reconsidered only when measured or
product requirements demand at least one of: multiple backend replicas,
survival of in-flight agent turns across process failure, global fair
scheduling and backpressure, independently deployed compute Workers, or a
demonstrated single-process control-plane bottleneck.

## Consequences

- Local data paths, Conda/R/GPU environments, streaming Events, cancellation,
  and Workspace ownership remain simple in the common local-first deployment.
- Multi-user isolation and restart resilience are requirements inside the
  single-process ownership model; they are not claims of distributed safety.
- Heavy Runs can gain process or machine isolation independently of chat-turn
  orchestration.
- A future queue is an execution-plane Implementation behind the Run Executor
  Seam, not a reason to route every Surface through distributed infrastructure.
- ADR 0006's in-process dispatch decision remains current, but its single-user
  premise is superseded by this ADR. ADR 0040's restart-resilience decision is
  constrained to this single-process control-plane model until a later ADR
  explicitly adds cross-process ownership.

## Forward refinement (2026-07-14)

[ADR 0061](0061-separate-run-dispatch-from-process-local-resource-scheduling.md)
defines the v1 execution-plane scheduling boundary inside this deployment
model: one bounded process-local Run Dispatcher owns FIFO and Assignment
eligibility, while one independent process-local Execution Resource Scheduler
owns multidimensional compute admission. Neither is a persistent cross-process
queue or Worker protocol.
