# Persist authoritative control state in a Backend-exclusive local SQLite database

## Status

Accepted (2026-07-14).

Refines
[ADR 0040](0040-restart-resilient-transcript-persistence.md),
[ADR 0043](0043-local-first-control-plane-extensible-run-execution.md),
[ADR 0048](0048-resolve-conversation-with-active-reply-target-binding.md),
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md),
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md), and
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md).

This ADR selects the physical persistence layout deliberately left open by ADR
0053. It does not decide Project archive/delete semantics, the `default` Run
grouping lifecycle, projection outbox policy, or automated retention.

**Project-lifecycle refinement (2026-07-14):**
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md)
resolves the Project portion of that deferral. `control.db` stores `active` or
`archived` plus the transition timestamp and revision; restore is a transition
back to `active`, while permanent purge remains a separate future workflow.

**Run-scope refinement (2026-07-14):**
[ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md)
resolves the Run-grouping deferral. The control plane validates and freezes a
typed Project or Unassigned Run Scope before execution; `control.db` does not
store Run manifests or create a Project Record for `default/`. Migration maps
provable legacy `default` Runs to Unassigned and reports unknown pseudo-Project
identifiers instead of inventing Projects.

**Run-receipt refinement (2026-07-14):**
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md)
adds minimal Run Receipts to `control.db` because accepted identity, immutable
Scope, operational lifecycle and Project-busy queries are control facts. This
does not reverse the narrow-store decision: executable Run Requests, parameters,
logs, Manifests and artifacts remain outside the Control Database.

**Run-submission and assignment refinement (2026-07-14):**
[ADR 0058](0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md)
adds durable Run Submission Bindings and narrow fenced Execution Assignment
facts to `control.db`. They prevent duplicate acceptance, executor starts and
stale callbacks without storing executable payloads or turning the database
into a queue.

**Attachment-store refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
preserves this ADR's narrow physical boundary: Attachment Records, provisional
staging metadata and content-addressed Blobs remain in the Attachment Store,
not `control.db`. The Attachment Store recognizes acceptance only from the
authoritative committed Turn Receipt.

**Outbound-delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
resolves the terminal Channel delivery portion of the Outbox deferral.
`control.db` stores Outbound Delivery identity, Item-plan references, attempts
and lifecycle so Delivery creation can be atomic with Turn terminalization; it
still stores no reply text, media bytes, provider credential or SDK object.

**Projection-fencing refinement (2026-07-15):**
[ADR 0064](0064-scope-scientific-memory-and-fence-project-projections.md)
adds content-free Project Projection Intents. They authorize exact idempotent
cross-store projection of already accepted work without making scientific
Memory content part of the Control Database.

## Implementation

Repository foundation and the Scheme 1 conversational runtime slice are
implemented (2026-07-16). `omicsclaw/control/` creates the versioned
strict-SQLite `control.db`, holds a fail-closed lifetime advisory lock, verifies
migration checksums/foreign keys/integrity/private paths, and exposes atomic
typed domain commands with fault checkpoints. A real parent/child-process
regression proves the lifetime lock rejects a second OS process while the owner
lives and permits it after release. Historical schema/policy migrations remain
immutable and pinned; later migrations add immutable terminal Transcript refs,
closed legacy-import state transitions, cutover identity and one opaque
Transcript Store binding without moving Transcript bodies into Control.

`ControlRuntime` now makes these records production authority for
prompt-toolkit/single-shot CLI and Desktop text/multipart-image Conversations/Turns. The
independent canonical `transcripts.db` owns immutable content and active order;
Control knows only its opaque Store identity, cutover/baseline evidence and the
terminal entry ID/digest committed with each terminal Receipt. Startup verifies
that cross-store identity and every terminal reference before ingress. The
profile-driven legacy Backend Transcript importer runs offline under the same
lifetime lock, requires exact plan-manifest evidence, publishes a fully verified
staged Store atomically, records `planned -> validated -> committed`, and leaves
legacy storage as a read-only backup with no runtime fallback.

Scheme 2 extends this authority to Owner-only Telegram text and persistent
Delivery; Scheme 3 binds the independent Attachment Store identity and commits
content-free per-Turn attachment manifests atomically with Turn acceptance.
The Desktop `POST /v1/runs`, exact prompt-toolkit demo command and Remote
exact-demo compatibility Adapter now also persist canonical Run Receipts,
Submission Bindings and the sole Execution Assignment through this Repository;
their scientific Manifest and artifacts remain in the separate Run Store.

The Desktop composition root resolves one existing absolute Active Workspace
once per Backend lifespan and freezes it with the sole `ControlRuntime` and
`RunRuntime`. Every Remote compatibility Adapter that requires Workspace
state—including Jobs, Artifacts, Datasets and Env—consumes only that
process-local binding; none re-resolves mutable environment state per request.
Bearer-policy-gated `GET /workspace` reports the frozen root. A same-root `PUT`
authenticates before reading a bounded strict JSON command and is an idempotent
no-op, while every different absolute root fails closed with
`workspace_change_requires_backend_restart` and cannot mutate environment,
trusted directories, output roots or either Runtime. This binding anchors
compatibility-state storage and Runtime composition; it is not filesystem
confinement. The retained Remote Session-resume route is a fixed compatibility
tombstone, intentionally requires no binding, and reads neither Workspace nor
legacy Job JSON. Legacy Chat Job submission and `/chat/stream` binding are also
retired; historical active Chat rows are read-only interrupted projections.
Remote Linux compatibility-state mutation holds no-follow directory handles
through commit or deletion and fails closed where those primitives are absent;
explicit imported Dataset source paths may remain outside the Workspace.

This remains a vertical slice. Textual TUI and non-Telegram Channel Adapters
still use Surface-specific legacy Conversation paths; root `oc run`, broader
Run kinds, the Memory projector, CLI `sessions.db`, Desktop App exports and
other legacy state require explicit cutover. The separate Backend Transcript,
CLI Session, graph Memory, Run Store and Desktop App stores are therefore not
being collapsed into `control.db`; the lifespan Workspace binding is process
configuration, not a second durable control-state owner.

## Context

ADR 0053 established one logical owner for Project, Conversation, active
binding, Turn-receipt and ingress-idempotency facts. OmicsClaw must now choose a
physical store that can atomically enforce those relationships without making
a Surface, content store or compute Worker authoritative.

The existing databases are unsuitable as that boundary:

- graph `memory.db` may be SQLite or PostgreSQL and owns versioned knowledge,
  Namespace and search semantics rather than mandatory ingress state;
- `transcripts.db` is a growing write-through content mirror with independent
  byte-identity, compaction, retention and ToolResult lifecycle requirements;
- CLI `sessions.db` belongs to one Surface;
- Desktop App `omicsclaw.db` belongs to the UI process, contains UI settings
  and credentials, and may be physically separated from a remote Backend.

A monolithic database would reduce the number of files but enlarge the failure,
retention and migration domain. PostgreSQL or a distributed control store would
add operational and cross-process semantics rejected for the local-first
single-process control plane.

## Decision

### One mandatory Backend-owned Control Database

Each OmicsClaw Backend control plane uses exactly one local SQLite file named
`control.db` beneath its resolved Backend state root. **Control Database** is the
physical implementation of authoritative **Control Plane State**.

`control.db` is mandatory whenever the conversational control plane starts. A
pure Skill catalog/help operation need not initialize it. Project-aware Run
Requests validate Project identity through the control plane; local or remote
compute Workers never open `control.db` themselves.

Only the Backend control-state Repository may read or write its tables. CLI,
Desktop, Channel Adapters, the Desktop App process, Memory, Transcript storage,
Run storage and compute Workers use typed control-plane Interfaces rather than
opening the file or issuing SQL.

### Physical contents stay narrow

The Control Database persists only the tables required by the accepted logical
owner and its migration mechanism:

- Project Records;
- Conversation Records;
- Active Conversation Bindings;
- Turn Receipts;
- Run Receipts;
- Ingress Idempotency Bindings;
- Run Submission Bindings;
- fenced Execution Assignments;
- Outbound Deliveries, ordered Items and Delivery Attempts;
- content-free Project Projection Intents;
- ordered schema-migration records;
- an auditable Legacy Identity Map used to make import idempotent and preserve
  old deep links during migration.

The first Project schema remains minimal: opaque identity, current display
metadata, a projection-friendly revision, and timestamps. Scientific fields
such as organism, domains, platforms and venue remain Project Memory. Lifecycle
columns beyond facts already accepted are added only after Project archive,
restore and delete semantics are decided.

`control.db` does not store Transcript messages, serialized Inbound Envelopes,
prompt or response content, attachments, SSE Event history, live execution
capabilities, executable Run payloads, ToolResult bodies, Memory knowledge, Run
manifests/artifacts, credentials, or a synthetic Owner row.

### SQLite durability and single-process ownership

The initial implementation uses Python's standard-library `sqlite3`, not the
Memory subsystem's SQLAlchemy engine. This keeps mandatory control state
independent of optional Memory dependencies and makes transaction boundaries
explicit.

The Repository configures SQLite with WAL journaling, `synchronous=FULL`,
foreign-key enforcement and a bounded busy timeout. Atomic control writes use
explicit transactions such as `BEGIN IMMEDIATE`. The database directory and
file are owner-private. Schema changes use ordered, checksum-visible migration
records and create a consistent SQLite backup before a destructive migration.

One cross-platform OS advisory lock is held for the lifetime of the Backend
control plane. A second process attempting to own the same `control.db` fails
closed; it does not become another control-plane writer. The lock must be
released by the operating system on process exit and must not rely only on a
stale PID or lock-file existence check.

The Repository owns connection and locking details and exposes atomic domain
operations rather than raw CRUD or a shared SQL session. At minimum those
operations cover Project creation/rename, Conversation creation and one-time
Project binding, Active Conversation Binding replacement, novel/duplicate Turn
acceptance, Turn lifecycle transitions, novel/duplicate Run admission, fenced
Execution Assignment creation and reports, Run lifecycle transitions, and
startup interruption reconciliation.

### Stores remain physically and logically separate

`transcripts.db` remains the Transcript content store; graph `memory.db` or its
configured PostgreSQL database remains the Memory store; ToolResult and
attachment bodies remain file/blob content; Run manifests and artifacts remain
under output storage; Desktop App `omicsclaw.db` remains an App-owned settings
and UI projection/cache store.

No foreign key is attempted across these physical stores. Coordination follows
the accepted ownership order:

- control identity is committed before Memory or filesystem projections;
- a missing or stale projection is observable and repairable but never becomes
  authority;
- verified terminal Transcript candidate content exists before a Turn Receipt
  is marked terminal; after that control transaction, the active Transcript
  view is promoted and the process-local terminal Event is published, so
  recovery can finish presentation without rerunning the Turn;
- Event replay remains bounded and process-local.

Sharing one physical file would not make terminal Event publication
transactional, so it is not a reason to merge Transcript content into the
Control Database.

### Explicit, auditable migration without legacy fallback

Migration is a versioned import and cutover, not runtime inference. Before
mutation it produces a dry-run inventory and consistent backups. Applying it
records source, legacy identity, canonical identity, status and conflicts so a
retry is idempotent.

Migration follows these rules:

- valid, non-conflicting opaque Project IDs generated by the existing Backend
  Bench service may be preserved to avoid rewriting Memory and Run references;
- orphaned or conflicting `project://` and `project_meta.json` candidates are
  reported for explicit Owner resolution rather than silently merged;
- legacy Desktop, CLI and Channel Session keys receive new control-generated
  opaque Conversation IDs plus Legacy Identity Map entries;
- Project binding is imported only from an explicit valid legacy reference,
  never guessed from display name or Workspace;
- ambiguous Active Conversation Bindings remain unset;
- historical messages do not receive fabricated Turn Receipts or Turn IDs;
- the Ingress Idempotency Binding store starts empty because legacy TTL caches,
  content and timestamps cannot prove source-request identity;
- an exact Backend Transcript is preferred over Surface presentation history;
  a fallback legacy transcript snapshot is imported explicitly and causes one
  known cache re-warm rather than timestamp-merging competing histories;
- remote Desktop migration uses a versioned import Interface; the Backend does
  not read the App's local SQLite file directly.

After cutover, new writes go only through the new owners. A legacy store may
remain as a read-only backup or UI cache, but ordinary runtime lookup must not
fall back from a missing Control Database record to Memory, `project_meta.json`,
CLI `sessions.db`, Desktop `omicsclaw.db` or a transport composite key. Recovery
is an explicit migration/reconciliation operation, never implicit identity
creation.

The Desktop App retains its own UI row identity and stores the canonical
Backend Conversation ID as a reference. Its message rows may serve as an
offline presentation cache after cutover but do not establish Conversation or
Project existence.

### Startup and corruption behavior

Control-plane startup acquires the lifetime ownership lock, opens and migrates
the database, performs an integrity check appropriate to the small control
store, and reconciles process-owned non-terminal receipts before accepting new
ingress. Turn receipts become `interrupted`; Run receipts may first terminalize
from immutable completion evidence under ADR 0057 and otherwise become
`interrupted`.

A missing database is initialized. A corrupt or incompatible existing
`control.db` fails closed with recovery guidance; it is never deleted and
reconstructed automatically from projections. Backup/restore treats
`control.db` as critical Owner data.

## Consequences

- All control identities and acceptance invariants share one local transaction
  boundary without introducing a distributed queue or database service.
- The execution plane remains horizontally replaceable because Workers receive
  Run Requests and never own control persistence.
- The most critical, compact state has a smaller corruption, backup and
  retention domain than Transcript or Memory content.
- Desktop local and remote modes use the same Backend authority instead of
  making the UI database authoritative.
- Existing Session and Bench data require explicit import tooling and a
  compatibility cutover; a permanent dual-read fallback is forbidden.
- The system has more than one database file by design. File count is not the
  architectural boundary; ownership and transaction semantics are.
- SQLite is sufficient only while the accepted single-control-process model
  holds. A future multi-process control plane must create a new ADR and replace
  this ownership/locking design rather than sharing the file opportunistically.

## Rejected alternatives

- **Reuse graph `memory.db`.** Rejected because Memory can be optional/remote
  and has knowledge, Namespace, FTS and versioning semantics unrelated to
  mandatory ingress transactions.
- **Reuse `transcripts.db`.** Rejected because content growth, compaction and
  retention must not enlarge the failure domain of Project identity and ingress
  idempotency.
- **Reuse Desktop App `omicsclaw.db`.** Rejected because it is a Surface/UI
  store, may be on another machine, and is unavailable to CLI and Channel.
- **Keep CLI and Surface-specific Session databases authoritative.** Rejected
  because it preserves competing Conversation identities.
- **Put all Backend content in one SQLite file.** Rejected because it couples
  unrelated ownership, backup, corruption and retention lifecycles without
  making process-local Event publication atomic.
- **Use PostgreSQL now.** Rejected because it adds a service and distributed
  operational model with no requirement under the local-first single-process
  control plane.
- **Use JSON files or directory scanning.** Rejected because they cannot enforce
  atomic multi-record acceptance, foreign keys and idempotency uniqueness.
- **Keep a runtime fallback to legacy stores.** Rejected because fallback would
  silently recreate the multi-authority design that ADR 0053 removed.
