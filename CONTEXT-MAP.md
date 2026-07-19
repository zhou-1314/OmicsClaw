# Context Map

OmicsClaw has multiple bounded contexts. Each owns its vocabulary in a local
`CONTEXT.md`; this map says where they live and how they relate.

## Contexts

- [Control Plane, Ingress & Memory](./docs/CONTEXT.md) — authoritative control
  identity, normalized ingress, the graph-backed Memory system and the three
  user-facing Surfaces (Channel / Desktop / CLI), plus analysis routing.
- [Runtime](./omicsclaw/runtime/CONTEXT.md) — code-driven orchestration: workflows, the
  workflow runtime, and consensus.
- [Bench](./docs/bench/CONTEXT.md) — the desktop research-assistant workspace
  (read → ideate → analyze → write).

## Relationships

- **Bench → Control Plane, Ingress & Memory**: Bench is a *page on the Desktop
  Surface*, not a new Surface. Each investigation thread presents one
  authoritative Control Plane Project; `project://<id>` stores its associated
  research knowledge. A Project is `active` or reversibly `archived`; archive
  retains the thread's Conversations and content but closes it to new scientific
  work until restore.
- **Bench → Runtime**: Bench's Analyze stage invokes workflows (consensus / pipeline) and
  skills through the existing Analysis Router; it never authors orchestration.
- **Control Plane, Ingress & Memory ↔ Runtime**: the control plane supplies
  canonical Project, Conversation, Turn and Run identities. A top-level Skill,
  Workflow or Autonomous submission carries a caller-generated opaque Run
  Submission ID that durably binds to one control-generated Run ID and immutable
  `ProjectScope(project_id)` or `UnassignedScope`. Its minimal Run Receipt,
  Submission Binding and sole Assignment-ID-fenced Execution Assignment stay in
  Control Plane State, while its scientific Manifest, Run Steps and artifacts
  stay in Run storage. Every accepted top-level Run enters the one bounded
  process-local strict-FIFO Run Dispatcher; before its sole Assignment, the
  Dispatcher obtains first-unit capacity from the shared Execution Resource
  Scheduler. The canonical local Assignment atomically binds a write-once
  Process Tree Owner before launch; ordinary Job/PID/Worker values remain
  replaceable Execution References. Process-local Resource Leases account for
  scientific-process capacity but never own execution. Fenced report conflicts,
  Manifest/Receipt drift, unconfirmed owners and recovery commit failures append
  content-free Run Integrity Incidents behind the same Repository Seam; their
  Desktop list Adapter is pure observation and cannot start or repair work.
  `analysis://` is a projection, and the
  Desktop Surface streams process-local progress. The compatibility `default/`
  output grouping is Unassigned storage, not a Project identity.
- **Ingress Attachment Store ↔ Control Plane ↔ Runtime**: the Attachment Store
  owns immutable per-Turn Attachment Records and content-addressed Blobs, while
  Control Plane State alone establishes whether their owning Turn exists. The
  Inbound Envelope and Transcript expose only ordered Attachment References;
  runtime tools resolve explicit references, and every consuming Run Manifest
  freezes the Attachment ID plus verified digest as scientific input
  provenance. File References to pre-existing Workspace files remain a
  separate authorization and mutation contract.
- **Runtime → Control Plane → Channel Delivery**: a Channel Turn durably commits
  its terminal Transcript/artifact references before Control Plane State
  atomically terminalizes the Turn and creates its one canonical Outbound
  Delivery. The in-process Delivery Pump invokes single-attempt provider
  Adapters from the persistent Outbox; safe delivery retry never re-enters the
  Agent, a Skill, Workflow, Autonomous execution or Run Executor. Desktop/CLI
  continue to observe existing Turn state rather than consuming this Outbox.
