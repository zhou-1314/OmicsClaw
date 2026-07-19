# Reconcile interrupted Skill evolution approvals

## Status

Accepted and implemented for EVO-R1 (2026-07-15).

Refines [ADR 0066](0066-govern-earned-skill-validation-promotion-in-the-backend.md).

## Context

EVO-G1 restores the manifest and generated projections when an ordinary Python
exception occurs, but a process termination can bypass exception handlers. A
termination after the guarded manifest commit can therefore leave a pending
proposal beside promoted manifest bytes and partial catalog/DAG projections.
Likewise, a persistent proposal-store failure can restore files but fail to
record the durable `rolled_back` state.

Inferring approval from promoted bytes would bypass the human decision. Blindly
restoring projection snapshots can erase unrelated later source changes. The
Backend needs enough durable intent to return to a known state without moving
policy or file mutation into OmicsClaw-App.

## Decision

### Persist one Backend-owned approval intent before manifest commit

After representation and execution validation, but before the guarded manifest
CAS, the Backend writes one mode-`0600` inflight journal beside the proposal
store. It contains only:

- schema version and proposal/Skill identity;
- Skills-root-relative manifest and guarded-swap witness paths;
- exact before/after manifest bytes plus SHA-256 hashes and original mode; and
- the asserted approver and review reason.

The journal is written through a same-directory temporary file, file `fsync`,
atomic replace, and POSIX directory `fsync`. Manifest and generated-projection
publishes use the same durability helper. Proposal JSONL creation also fsyncs
its parent directory.

Only one journal may exist because proposal decisions already share the
proposal-store exclusive lock. A new approval fails closed while an inflight
journal exists. Approval and rejection perform a fast read for normal requests
and repeat the journal guard while holding that proposal lock. A queued decision
that observed an earlier empty state therefore cannot overtake an interrupted
approval. `prepare()` also raises a dedicated recovery-required error when the
journal already exists; generic validation rollback must not convert that error
to `rolled_back` or clear the prior witness.

A guarded manifest publish distinguishes an ordinary compare-and-swap conflict
from unconfirmed directory durability. If neither publication nor rollback
durability can be confirmed, the proposal remains `pending` and the journal is
not cleared. A failed projection or manifest rollback likewise retains the
journal even after a `rolled_back` record was durably appended. Recovery can
therefore inspect the exact endpoints and rebuild projections after storage is
healthy instead of discarding its only durable witness.

The guarded CAS stages `after` at the journal-bound, same-directory hidden swap
path and durably publishes that name before `RENAME_EXCHANGE`. It removes the
swap name only after predecessor verification and the exchange directory
barrier. A termination after exchange but before verification therefore leaves
the exchanged-out predecessor at a deterministic path. If those bytes are
neither exact journal `before` nor exact `after`, reconciliation returns
`conflict` and preserves both the live manifest and swap witness; it cannot
mistake a displaced external edit for a verified commit. Schema-v1 journals
from before this witness protocol fail closed when a pending live manifest is
already exact `after`, because they cannot prove what the old random swap path
contained.

After the first exchange, predecessor mismatch or publish-directory fsync
failure never triggers an automatic second exchange. A userspace
check-then-exchange rollback has its own race in which a newer external write
could be moved into the witness and then deleted. These cases therefore retain
the proposal as `pending`, preserve the journal and both paths, and require
explicit reconciliation. “Conflict” guarantees byte preservation, not that an
external predecessor remains at the original pathname after the first atomic
exchange.

When rollback removes a partial catalog or DAG that did not exist in the
snapshot, it also fsyncs the projection parent directory before the journal may
be cleared; a deleted partial projection cannot reappear after a power loss.

### Reconciliation never infers a missing approval

`SkillEvolutionGovernance.reconcile(operator, reason)` is the only recovery
Interface. It acquires the proposal lock and handles the observable states as
follows:

| Durable proposal | Live manifest | Swap witness | Action |
| --- | --- | --- | --- |
| `pending` | exact journal `after` | absent or exact `before` | guarded restore to `before`, rebuild projections, append `rolled_back` |
| `pending` | exact journal `before` | absent or exact `after` | rebuild projections, append `rolled_back` |
| `rolled_back` / `stale` | exact `before` or `after` | matching safe endpoint or absent | converge to `before`, rebuild projections, clear journal |
| `approved` | exact journal `after` | absent or exact `before` | keep approval, rebuild projections, clear journal |
| any | otherwise | any third byte sequence or inconsistent endpoint | return `conflict`; preserve all files and keep the journal |

An external write that wins between reconciliation's live-byte read and its
guarded restore is the same structured `conflict`, not an internal server
error. The guarded write preserves the later bytes and the journal remains for
manual inspection.

The recovered proposal records the original approval label/reason together
with `reconciled_by`, `reconciliation_reason`, and
`interrupted_approval_reconciled`. Reconciliation is idempotent after the
journal is cleared.

Catalog and compatibility DAG are regenerated from the current canonical
manifest tree. Recovery never replays old generated-file snapshots, so an
unrelated later manifest change can participate in the new projections.

### Keep the cross-repository boundary narrow

The authenticated Backend adds:

`POST /skill-evolution/reconcile` with body `{operator, reason}`.

OmicsClaw-App does not parse the journal, restore files, choose a recovery
branch, or infer approval. A future operator UI may proxy this Backend command,
but the current milestone intentionally changes only Backend policy,
persistence, and HTTP contract.

## Consequences

- Process termination and persistent proposal-store failure no longer require
  manual guessing when the manifest still matches an exact journal endpoint.
- External manifest drift is preserved and reported as a conflict. Recovery
  cannot be used as an overwrite primitive.
- A journal that survives after a durable `approved` append cannot demote that
  decision; it only completes deterministic projections and cleanup.
- This is recoverability, not a single crash-atomic transaction spanning JSONL,
  manifest, catalog, and DAG. Each durable step remains independently visible,
  and explicit reconciliation converges it after restart.
- Direct mutation of the journal, proposal JSONL, or health JSONL is unsupported
  store corruption. All production writers use the Backend-owned stores.

## Alternatives considered

- **Auto-approve when the manifest already contains promoted bytes.** Rejected:
  bytes do not prove that the human decision record became durable.
- **Always restore saved catalog/DAG bytes.** Rejected: generated projections
  are rebuildable and a stale snapshot can erase later canonical manifest
  changes.
- **Put recovery in OmicsClaw-App.** Rejected: the App does not own manifests,
  proposal state, projection generation, or governance policy.
