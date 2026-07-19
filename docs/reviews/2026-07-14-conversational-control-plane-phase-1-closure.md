# Conversational control-plane Phase 1 closure audit

Date: 2026-07-14

## Outcome

Phase 1 architecture decision work is complete. Every blocking question in the
agreed plan has one accepted v1 answer, consistent canonical vocabulary and an
explicit implementation-drift record.

This is **decision closure, not implementation completion**. Most of the target
control plane remains unimplemented; Phase 2 must turn the accepted boundaries
into one evolvable design before Phase 3 code migration begins.

**Phase 2 update (2026-07-14):** the required implementation design is now
complete in
[`docs/design/conversational-control-plane.md`](../design/conversational-control-plane.md).
Its closure audit is recorded in
[`2026-07-14-conversational-control-plane-phase-2-closure.md`](2026-07-14-conversational-control-plane-phase-2-closure.md).

## Original blocking goals

| Goal | Accepted resolution | Evidence | Implementation state |
|---|---|---|---|
| Project and control-state authority | Control Plane State alone owns Project, Conversation, Active Conversation Binding, Turn Receipt and ingress idempotency. Project knowledge, Transcript, UI data and filesystem metadata remain specialized content/projections. | [ADR 0053](../adr/0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md) | Not implemented; authority remains fragmented. |
| Physical control-plane storage and legacy migration | One Backend-exclusive local SQLite `control.db`, opened only through the control-state Repository under a lifetime process lock; explicit auditable idempotent import, then no runtime fallback. | [ADR 0054](../adr/0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md) | Not implemented; database, Repository and migration tooling are missing. |
| Attachment ownership, staging, deletion and duplicate semantics | Specialized Attachment Store owns immutable per-Turn Records and content-addressed Blobs; duplicate lookup precedes all-or-nothing staging; publish-before-control reconciliation handles crash windows; no ordinary accepted-attachment delete in v1. | [ADR 0059](../adr/0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md) | Not implemented; current Surfaces still use incompatible paths, Base64/provider objects and mutable Session file registries. |
| Outbound failure and retry | One terminal Channel Outbound Delivery is inserted with Turn terminalization; a bounded persistent Outbox and in-process Delivery Pump retry only safe provider delivery, never Turn or Run execution; ambiguous acceptance becomes `unknown`. | [ADR 0060](../adr/0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md) | Not implemented; direct SDK sends and `pending_media` remain. |
| Default Project, Run reassignment and Project deletion | Project lifecycle is reversible `active | archived`; permanent purge is outside v1. Every Run freezes `ProjectScope(project_id) | UnassignedScope`; `default/` is only Unassigned storage, never a Project, and completed Runs cannot be moved or retagged. | [ADR 0055](../adr/0055-model-project-lifecycle-as-reversible-archive-and-restore.md), [ADR 0056](../adr/0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md) | Not implemented; legacy `is_deleted`, `project_id="default"` and path-derived scope remain. |

## Enabling decisions closed during the audit

The original five goals depend on a larger coherent identity and scheduling
model. Phase 1 therefore also closed these boundaries:

- single Backend serves one Owner; Channel accepts only configured Owner
  identities ([ADR 0044](../adr/0044-single-owner-control-plane-and-owner-only-channel-ingress.md));
- Owner Identity is admission/source evidence, never a tenant or storage
  partition ([ADR 0045](../adr/0045-owner-identity-is-not-a-state-partition.md));
- every chat Surface crosses one Raw Inbound → Ingress Normalizer → Inbound
  Envelope boundary and receives fresh live Dispatch Context
  ([ADR 0046](../adr/0046-normalize-all-conversational-ingress-before-dispatch.md),
  [ADR 0047](../adr/0047-separate-inbound-envelope-from-dispatch-context.md));
- Conversation has an opaque control-generated ID, immutable Surface/Reply
  Target address, at most one immutable Project binding, and a durable active
  binding per address; cross-Surface continuity belongs to Project
  ([ADR 0048](../adr/0048-resolve-conversation-with-active-reply-target-binding.md),
  [ADR 0049](../adr/0049-immutable-conversation-address-ephemeral-response-sink.md));
- whole Turns serialize through a bounded FIFO per Conversation, receive opaque
  Turn IDs and non-replayable Receipts, and bind transport retries to the same
  Turn while SSE merely re-observes it
  ([ADR 0050](../adr/0050-serialize-turns-per-conversation-with-bounded-fifo.md),
  [ADR 0051](../adr/0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md),
  [ADR 0052](../adr/0052-bind-retried-ingress-to-one-turn-and-resume-observation.md));
- every top-level scientific execution has one opaque Run ID, minimal Receipt,
  idempotent Submission Binding and at most one Assignment-ID-fenced start;
  restart never automatically replays it
  ([ADR 0057](../adr/0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md),
  [ADR 0058](../adr/0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md));
- one bounded process-local Run Dispatcher owns FIFO and Assignment eligibility,
  while one independent Execution Resource Scheduler owns all scientific
  process capacity; first-unit capacity precedes Assignment
  ([ADR 0061](../adr/0061-separate-run-dispatch-from-process-local-resource-scheduling.md)).

## Consistency checks

- ADRs 0044 through 0061 all have explicit accepted status; ADR 0043 is
  explicitly superseded by ADR 0044 for its multi-user scope.
- Canonical terms and relationships are synchronized in
  [`docs/CONTEXT.md`](../CONTEXT.md), with legacy ambiguities preserved as
  resolved tombstones rather than silently reused.
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) describes the accepted target and
  separately lists implementation drift. Public README and architecture
  projections no longer describe Channel as a multi-user product.
- Relative links from the new/refined ADR set resolve, `git diff --check`
  passes, and `tests/test_documentation_facts.py` passes.

## Explicitly non-blocking future questions

These do not reopen Phase 1:

- whether dataset Memory belongs to Project, Workspace or an explicit
  Owner-wide catalog;
- permanent cross-store Project data purge;
- an independently deployed cross-process Worker or durable scheduler protocol;
- a measured future replacement for strict-FIFO head-of-line blocking;
- durable recovery of non-terminal approval/input workflows.

Each requires separate evidence and, if hard to reverse, a future ADR. None is
implicitly implemented by Phase 2 schema design.

## Required Phase 2 artifact — completed

Phase 2 created
[`docs/design/conversational-control-plane.md`](../design/conversational-control-plane.md)
as one evolvable implementation design beneath these ADRs. It defines concrete
schemas and invariants for:

1. Raw Inbound, Inbound Envelope and Dispatch Context;
2. Project, Conversation, Turn, Run, idempotency, Assignment, Attachment and
   Outbound Delivery repositories;
3. atomic acceptance order and crash-point reconciliation;
4. Turn Sequencer, Run Dispatcher, Execution Resource Scheduler, cancellation,
   approval and backpressure interfaces;
5. Desktop submission/SSE observation and Channel Reply Target contracts;
6. explicit legacy import and cutover from Session, Transcript, Project,
   Remote Job and Channel caches.

Detailed fields may evolve in that design document without creating another
ADR unless they change an accepted ownership, identity, durability or
deployment boundary.
