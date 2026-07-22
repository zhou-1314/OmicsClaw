# Scope scientific Memory and fence Project projections

## Status

Accepted (2026-07-15).

Refines
[ADR 0045](0045-owner-identity-is-not-a-state-partition.md),
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md),
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md),
and
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md).

## Implementation

Projection-fence repository foundation implemented (2026-07-15); the full
projection path — Producer at terminalization, frozen-source reader, background
trigger, and application into Project-scoped Memory — implemented, wired into the
Desktop backend, and tested end-to-end (2026-07-22). Only the scope /
dataset-identity migration of the *ad-hoc* Memory write path remains.

The ControlStateRepository stores immutable, digest-bound Project Projection
Intents atomically with Turn/Run terminal state and permits idempotent terminal
application after archive; `list_pending_projection_intents` sweeps them
cross-Project and lifecycle-blind. `omicsclaw.memory.scientific_scope` defines
the explicit Owner / Project / Workspace / Conversation / Run / system scope
vocabulary and the canonical `(workspace_id, normalized_relative_path,
content_sha256)` dataset-observation identity, plus a distinct provisional
`(observed_size, observed_mtime_ns)` identity that never dedups against a
settled one. `omicsclaw.memory.projection` applies a pending Intent idempotently
— digest-verified, source-loss-safe, restart-safe, and archive-independent — in
both a sync and an async (`aapply_projection_intent`) form;
`omicsclaw.memory.projection_driver` drives a pending sweep (sync + async) with
per-Intent transient-fault deferral; and `omicsclaw.memory.projection_writer`
lands the frozen projection in an explicit `project/<project_id>` namespace at
`projection://<kind>/<intent_id>` (overwrite-mode, idempotent by Intent ID) —
the first explicit-scope write target.

The production path is now closed. A project-scoped Run that terminalizes as
succeeded freezes exactly one `analysis_lineage` Projection Intent in the SAME
control transaction that terminalizes it (`RunRuntime` attaches it to the
success `RunReport`; the frozen source is the immutable Manifest, the digest the
canonical `omicsclaw.control.projection_payload` derivation). A
`RunManifestSourceReader` re-derives those bytes from the Manifest, and a
`MemoryProjectionService` background sweep — wired into the Desktop backend
lifespan next to the bridge task and cancelled before its repository / Run Store
/ Memory engine dependencies close — applies pending Intents into
`project/<project_id>` Memory, including after archive. Proven end-to-end over a
real `control.db` + `MemoryEngine`.

Still absent: only the scope / dataset-identity migration of the *ad-hoc* Memory
write path. Current Memory Namespaces for direct writes still mix Workspace,
Desktop launch, and Channel sender identities; `_auto_capture_dataset` can still
create transport-partitioned `dataset://` records; and the explicit scope
vocabulary is not yet enforced on those ad-hoc writes (novel Project-scoped
mutation is transitively fenced only because Turn admission rejects archived
Projects, not by a Memory-layer gate).

## Context

ADR 0045 rejected Owner Identity as a state partition but left Dataset Memory
ownership open. ADR 0055 says an archived Project rejects new scientific Memory
mutation, while content stores remain separate from `control.db` and may need
repair after a Turn or Run has already terminalized.

Those rules race. A terminal Run can durably commit valid scientific evidence,
then Project archive can succeed before a best-effort Memory projection runs.
Rejecting the projection loses research continuity; allowing any post-archive
write makes archive ineffective. Transport-keyed Namespaces also cannot decide
whether the same local dataset mentioned through Telegram and Desktop is one
scientific object.

The architecture needs explicit scientific ownership and a narrow durable
authorization for delayed, idempotent projection. It does not need to merge
Memory content into the Control Database.

## Decision

### Scientific Memory uses explicit domain scopes

Target Memory writes resolve one of these scopes before mutation:

- **Owner scope** — preferences, persona, and Owner-wide non-scientific
  settings;
- **Project scope** — hypotheses, insights, analysis lineage, and references
  that make scientific content part of one Project's continuity;
- **Workspace scope** — a catalog of observations about authorized pre-existing
  local files;
- **Conversation, Run, or system scope** — facts whose existing domain owner is
  already explicit.

Owner Identity, Source Namespace, Channel sender ID, Desktop launch ID, and a
legacy Memory Namespace never select these scopes. Namespace remains a
migration/implementation field until the Memory schema represents the explicit
owner directly.

### Canonical dataset observations belong to Workspace or Attachment identity

A pre-existing local file is represented by a Workspace-scoped dataset
observation. Its version identity is based on:

```text
(workspace_id, normalized_relative_path, observed_content_sha256)
```

When a digest has not yet been established, `(observed_size,
observed_mtime_ns)` may identify a provisional observation but MUST NOT merge it
with another path or claim byte equality. Display filename alone never dedups a
dataset.

Uploaded bytes remain canonically identified by their immutable Attachment
Record and content digest. A Project may create a Project-scoped Dataset
Reference to a Workspace observation or Attachment Record; multiple Projects
may reference the same source without moving it or rewriting provenance.

Unassigned Runs record input provenance in their Run Manifest and may update a
Workspace observation, but they create no Project Dataset Reference until a
separate active-Project action explicitly cites that evidence.

### Novel Project Memory mutation requires an active Project

Every new Project-scoped hypothesis, insight, analysis relation, or Dataset
Reference checks the authoritative Project Record. An archived Project rejects
novel mutation with `project_archived`. A Memory URI, existing projection, or
output directory cannot bypass that gate.

### Accepted work may create one frozen Projection Intent

Before a Project-bound Turn or Run is allowed to make a Project-scoped Memory
effect, the control plane records a content-free **Project Projection Intent**
while the Project is active. The intent freezes:

- opaque intent ID and Project ID;
- origin kind and canonical Turn or Run ID;
- projection kind and schema version;
- authoritative source-store reference and content digest;
- lifecycle `pending | applied | failed` and timestamps.

The source content remains in Transcript, Run, Attachment, or other specialized
storage. The Control Database stores only the identity, fence, digest, and
operational projection state needed for idempotency and archive semantics.

When the projection is derived from terminal Turn content or Run completion
evidence, its Intent is inserted in the same control transaction that
terminalizes that Turn or Run. Archive therefore observes either nonterminal
work, or terminal work with its complete frozen projection authority; it can
never commit in the gap between those two facts.

A projector may apply a matching pending Intent after the Project becomes
archived. That is completion of already accepted work, not novel scientific
mutation. It must write exactly the frozen projection, be idempotent by intent
ID, verify the source digest, and then mark the Intent applied. It cannot derive
additional hypotheses, follow new references, or broaden scope after archive.

Project archive does not wait for pending Projection Intents and does not
cancel them. It still rejects new intents after its lifecycle transaction
commits. A mismatched source, digest, Project, or origin fails the Intent and
requires explicit repair; it never falls back to a legacy Namespace write.

Administrative projection repair that changes no scientific content may run
without an Intent. Any repair that would add or change Project-scoped
scientific content requires either the original matching Intent or Project
restore and a new active-state operation.

## Consequences

- Mentioning one dataset through different Owner Identities no longer forks its
  scientific identity.
- Dataset reuse across Projects is represented as references rather than
  duplicated or retagged canonical records.
- Project archive closes novel scientific work without losing a delayed
  projection of work accepted while active.
- `control.db` gains a narrow projection-intent table but still stores no
  scientific Memory content.
- Memory migration must map provable legacy dataset rows to explicit scopes and
  report ambiguous Namespace-derived ownership instead of guessing.
- Projection tests must cover archive races, duplicate application, digest
  mismatch, source loss, restart, and rejection of broadened post-archive
  mutation.

## Rejected alternatives

- **Make Dataset Memory Owner-wide by filename.** Rejected because equal names
  across Workspaces do not prove equal scientific content.
- **Make every dataset record Project-owned.** Rejected because one authorized
  local dataset may be reused by several Projects without duplicated identity.
- **Keep per-Surface or per-sender Namespace ownership.** Rejected because
  transport identity is not scientific ownership.
- **Reject every Memory write after archive, including accepted-work repair.**
  Rejected because a cross-store crash could permanently omit valid completed
  research from Project continuity.
- **Allow unrestricted post-archive projection.** Rejected because it would
  turn archive into a UI flag rather than an admission fence.
- **Store the projected scientific content in `control.db`.** Rejected because
  the narrow control store owns authorization and lifecycle, while Memory and
  scientific stores own content.
