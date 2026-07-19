# Model Project lifecycle as reversible archive and restore

## Status

Accepted (2026-07-14).

Refines
[ADR 0018](0018-investigation-thread-equals-project.md),
[ADR 0048](0048-resolve-conversation-with-active-reply-target-binding.md),
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md),
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md), and
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md).

It replaces the legacy Bench interpretation of Project deletion as
`ThreadMemory.is_deleted`, while preserving the Project granularity and
immutable Conversation binding decisions. It does not define deletion of a
Memory node, Dataset, CLI Session, individual Run, or other independently owned
content.

**Run-scope refinement (2026-07-14):**
[ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md)
defines immutable `ProjectScope(project_id) | UnassignedScope`. This ADR's
Project lifecycle gate applies only to Project-scoped Run admission. The
Unassigned Run Grouping has no Project lifecycle, and archive/restore never
changes the scope of an existing Run.

**Run-lifecycle refinement (2026-07-14):**
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md)
makes the Run Receipt in `control.db` authoritative for the Project-busy query.
Archive is blocked by any accepted non-terminal Project-scoped Run, including
`queued`, `running` and `cancel_requested`; filesystem or Manifest scanning is
not the lifecycle gate.

**Run-submission refinement (2026-07-14):**
[ADR 0058](0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md)
requires duplicate Run Submission Binding lookup before the current Project
lifecycle and capacity gates. A duplicate returns its historically accepted Run
after archive, while a novel submission or retry must still pass the active
Project gate.

**Attachment-retention refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
makes accepted Attachment Records immutable history of their originating Turn.
Archive/restore, `/new`, cancellation and navigation never remove them;
permanent Blob erasure belongs to the same future reference-aware cross-store
purge boundary as other retained Project content.

**Scientific-Memory refinement (2026-07-15):**
[ADR 0064](0064-scope-scientific-memory-and-fence-project-projections.md)
keeps the archive gate closed to novel Project Memory mutation while allowing
one frozen, content-free Projection Intent created during active-state work to
be applied idempotently after archive.

## Implementation

Repository foundation implemented (2026-07-15), product migration not
implemented. The isolated ControlStateRepository now implements revisioned
active/archive/restore commands and atomically treats queued/running Turns plus
queued/running/cancel-requested Project Runs as busy. Bench still stores
Project-like thread metadata in
`project://<thread_id>` Memory, implements `DELETE /thread/{thread_id}` by
setting `ThreadMemory.is_deleted=True`, and hides that record from ordinary
list/get operations. It has no restore command. The operation does not validate
or coordinate bound Sessions, active chat execution, Run records, Transcript
content, ToolResult or attachment blobs, `project_meta.json`, or Project output
directories. No product route uses the new Project Record yet.

## Context

OmicsClaw Projects retain scientific knowledge, Conversations, Runs and files
across several specialized stores. Those stores deliberately do not share one
transaction, retention policy or physical database. A lifecycle value named
`deleted` would therefore be ambiguous: it could mean hidden from a list, no
longer executable, content scheduled for removal, partially erased, or fully
purged.

The current soft-delete proves only the first meaning. Calling it deletion
creates a false promise that scientific content and references have been
removed. Conversely, implementing immediate cascading deletion would risk
breaking immutable Conversation-to-Project bindings, Transcript and Turn
history, Memory lineage, stored output paths and ingress-idempotency facts.

The first Project lifecycle needs a small reversible state machine that closes
new scientific work without destroying research continuity. Permanent data
erasure has a different cross-store failure and recovery model and must not be
hidden inside ordinary Project CRUD.

## Decision

### Project has exactly two durable lifecycle states

Every Project Record has one current lifecycle state:

```text
active | archived
```

A newly created Project is `active`. `archive` transitions `active -> archived`;
`restore` transitions `archived -> active`. Both commands are idempotent: a
request already satisfied returns the current Project and `changed=false`
rather than creating another transition or failing.

`restored` is an operation outcome, not a durable state. `deleted`, `trashed`
and `purged` are not v1 Project states. The Project Record stores the current
state, `lifecycle_changed_at`, the already accepted creation/update timestamps,
and the projection-friendly `revision`; a real transition increments
`revision`. No authoritative `is_deleted` flag exists after cutover.

### Archived means retained but closed to new scientific work

An archived Project remains a real, addressable Project with the same opaque
Project ID. Explicit lookup and an archived-project view may return it, while
ordinary Project lists default to `active` only. The Owner may inspect, export,
restore, or correct administrative display metadata for an archived Project.

Archiving does not delete, move, detach or rewrite:

- the Project Record or Project ID;
- a Conversation Record, its immutable Project binding, Transcript, or inbound
  attachment context;
- an Active Conversation Binding that currently points to a Project-bound
  Conversation;
- `project://<project_id>` knowledge, research lineage or historical Memory;
- Run records, manifests, indexes, artifacts or Project output directories;
- `project_meta.json`, ToolResult or attachment bodies;
- Turn Receipts or Ingress Idempotency Bindings.

An archived Project rejects every operation that would start or record new
scientific work:

- a new Conversation cannot be created or first-bound to it;
- a novel conversational input cannot be accepted as a Turn in a Conversation
  already bound to it;
- a new Project-associated Run Request cannot be admitted;
- Project-scoped scientific Memory, hypothesis, analysis or artifact mutation
  cannot begin.

Administrative display-metadata correction is allowed because it changes
navigation, not scientific content or execution. Restoration re-enables the
same Project and all of its existing bound Conversations; it never creates a
replacement Project or rebinds a Conversation.

### Duplicate ingress precedes current-state admission

After Owner admission, ingress checks an existing Ingress Idempotency Binding
before applying the Project lifecycle gate. The same key and fingerprint still
returns its original Turn in any lifecycle state even if the Project was
archived after that Turn was accepted. Archiving cannot rewrite an accepted
historical intent.

For a novel idempotency key, Project state validation occurs before any new
Conversation creation or binding, Active Conversation Binding mutation caused
by that submission, Turn Sequencer reservation, Turn Receipt, Ingress
Idempotency Binding, attachment staging, Transcript write, Agent execution,
tool call or Run. A novel input targeting an archived Project is rejected with
a typed `project_archived` outcome and creates no Turn.

Keeping the existing Active Conversation Binding is deliberate. It prevents
the next message from silently selecting or creating an unrelated unbound
Conversation. The Surface may offer restore or explicit Project/Conversation
selection; after restore, the same pointer continues the same Conversation.

### Archive is serialized with Turn and Run admission

Archive/restore commands and Project-aware Turn/Run admission share one
Project-scoped lifecycle gate in the single-process control plane. This closes
the race in which an archive preflight observes no work while another request
simultaneously admits new work.

Archive succeeds only when the Project has no accepted non-terminal Turn and no
Project-associated Run in `queued`, `running`, or `cancel_requested`. Otherwise
it returns a typed
`project_busy` result and leaves the Project active. Archive does not implicitly
cancel, wait for, or detach live work; the Owner may wait or cancel explicitly
and retry. Once the authoritative archive transaction commits, every novel
Turn or Run admission observes `archived` and fails closed.

Restore requires no projection repair precondition. It changes the
authoritative state first; stale Memory or filesystem projections remain
observable repair work and cannot keep the restored Project inactive.

### Control state changes first; content remains in its owner

Archive and restore commit the Project Record transition in `control.db`.
Memory, `project_meta.json`, Desktop App rows or other projections may mirror
the state and revision afterward, but projection failure does not roll back or
override the authoritative transition. Output directory names remain frozen
and no content store may infer lifecycle from directory presence.

The target control-plane Interface exposes explicit commands such as:

```text
POST /projects/{project_id}/archive
POST /projects/{project_id}/restore
GET  /projects?state=active|archived|all
```

Exact route spelling may evolve, but a route named `DELETE Project` must not
mean archive in the steady state. During cutover, the existing
`DELETE /thread/{thread_id}` may be a documented deprecated adapter that invokes
the authoritative archive command. Product language must say **Archive
Project**, expose archived Projects and provide **Restore**.

### Legacy soft-delete imports as archive

The ADR 0054 migration inventory includes hidden `ThreadMemory` records and
maps them deterministically:

```text
is_deleted == false -> active
is_deleted == true  -> archived
```

The import preserves a valid non-conflicting opaque Project ID and all explicit
Conversation, Memory, Run and output references. `lifecycle_changed_at` uses
the trustworthy timestamp of the soft-delete version when available; otherwise
it uses migration time and records the timestamp source in migration evidence.

After cutover, `ThreadMemory.is_deleted` may exist briefly as a one-way
compatibility projection but is never read as lifecycle authority and ordinary
runtime has no fallback to it. Restoration changes the Project Record; legacy
projection repair may follow without becoming a precondition for continuity.

### Permanent data purge is not a v1 Project operation

v1 has no automatic retention timer and no command that permanently deletes a
Project aggregate. Archived content continues to consume storage by design.

If permanent Project data erasure becomes necessary, it requires a separate
ADR and an explicit cross-store purge workflow. At minimum that future design
must require an archived, idle Project; produce a dry-run inventory; require
unambiguous Owner confirmation; durably track per-store progress and retry;
coordinate Transcript, Memory, Run, ToolResult, attachment, output and App
projection cleanup; and preserve the minimum control-plane tombstone needed to
prevent ID reuse and make surviving historical references unambiguous. A
partially completed purge must be observable and resumable rather than
represented by an ordinary lifecycle enum update.

## Consequences

- Project lifecycle is understandable and reversible without pretending that
  distributed scientific content was atomically deleted.
- Archived Projects preserve every Conversation and research artifact but
  cannot accept new scientific work until restored.
- Active Conversation Bindings remain stable across archive/restore, so no
  hidden rebind or replacement Conversation violates earlier decisions.
- Ingress redelivery continues to resolve the original Turn even when current
  Project state would reject a novel request.
- Archive never surprises the Owner by canceling a long-running analysis; busy
  work must finish or be canceled explicitly.
- Archived data consumes disk indefinitely until a separately governed purge
  capability is accepted and implemented.
- Backend, CLI, Desktop and Channel product language and APIs must migrate from
  delete/soft-delete to archive/restore.
- The Control Database schema and Project Repository must add lifecycle state,
  state-change timestamp, revisioned commands and Project-aware admission
  checks before the legacy `is_deleted` field can be retired.

## Rejected alternatives

- **Use `active / archived / restored / deleted` as peer states.** Rejected
  because `restored` is a transition back to active and `deleted` does not say
  which independently owned content was erased.
- **Add a trash state before archive.** Rejected because, without an accepted
  purge deadline or separate behavior, trash and archive both mean hidden,
  retained and restorable while multiplying transitions and edge cases.
- **Hard-delete the Project Record and cascade immediately.** Rejected because
  there is no transaction across control, Transcript, Memory, Run and file
  stores, and deleting identity would break immutable historical references.
- **Archive only hides the Project but permits new Turns and Runs.** Rejected
  because archive would then have no reliable execution meaning and hidden
  Projects could continue changing.
- **Cancel running work automatically when archiving.** Rejected because
  cancellation may interrupt expensive or partially side-effecting scientific
  tools; explicit cancellation and archive are separate Owner intents.
- **Clear or retarget Active Conversation Bindings on archive.** Rejected
  because the next inbound message could silently enter an unrelated
  Conversation and restoration would not resume the original continuity.
