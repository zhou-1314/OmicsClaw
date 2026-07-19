# Bind retried Run submissions to one fenced execution assignment

## Status

Accepted (2026-07-14).

Refines
[ADR 0043](0043-local-first-control-plane-extensible-run-execution.md),
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md),
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md),
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md),
[ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md), and
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md).

This ADR closes two separate duplicate-execution windows. A durable **Run
Submission Binding** maps retries of one logical submission to one canonical
Run, while one control-plane-fenced **Execution Assignment** permits that Run
to start at most once. v1 deliberately adds neither a renewable Execution
Lease nor automatic replay or reassignment.

## Implementation

Repository foundation implemented (2026-07-15); Canonical Simple Skill tracer
and Desktop, prompt-toolkit demo, root `oc run` exact demo, and Remote
exact-demo submission Adapters integrated (2026-07-17). The isolated
Repository atomically binds one Submission ID and
Fingerprint to one Run, grants one Assignment-ID-fenced start transition,
guards Execution Reference updates and rejects mismatched terminal reports.
Desktop `POST /v1/runs` now accepts one 32-hex Run Submission ID, computes the
versioned canonical fingerprint from caller-declared Scope, Skill demo input,
empty parameters and complete static resource request, and returns a matching
duplicate before current Registry, Project, budget or Dispatcher gates. The
Dispatcher treats only `ASSIGNED` as executor-start authority; every completion
report carries the same Assignment ID.

Each explicit prompt-toolkit `/run <canonical-skill> --demo` invocation creates
one fresh 32-hex Submission ID exactly once, freezes the Backend-resolved Skill
and resource request into one immutable submission, and reuses that submission
for any in-call duplicate observation. Accepted and matching duplicate results
wait on the same canonical Run; neither waiting nor terminal projection grants
another Assignment or invokes the executor.

Each of the three root exact-demo Scope wires likewise creates one fresh
32-hex Submission ID and never falls back after canonical routing. Its accepted
or duplicate Receipt is observed through the same typed Runtime; explicit
Project and Unassigned enter the same fingerprint as immutable Scope, while
raw aliases and every other option-bearing, conflicting, reordered,
duplicated, abbreviated or attached-value demo spelling fail closed before
legacy execution. This is new intent per explicit invocation, not idempotency
across CLI processes.

Remote `POST /jobs` now uses its exact 32-hex `Idempotency-Key` as the Run
Submission ID and includes complete caller-declared resource semantics in the
strict request fingerprint. A novel request returns `202`; matching duplicate
lookup returns the original Run with `200` before current Registry, resource,
capacity, Dispatcher or quarantine gates; different semantics under the same
key return `409 run_idempotency_conflict`. The compatibility
`run-<run_id>` Job ID is derived display state, not another Submission ID, Run
ID, Assignment ID or Execution Reference. The Adapter shares the same sole
Assignment transition and shared runner as Desktop/CLI and never creates an
executable Job record.

Remote detail/list/SSE/artifact reads are bounded observation Interfaces. SSE
disconnect releases only the observer, and canonical cancel resolves the
projection to its Run ID before invoking `RunRuntime.cancel()`. Canonical retry
returns `canonical_retry_not_supported`; this slice does not synthesize a new
Submission ID, clone payload or grant a second Assignment. A future explicit
scientific retry remains a new Submission/Run linked to the immutable original
under this ADR's core rule.

For the canonical local tracer the Assignment transaction also binds one
unique, write-once `linux-user-systemd-bwrap-v1` Execution Reference before any
launcher instruction. The async driver must consume that exact reference.
Migration 8 enforces required format, uniqueness and write-once semantics; a
parent-death-bound launcher plus bubblewrap containment closes the crash window
between helper creation and user-systemd scope publication.

Persistent fencing incidents integrated (2026-07-17). Migration 9 and the
typed Repository Interface atomically append a content-free incident whenever
an existing Run rejects a missing/mismatched Assignment report or Execution
Reference update. A conflicting terminal report is inserted and committed in
the same rejecting transaction; only after that commit does the Repository
raise `RunIntegrityIncidentError`, so the audit fact cannot disappear in the
normal rollback path. Exact terminal replay and a losing second Assignment
claim remain ordinary idempotent outcomes and create no incident. Runtime
owner/recovery and verified Manifest conflicts use the same idempotent ledger;
they never reopen a Receipt, replay, or grant a second Assignment.

Root non-demo and unsupported option-bearing forms, Textual TUI, non-demo and
option-bearing prompt-toolkit `/run`, Agent tool, Bench, Workflow and
Autonomous entry points still have no shared Run Submission ID or durable
idempotency binding. A fresh CLI demo invocation is new intent; this slice does
not promise idempotency across CLI restarts.
Historical terminal Remote Jobs retain their independent UUIDs only as
read-only legacy identity; historical active scientific Jobs close at startup
as `interrupted/legacy_execution_unrecoverable` without replay, retry or a
fabricated Binding/Assignment. The shared `ResourceLease` remains only
process-local capacity accounting and never proves Run execution ownership.

## Context

ADR 0052 prevents Channel/Desktop redelivery from accepting the same Turn
twice, but one Turn may create several Runs and a live Tool Runtime may retry a
single Run submission. Explicit CLI, Desktop, Bench and API Run Requests do not
pass through conversational ingress at all. Turn idempotency therefore cannot
be reused as Run idempotency.

ADR 0057 gives every accepted Run a control-generated opaque Run ID and a
minimal non-replayable Run Receipt. Run ID is created only at acceptance, so it
cannot identify a client retry that arrives before the caller learns that ID.
Conversely, accepting one Run Receipt does not prevent two scheduler tasks or
a stale callback from starting or terminalizing it unless execution ownership
is fenced separately.

The local-first single-control-process architecture does not need a generic
distributed lease protocol. A time-based lease cannot stop an orphaned
scientific process, and automatically assigning another Worker after expiry
could execute non-idempotent work twice. The required v1 guarantee is narrower:
one durable admission per logical submission and one process-bound start grant
per accepted Run.

## Decision

### One guarantee has two layers

OmicsClaw v1 provides both:

```text
Run Submission ID -> at most one accepted Run ID
Run ID             -> at most one Execution Assignment
```

The first relation prevents duplicate Run creation. The second prevents
duplicate executor start and stale lifecycle updates. Neither relation claims
end-to-end exactly-once scientific side effects.

### Every top-level submission has a Run Submission ID

A **Run Submission ID** is a globally unique opaque identifier generated by the
control-plane caller before Run acceptance. It identifies one logical attempt
to submit a top-level Run and carries no Project, Turn, Skill, input, executor,
retry or timestamp semantics.

It is distinct from canonical Run ID:

- Desktop generates it when the Owner activates a Run action and reuses it for
  every transport retry of that action;
- CLI generates it once per command invocation; a manually repeated command is
  new intent and receives a new value;
- Tool Runtime generates it once per logical top-level Run call inside an
  active Turn and reuses it for internal submission retries;
- internal Bench, Workflow-launch or other control-plane callers generate it
  at their user-visible operation boundary;
- nested Run Steps do not create top-level Run Submission IDs or Bindings.

The value is untrusted input at an API boundary and must be length-, format-
and entropy-bounded. A caller may choose this retry identity but never chooses
Run ID. OmicsClaw does not derive it from content, a path, a tool-call ID or the
future output directory.

### Control Plane State persists the Run Submission Binding

The Control Database persists one unique **Run Submission Binding**:

```text
Run Submission ID -> (Run ID, Run Request Fingerprint)
```

The Binding and its Run Receipt are created in the same transaction and share
retention. It is not a TTL cache and disappears only with the explicit purge
that removes the corresponding Run identity. It contains no executable Run
Request or scientific payload.

The **Run Request Fingerprint** is a versioned canonical digest of the caller-
declared execution semantics. It includes, when present:

- Run Kind and canonical Skill, Workflow or Autonomous target;
- explicit input identities or already available immutable digests;
- normalized declared parameters and requested options;
- explicit Project selection;
- `parent_turn_id` and `retry_of_run_id`.

It excludes canonical Run ID, arrival time, dynamic executor/resource choice,
Execution Assignment, Execution Reference, output path, credentials, logs,
tracing, cancellation/approval capabilities, current navigation pointers and
other dynamically resolved defaults. Raw input contents and secrets are never
copied into `control.db` merely to compute the digest.

The Binding stores the fingerprint version. A compatibility implementation
must compare with the recorded version rather than silently reinterpret an old
binding under a changed normalization algorithm.

### Duplicate lookup precedes novel Run admission

After authenticating the control-plane caller, admission normalizes the Run
Submission ID and Fingerprint without durable side effects and checks the
Binding before current Project lifecycle validation, queue capacity reservation,
Run ID generation, output allocation or execution.

- Same Submission ID and same Fingerprint returns the existing Run ID and
  current Run Receipt status in every lifecycle state. It creates no new
  Assignment or side effect.
- Same Submission ID and a different Fingerprint returns a typed
  `run_idempotency_conflict` and creates nothing.
- Different Submission IDs remain distinct Owner intent even when every input
  and parameter is equal.

Checking duplicates first preserves historical acceptance. A duplicate still
returns its original Run after a Project is archived or navigation changes; it
never re-resolves defaults, retargets Scope or restarts execution. A novel
Project-scoped submission must still pass the current `active` Project gate.

### Novel acceptance is atomic and the executable queue remains bounded

For a novel Submission ID, the control plane reserves capacity in a bounded
process-local Run submission buffer before acceptance. Exact capacity and
scheduling policy are implementation settings, but the buffer may not be
unbounded.

It then uses one `control.db` transaction to:

1. validate the current Project lifecycle and resolve immutable Run Scope;
2. generate the opaque Run ID;
3. create the `queued` Run Receipt;
4. create the Run Submission Binding.

Only after commit does it enqueue the typed executable Run Request in process
memory. If capacity is unavailable or the transaction fails, the reservation
is released and the caller receives `run_backpressure` or the relevant
validation error; no Run, Receipt or Binding exists, so the same Submission ID
may safely retry.

A unique constraint is authoritative when concurrent copies of the same
Submission ID race. If commit succeeds but enqueueing fails in the live
process, the accepted Receipt becomes `failed` with `submission_failed`. If the
process dies in that gap, startup reconciles the queued Receipt to
`interrupted`. The durable Binding remains and every duplicate returns that
non-replayed Run.

The process-local buffer contains the only executable payload. Run Submission
Binding and Run Receipt never become a persistent queue or replay source.

### Explicit retry is itself an idempotent new submission

Retry is permitted only from a terminal Run and always creates a new Run ID.
The retry command carries a fresh Run Submission ID and includes the source
`retry_of_run_id` in its Fingerprint. Repeated delivery of that retry command
therefore returns the same newly created Run instead of creating several retry
Runs.

Retry preserves the original immutable Run Scope and revalidates that a
Project-scoped Project is active. A duplicate accepted retry returns its
existing new Run even if the Project was archived later. A novel retry against
an archived Project is rejected. Executing equivalent work under another
Project is an ordinary new Run, not a retry.

### Every Run has at most one Execution Assignment

An **Execution Assignment** is the control-plane grant that authorizes exactly
one executor invocation to begin one Run. **Assignment ID** is its opaque
fencing value. It is not Run ID, an Execution Reference, a Resource Lease or an
authentication credential by itself.

In v1 one Run can acquire at most one Assignment for its entire lifetime. The
Control Database records the Assignment ID, executor kind, assignment time and
typed Execution Reference as narrow Run control state. The canonical local
executor requires that reference atomically; other future executor kinds may
define a different typed reference contract. Those fields may be
stored on the Run Receipt row or normalized internally, but they share the same
Repository and transactional authority and never contain the Run payload.

When an executor is ready, the Repository performs one compare-and-set:

```text
precondition:
  run_id = <Run ID>
  status = queued
  assignment_id IS NULL

transition:
  status = running
  assignment_id = <new opaque Assignment ID>
  executor_kind = <kind>
  execution_reference = <typed write-once owner>
  assigned_at = <time>
```

The control plane then supplies Run ID, Assignment ID, typed Run Request and
resolved Run Scope to the executor. The executor must not begin scientific side
effects until the Assignment transition is committed and acknowledged. A
second claimant loses the compare-and-set and must not invoke the tool.

### Assignment ID fences lifecycle evidence

Every executor-originated start acknowledgement, Execution Reference update,
terminal report and cancellation confirmation carries both Run ID and
Assignment ID. The authenticated control-plane Interface applies guarded
transitions only when the Assignment matches the current non-terminal Receipt.

- a duplicate compatible report is idempotent;
- a mismatched Assignment ID is stale or invalid evidence and is rejected;
- a callback after terminalization cannot reopen the Receipt;
- conflicting terminal evidence for the same Assignment is recorded as an
  integrity incident and never resolved by last-write-wins.

Assignment ID fences state mutation; it does not make an untrusted callback
authorized. Local and remote callback channels retain their normal
authentication and validation.

### Assignment and queued cancellation race through one state gate

Queued cancellation and Assignment creation compete through guarded control
state transitions:

- if `queued -> canceled` commits first, Assignment creation fails and no
  executor may start;
- if `queued -> running` plus Assignment commits first, cancellation moves the
  Run to `cancel_requested` and signals that Assignment;
- `canceled` is committed only after queued cancellation won before start or
  the assigned executor later confirms stop.

An Assignment whose local spawn/setup fails terminalizes the same Run as
`failed` with a normalized code. It is not silently replaced by another
Assignment.

### No reassignment or renewable Execution Lease in v1

v1 never grants a second Assignment to the same Run after failure,
cancellation, ownership loss or timeout. Recovery or explicit retry creates a
new Run ID and a new Submission ID. A future reattachment capability, if
accepted, must reconnect to the same Assignment and prove external scheduler
ownership; it cannot manufacture another start grant.

v1 has no `lease_expires_at`, Worker heartbeat, lease stealing, timeout-based
requeue or automatic executor reassignment. The single Backend process owns
process-bound Assignments. Current remote mode runs that Backend on the remote
machine rather than operating a separate durable Worker fleet.

The existing **Resource Lease** reserves bounded CPU, memory, GPU, threads and
temporary disk inside one process. It does not authorize Run execution, fence a
callback or survive restart. Product and domain language must say Resource
Lease in full when that concept is intended; bare "lease" must not be used for
Run ownership.

A future cross-process Worker or durable external scheduler requires another
ADR for executable-payload durability, Execution Lease renewal, heartbeat,
fencing generation and safe reattachment. Lease expiry alone can never prove
that a scientific process stopped and therefore can never authorize automatic
same-Run reassignment.

### Restart invalidates process-bound Assignment without replay

On Backend restart, the process-local Run buffer and every process-bound
Assignment capability are gone. Startup applies ADR 0057 completion-evidence
reconciliation after proving the persisted local owner stopped; otherwise
queued, running or cancel-requested work becomes `interrupted`. If owner or
completion proof is unobservable, the Receipt remains nonterminal and novel
admission is quarantined. The Submission Binding and Assignment ID remain as historical
control evidence but authorize no new execution.

A late callback from the old Assignment cannot change the terminal Receipt. A
repeated Run submission returns the existing interrupted Run, not a replacement
Assignment. Only an explicit idempotent retry command creates new work.

### Submission and observation remain separate

The target Interface has semantics equivalent to:

```text
POST /runs
Idempotency-Key: <Run Submission ID>
-> Run ID + Run Receipt status

POST /runs/{run_id}/retry
Idempotency-Key: <new Run Submission ID>
-> new Run ID + retry_of_run_id + status

GET /runs/{run_id}
-> Run Receipt plus authorized projections
```

Exact routes and header names may evolve, but observing a Run never submits,
assigns, resumes or retries it. The Remote compatibility SSE now streams
Receipt snapshots only; `GET` cannot call an `_ensure_*` function that starts
queued scientific work.

### Migration does not fabricate historical idempotency or ownership

ADR 0057's Run migration is extended as follows:

- historical Jobs/Runs do not receive invented Run Submission Bindings because
  old UUIDs, payloads and timestamps cannot prove client retry identity;
- terminal imported Runs need no fabricated Assignment ID;
- legacy queued/running executable Job JSON is inventoried and reconciled to
  `interrupted`, never used as an automatic replay source;
- `inputs` and `params` migrate to the scientific Manifest when evidence is
  trustworthy, not into Run Receipt or Submission Binding;
- a current Job ID may survive as a Legacy Identity Map alias or Execution
  Reference but never as Run Submission ID, Run ID or Assignment ID;
- generic `chat_stream` Jobs remain Turn observation/projection state and do
  not become Run Bindings or Assignments.

After cutover, compatibility `/jobs` routes must delegate to canonical Run
admission, status and cancellation Interfaces. They cannot retain an
independent executable JSON queue or lifecycle authority.

## Guarantee boundary

This decision guarantees:

- at most one accepted Run per Run Submission ID;
- at most one start grant per Run;
- stale or duplicate Assignment callbacks cannot rewrite canonical lifecycle;
- observation and redelivery do not start scientific work;
- restart never automatically replays or reassigns a Run.

It does not guarantee exactly-once external or scientific side effects. A
process may change an external system and lose contact before durable terminal
evidence exists. The honest outcome is `interrupted`; an explicit Owner retry
may repeat that effect. Stronger protection belongs to individual Tool/Skill
idempotency contracts, transactional external systems or compensating actions,
not to Run identity alone.

## Consequences

- Desktop, CLI, Bench, Tool Runtime and Remote compatibility entry points must
  create and propagate Run Submission IDs.
- `control.db` gains durable Run Submission Bindings and fenced Assignment
  fields/operations while executable payloads stay process-local.
- Run admission needs a bounded process-local buffer and atomic capacity
  reservation around acceptance.
- Run cancellation, executor completion and retry become compare-and-set domain
  commands rather than JSON last-write-wins updates.
- Canonical Remote exact-demo replay, premature cancellation and orphan-failed
  behavior are removed; historical active Jobs close as interrupted without
  replay, while broader Remote shapes still require explicit future Adapters.
- The architecture retains its local-first single-process simplicity while
  preserving a clean future seam for an explicitly designed Worker protocol.

## Rejected alternatives

- **Use Run ID as the idempotency key.** Rejected because Run ID does not exist
  until after the retry ambiguity has already occurred and callers never mint
  canonical identity.
- **Deduplicate equal inputs and parameters.** Rejected because the Owner may
  intentionally repeat an analysis, mutable paths may change contents, and
  content equality is not intent identity.
- **Keep a process-local or TTL submission cache.** Rejected because response
  loss and Backend restart are precisely the cases that need the Binding.
- **Reuse Turn ID or provider tool-call ID.** Rejected because one Turn may
  create several Runs, direct Runs have no Turn, and provider call IDs are not
  the control plane's stable Run-submission contract.
- **Persist the complete Run Request beside the Binding.** Rejected because it
  would turn `control.db` into a replay queue and violate ADR 0057.
- **Allow several executors to race and accept the first result.** Rejected
  because losing executions may still modify artifacts or external systems.
- **Automatically create a second Assignment after timeout or failure.**
  Rejected because inability to observe the first executor is not proof it
  stopped.
- **Add renewable Execution Leases and heartbeats now.** Rejected because v1
  has no independent durable Worker fleet, while expiry cannot safely fence an
  already-running scientific process.
- **Reuse Resource Lease as execution ownership.** Rejected because resource
  accounting and authority to start/terminalize a Run have different lifecycle,
  durability and failure semantics.
- **Claim exactly-once scientific execution.** Rejected because no local
  Receipt transaction can atomically cover arbitrary files, schedulers and
  external scientific tools.

## Forward refinement (2026-07-14)

[ADR 0061](0061-separate-run-dispatch-from-process-local-resource-scheduling.md)
closes the deferred v1 scheduling policy. One bounded strict-FIFO Run
Dispatcher coordinates the sole Assignment only after the first execution unit
has a provisional Resource Lease; one separate strict-FIFO Execution Resource
Scheduler owns global multidimensional process capacity. The Lease remains
process-local accounting and cannot authorize, replay or reassign the Run.
