# Make Control Plane State authoritative for Project, Conversation, and Turn identity

## Status

Accepted (2026-07-14).

Refines
[ADR 0018](0018-investigation-thread-equals-project.md),
[ADR 0019](0019-kg-first-class-dependency-for-bench.md),
[ADR 0035](0035-project-scoped-output-directories.md),
[ADR 0044](0044-single-owner-control-plane-and-owner-only-channel-ingress.md),
[ADR 0045](0045-owner-identity-is-not-a-state-partition.md),
[ADR 0048](0048-resolve-conversation-with-active-reply-target-binding.md),
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md),
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md), and
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md).

It supersedes ADR 0018's claim that a `project://<id>` Memory binding is the
durable Project object, narrows ADR 0019's "Memory owns agent/study state"
language to Project knowledge rather than Project identity/lifecycle, and
narrows ADR 0035's `project_meta.json` authority to the output subsystem's
Project-to-directory projection. Their Project granularity, Memory/KG knowledge
boundary, and Project-scoped output layout remain accepted.

**Physical-store refinement (2026-07-14):**
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md)
implements this logical boundary as a Backend-exclusive local SQLite
`control.db`, while keeping Transcript, Memory, App data and Run artifacts in
their separate stores. References below to a later physical-store decision are
the deferral that ADR 0054 has now resolved.

**Project-lifecycle refinement (2026-07-14):**
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md)
resolves the lifecycle deferral below: Project Records are `active` or
`archived`; restore returns the same Project to `active`, and permanent
cross-store data purge is not a v1 Project state or CRUD operation.

**Run-scope refinement (2026-07-14):**
[ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md)
resolves the remaining output deferral. `default/` is the non-Project
Unassigned Run Grouping; each Run freezes `ProjectScope(project_id)` or
`UnassignedScope` at admission, and v1 does not reassign completed Runs.

**Attachment-store refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
keeps Attachment Records and Blobs in a specialized Attachment Store. That
store may persist a proposed Turn ID but cannot establish Turn existence;
Control Plane State remains authoritative for acceptance, and cross-store
reconciliation follows the committed Turn Receipt.

**Outbound-delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
makes Control Plane State authoritative for canonical Channel Outbound Delivery
identity, ordered Item plan and operational lifecycle. Transcript and artifact
stores still own reply and media content.

## Implementation

Repository foundation implemented (2026-07-15), production cutover not
implemented. `omicsclaw/control/` now owns an isolated typed
`ControlStateRepository` with Project, Conversation, Active Binding, Turn
Receipt and Ingress Binding transactions and invariants, but no Surface or
legacy path calls it yet. Project identity is therefore still operationally
split among Bench thread IDs,
`project://` Memory, `project_meta.json`, request `thread_id` fields and output
indexes. Conversation continuity is split among CLI sessions, Channel composite
keys, Desktop session IDs and Transcript keys. No shared control-plane
repository owns Project records, Conversation records, Active Conversation
Bindings, Turn Receipts and Ingress Idempotency Bindings as one authoritative
state boundary in current production paths.

## Context

The accepted architecture makes Project the unit of research continuity across
Surfaces and makes Conversation and Turn durable control-plane identities. That
cannot be implemented safely while several projections can independently
declare whether a Project exists or what it is called.

Early Bench work used a `project://<id>` Memory subtree as the Project anchor.
Project-scoped output later used `project_meta.json` as the durable
Project-to-directory mapping and copied display metadata into it. Both were
reasonable before Project became a shared control-plane object, but treating
either as the Project registry now creates multiple writers and ambiguous
recovery: a Memory row may exist without an output directory, a directory may
survive after a Memory deletion, and a Conversation may point at an identifier
that neither projection recognizes.

The same problem would recur for Conversation and Turn if each Surface or
Transcript store created its own lifecycle records. OmicsClaw needs one logical
owner for durable identity and lifecycle facts, while allowing content-heavy
stores and filesystem layouts to remain specialized.

## Decision

### One authoritative logical state boundary

**Control Plane State** is the sole durable authority for these records:

- **Project Record** — opaque Project ID, current display metadata, creation
  metadata and lifecycle facts required to determine that the Project exists;
- **Conversation Record** — opaque Conversation ID, immutable Conversation
  Address, optional immutable Project binding, and Conversation lifecycle facts;
- **Active Conversation Binding** — the current Conversation selected at one
  Conversation Address;
- **Turn Receipt** — the content-free identity and lifecycle record for one
  accepted Turn;
- **Ingress Idempotency Binding** — the durable mapping from one retryable
  source submission to its canonical Turn ID and request fingerprint.

“Sole authority” is a logical ownership rule, not yet a physical database
choice. The concrete database, driver, file layout, transaction API and
migration mechanism require a later decision and implementation design. They
must preserve this ownership boundary rather than distributing these records
back across Surfaces or projections.

The singleton Owner does not require a synthetic Owner row in the first
implementation. Owner Identity configuration remains ingress configuration and
Source Attribution, not a state partition or Project owner key.

### Project is a first-class control-plane aggregate

A **Project** is one durable research-continuity aggregate for the Owner,
identified by an opaque Project ID. Bench investigation threads, Conversations,
Memory knowledge and Runs refer to that ID; none of them creates another kind
of Project.

Project creation first commits a Project Record through Control Plane State.
A Conversation may be unbound or reference exactly one existing Project Record
under the immutable binding rule. No Surface, Memory write, output-directory
scan or display name may fabricate a Project identity implicitly during normal
runtime operation.

Project rename, archive, restoration or deletion—when their detailed lifecycle
semantics are defined—must begin as a Control Plane State transition. Other
stores follow that transition or are reconciled to it; they do not win a
last-writer contest against the Project Record.

### Memory and output are associated content and projections

The `project://<project_id>` Memory subtree holds Project knowledge, research
context and lineage. Its content is durable and valuable, but the subtree is
not the Project existence registry and does not own the Project's current name
or lifecycle. Missing or orphaned legacy Memory needs explicit migration or
reconciliation; runtime recall does not silently create a Project Record.

`project_meta.json` remains the output subsystem's durable path-local mapping
between a Project ID and its frozen output directory. It may mirror the current
display name for human navigation and supports directory scanning/reindexing,
but it cannot create, rename, archive or delete the Project domain object. A
stale display name is repaired from Control Plane State without moving the
frozen directory.

Run manifests, Run indexes, `analysis://` Memory and Desktop `run_meta` rows
remain projections or associated execution records under their existing
owners. They reference the canonical Project ID and cannot redefine it.

### Specialized content stores remain separate owners

Control Plane State does not absorb all Owner data:

- Transcript storage owns ordered provider-visible Conversation content and
  its storage-side Turn attribution;
- graph Memory owns learned knowledge, preferences and research context;
- attachment and tool-result stores own their file/blob content;
- Run storage owns execution records, manifests and artifacts;
- Turn Execution owns process-local scheduling, cancellation, approval and
  Event observation capabilities.

These stores reference control-plane IDs and participate in lifecycle fan-out
or reconciliation, but they do not become identity registries. Sharing a future
physical database does not merge their logical ownership or permit one module
to write another module's tables directly.

### Consistency and recovery direction

Operations that require atomic identity changes—Project creation, Conversation
creation and first Project binding, Active Conversation Binding replacement,
and Turn Receipt plus Ingress Idempotency Binding acceptance—must transact
inside Control Plane State.

Updates to Memory or filesystem projections may follow outside that transaction
because those stores have different durability mechanisms. A projection failure
must leave an observable repairable mismatch, never cause the projection to
become the new authority. Exact outbox, retry, tombstone, deletion and retention
mechanisms remain implementation/lifecycle decisions beneath this ownership
rule.

This ADR does not decide whether the reserved `default` Run grouping becomes a
Project Record or how an existing Run may be reassigned. Those questions cannot
change the rule that any real Project identity and lifecycle comes from Control
Plane State.

## Consequences

- Project, Conversation and Turn references have one canonical validation
  boundary across CLI, Desktop, Channel, Bench, Memory and Run output.
- A Project rename or lifecycle change cannot diverge permanently because one
  projection happened to write last.
- `project://` knowledge and `project_meta.json` retain their useful specialized
  roles without acting as competing Project registries.
- Control-plane transactions can enforce Conversation, active-binding, Turn
  receipt and ingress-idempotency invariants in one logical owner.
- Existing Bench, CLI Session, Channel composite-key, Memory and output records
  require an explicit migration and reconciliation plan before legacy stores
  can be retired.
- Selecting the physical persistence layout is now the next blocking decision;
  this ADR deliberately does not choose SQLite, reuse an existing database or
  introduce another file.

## Rejected alternatives

- **Keep `project://` Memory as the Project registry.** Rejected because Memory
  knowledge has different write, recall, versioning and deletion semantics from
  control-plane identity and lifecycle.
- **Treat `project_meta.json` as the Project record.** Rejected because Projects
  may exist before any Run directory, and a filesystem projection cannot
  transact with Conversation and Turn admission.
- **Let each Surface own its Project and Conversation records.** Rejected because
  cross-Surface continuity would require reconciliation among competing
  identities and recreate composite transport keys.
- **Infer Projects by scanning Memory and output directories at startup.**
  Rejected because absence and partial writes are ambiguous, display metadata
  may conflict, and reconstruction cannot enforce atomic binding rules.
- **Put every content store under one undifferentiated repository.** Rejected
  because one physical deployment choice must not erase the ownership boundaries
  among control facts, Transcript content, Memory knowledge and Run artifacts.
