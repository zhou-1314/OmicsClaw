# Keep unassigned Runs outside Project lifecycle and freeze Run Scope

## Status

Accepted (2026-07-14).

Refines
[ADR 0035](0035-project-scoped-output-directories.md),
[ADR 0043](0043-local-first-control-plane-extensible-run-execution.md),
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md),
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md), and
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md).

It preserves ADR 0035's Project-scoped output layout and literal `default/`
directory for compatibility, but supersedes its description of that directory
as a "default project". The directory is a non-Project storage grouping.
It also resolves ADRs 0035 and 0053's deferral about later Run reassignment:
the Run's accepted scope is immutable in v1.

**Run-record ownership refinement (2026-07-14):**
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md)
sharpens the overloaded "durable Run record" language below. Control Plane
State owns the minimal Run Receipt, including accepted Scope and operational
lifecycle; Run storage owns the scientific Run Manifest and artifacts. The
Control Database still does not absorb executable requests, parameters, logs,
Manifests or artifact content.

**Run-submission refinement (2026-07-14):**
[ADR 0058](0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md)
requires an accepted duplicate to return the original immutable Run Scope.
Explicit retry creates a new linked Run with the same Scope and revalidates an
active Project; equivalent work under another Project is an ordinary new Run,
not a scope-changing retry.

## Implementation

Canonical Simple Skill tracer and four production submission Adapters
implemented (2026-07-17), with root explicit Scope added on 2026-07-18;
broader caller migration remains incomplete.
`omicsclaw/control/run_contract.py` now exposes the
closed `ProjectScope | UnassignedScope` union; `RunRuntime` validates an active
Project before novel acceptance and persists the flattened immutable Scope in
the atomic Run Receipt. `FilesystemRunStore` writes Project Runs below their
Project output directory and explicit Unassigned Runs below the literal
`default/` grouping without creating a `default` Project identity. The
versioned Desktop `POST /v1/runs` Adapter exercises both Scope variants. The
prompt-toolkit REPL's exact `/run <canonical-skill> --demo` shape is the second
Adapter and always freezes `UnassignedScope`; it does not consult legacy CLI
Project navigation or create a Project.

The root exact-demo command family is another typed CLI Adapter. It accepts
only the fixed-order omitted-Scope wire, `--demo --project <32-lower-hex-id>`,
or `--demo --no-project`. Only the omitted selector reads the legacy
current-Project pointer as a bounded, side-effect-free opaque navigation hint,
then asks `RunRuntime` to validate it against Control. An active canonical
Project freezes `ProjectScope`; a missing, malformed, stale or archived hint
freezes `UnassignedScope` without creating a Project or
`default/project_meta.json`. Explicit Project freezes `ProjectScope` directly
and novel admission returns `project_not_found` or `project_archived` without
downgrade; explicit Unassigned never reads current navigation. Final acceptance
revalidates Project lifecycle in the same authoritative transaction.

This root slice deliberately rejects `--project default` as
`invalid_project_id`; it does not retain the optional migration alias described
by the target decision. Control-backed `project new/use/list/current`, root
non-demo execution, demo input/output/parameter options, prompt-toolkit Scope
selectors, Remote Project Scope, Textual TUI, Agent, Workflow and Autonomous
remain outside this slice. It therefore does not claim ADR 0056 complete.

These are intentionally narrow vertical slices. Root non-demo and unsupported
option-bearing forms, Textual TUI, non-demo and option-bearing prompt-toolkit
`/run` forms, Agent tool, Workflow, Autonomous and broader Remote Job shapes
still use older stringly `project_id` and output-resolution conventions; some
legacy helpers still write `default/project_meta.json` or derive identity from
directory leaves. Those paths are implementation drift and are not claimed
compliant until migrated behind the same `RunRuntime` Interface.

## Context

Some OmicsClaw executions are intentionally useful before the Owner creates or
selects a research Project: a one-off CLI inspection, a demo, or a chat in an
unbound Conversation should still be able to produce a durable Run. ADR 0035
gave these Runs the convenient physical location `<output root>/default/`.

Treating `default` as a Project ID would now violate the authoritative Project
model. A real Project has a control-generated opaque ID, an authoritative
Project Record, an `active` or `archived` lifecycle, and research-continuity
semantics across Conversations, Memory and Runs. The fallback bucket has none
of those properties and should not acquire them merely because a directory
exists.

The current use of a nullable or stringly typed `project_id` also permits
unrelated identifiers to masquerade as Projects. An empty string, the literal
`default`, a Session ID, Chat ID, filesystem directory name or user-facing
Project name can cross the Worker seam without proving that the Project exists
and is active.

Run reassignment is not a harmless metadata edit. The Run directory, manifest,
per-group index, `analysis://` Memory, Desktop `run_meta`, downstream artifact
references and user scripts can retain its original path or Project ID.
Moving, retagging, copying or symlinking one representation would create a
second account of the same historical execution and weaken scientific
provenance.

## Decision

### Every Run receives one typed immutable Run Scope

At admission, the single-process control plane resolves exactly one value:

```text
RunScope =
  ProjectScope(project_id)
  | UnassignedScope
```

`ProjectScope(project_id)` requires an existing `active` Project Record and its
opaque control-generated Project ID. `UnassignedScope` explicitly states that
the Run does not belong to a Project; it contains no synthetic Project ID.

The control plane validates and freezes the value before submitting the typed
Run Request to the Run Executor facade. A local or remote Worker receives the
resolved Run Scope and cannot derive, replace or validate Project identity by
opening `control.db`, scanning output directories or interpreting other IDs.

The following values are never Project identity:

- `"default"`, an empty string or null-like sentinel;
- Session ID, Conversation ID, Chat ID, thread/topic ID or Turn ID;
- Project display name, slug, output-directory name or `project_meta.json`
  presence;
- Workspace, Namespace, Owner Identity or Surface-local row ID.

Control Plane State owns the minimal Run Receipt table, including immutable
Scope and operational lifecycle. Run storage owns the scientific Manifest and
artifacts. The control plane validates a referenced Project and its lifecycle
at admission but does not absorb executable requests, parameters, logs,
Manifests, or artifact content.

### Conversational Runs derive scope from the Conversation binding

A chat-triggered Run cannot independently select a Project. Its scope is
derived from the immutable Project binding of the Conversation whose Turn
caused it:

```text
Conversation bound to active Project  -> ProjectScope(project_id)
Conversation unbound                  -> UnassignedScope
Conversation bound to archived Project -> reject novel Turn as project_archived
```

The Turn and Run retain their own identities; the Run may record the causing
Turn ID as provenance without encoding that ID into Run Scope. If an initially
unbound Conversation is later first-bound to a Project under ADR 0048, only
future Runs receive that Project Scope. Earlier Runs remain Unassigned.

An explicitly non-conversational Run Request must choose either a validated
active Project or explicit Unassigned. It never creates a Project as a side
effect.

### CLI selection is navigation, not identity creation

The target CLI follows these semantics:

- `project new` asks the control plane to create a Project and returns its
  opaque ID; it does not mint a slug as identity;
- `project use` selects an existing active Project for future requests;
- `run --project <id>` accepts only an existing active Project ID;
- `run --no-project` explicitly requests `UnassignedScope`;
- omitting both uses a still-valid current Project, otherwise Unassigned;
- `--project default`, if retained during migration, is a deprecated one-way
  alias for Unassigned and never resolves to a Project Record.

A current-Project pointer is local navigation state. It cannot establish that a
Project exists, override an archived state or change any already accepted Run.

### `default/` is the Unassigned Run Grouping

The canonical product and domain term is **Unassigned Run Grouping**. Its
steady-state filesystem projection remains:

```text
<output root>/default/<run_id>/
```

The literal path is retained to avoid unnecessary layout churn and keep one-off
Runs easy to find. It has no Project Record, Project ID, Project lifecycle,
Conversation binding, `project://` namespace or Project display metadata. It is
shown separately as **Unassigned Runs** and is excluded from Project lists.

The Unassigned Run Grouping may contain its rebuildable `index.jsonl`, but it
does not contain an authoritative `project_meta.json` in the steady state.
Retention or cleanup of Unassigned Runs belongs to future Run-retention policy,
not to Project archive, restore or purge.

### Manifest and index encode the union explicitly

Every newly accepted Run manifest and derived index row records the normalized
scope without overloading a string sentinel:

```text
scope_kind = "project" | "unassigned"
project_id = <opaque Project ID> | null
```

The invariant is:

```text
scope_kind == "project"    iff project_id is a valid opaque Project ID
scope_kind == "unassigned" iff project_id is null
```

The Run Receipt is authoritative for accepted scope and operational lifecycle;
the Manifest repeats that scope as immutable scientific provenance, and any
mismatch is an integrity fault. The per-group index is a rebuildable view.
Compatibility serializers may emit a deprecated legacy field or accept the old
`"default"` sentinel at a boundary, but internal domain code and new storage
never treat it as a Project ID.

Listing and filtering use `scope=unassigned` or a validated `project_id`, not
`project=default`. Exact route and field spelling may evolve while preserving
the union and invariant.

### Run Scope cannot be reassigned after admission

Once a Run Request is accepted, its Run Scope is historical provenance and is
immutable. Project selection affects future Runs only. v1 provides no
operation that:

- moves an Unassigned Run into a Project output directory;
- retags `project_id` in a manifest, index, Memory row or App cache;
- copies a Run and calls the copy the same execution under another Project;
- symlinks a Run into a Project tree to imply membership;
- detaches a Project-scoped Run into Unassigned or another Project.

Project archive and restore do not mutate the scope of completed or historical
Runs. The lifecycle gate only controls admission of new Project-scoped work.

Migration and reconciliation are not product reassignment. They may repair an
incorrect legacy projection to the one provable original scope while recording
evidence, but cannot offer ordinary post-hoc reclassification.

### Future Project reuse is a reference, not provenance mutation

If later research workflows need to cite an existing Run from another Project
or from Unassigned, a future ADR may introduce a separate **Project Run
Reference** from `Project ID -> Run ID`. Such a reference would mean "this
Project uses this prior evidence", not "this Run was originally executed in
this Project". It may support multiple Project references without changing the
Run's manifest, directory or Run Scope.

Project Run Reference is not part of v1 and is not implied by UI pinning,
copying, symlinking or editing `project_id`.

### Legacy output is imported without inventing Projects

ADR 0054's migration inventory applies these additional rules:

- Runs under literal `default/`, root-level legacy Runs and manifests whose
  `project_id` is exactly `"default"` import as `UnassignedScope`;
- a legacy `default/project_meta.json` is ignored as Project evidence and may
  be removed only after inventory and backup;
- a Run imports as `ProjectScope` only when its legacy reference maps
  explicitly and unambiguously to one canonical Project Record;
- slug, Session, Chat, Conversation or unknown identifiers without such a
  mapping are reported as conflicts and never auto-create Projects;
- migration does not move Run directories; compatibility indexes may point to
  an inventoried legacy location while every new Run uses the canonical layout;
- compatibility reads are one-way and time-bounded; normal runtime never falls
  back to directory inference after cutover.

The migration report records the inferred scope, evidence, conflicts and any
legacy location so retries are idempotent and ambiguous scientific provenance
remains visible for Owner resolution.

## Consequences

- One-off and unbound analysis remains possible without polluting the Project
  registry or forcing premature Project creation.
- Every Project-associated Run is validated against one authoritative active
  Project before execution, including on a remote execution plane.
- The literal `default/` layout remains compatible while no longer weakening
  opaque Project identity or Project lifecycle semantics.
- Historical provenance is stable: later navigation and organization cannot
  silently rewrite where or why a scientific execution occurred.
- The Run Request, manifest, index, CLI, output browser, Memory capture and
  migration tooling must adopt the explicit Run Scope union.
- The Owner cannot place an already completed Unassigned Run into a Project in
  v1. Reusing it requires an explicit future reference model, not a misleading
  move or retag operation.
- Current stringly `project_id` fallbacks and divergent autonomous output paths
  become implementation drift to remove before claiming this ADR is complete.

## Rejected alternatives

- **Create a reserved `default` Project Record.** Rejected because a fallback
  storage bucket has no research-continuity intent and would require artificial
  lifecycle, binding and Memory semantics.
- **Require every Run to belong to a Project.** Rejected because demos,
  inspection and unbound conversations are legitimate local-first workflows.
- **Treat empty, `default`, Session or Chat IDs as Project IDs.** Rejected
  because it bypasses opaque identity generation and authoritative validation.
- **Allow post-run `project_id` edits.** Rejected because manifest, path, index,
  Memory, App and downstream references would disagree about provenance.
- **Move the Run directory on reassignment.** Rejected because absolute and
  external paths dangle and an interrupted move has no cross-store transaction.
- **Copy the Run into the Project.** Rejected because one execution would appear
  as two Runs unless a new derived execution and lineage model were defined.
- **Use symlinks as membership.** Rejected because they create ambiguous
  ownership and are unsupported or unsafe across platforms and path resolvers.
- **Store all Runs in `control.db`.** Rejected because the control plane needs to
  validate Project identity and admission, while Run storage already owns
  execution records and artifacts.
