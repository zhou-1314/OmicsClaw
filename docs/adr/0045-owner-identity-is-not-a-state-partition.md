# Owner Identity authenticates ingress but never partitions Owner state

## Status

Accepted (2026-07-14).

Refines
[ADR 0044](0044-single-owner-control-plane-and-owner-only-channel-ingress.md).

**Authoritative-state refinement (2026-07-14):**
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md)
places Project, Conversation and Turn identity/lifecycle facts in Control Plane
State. Owner Identity remains absent from those ownership and partition keys.

**Attachment-ownership refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
makes the Attachment Store authoritative for immutable per-Turn Attachment
Records and content-addressed Blobs. Records belong to a Turn inside its opaque
Conversation; Owner Identity, Surface and provider subjects remain only
admission or non-secret source-attribution facts.

## Implementation

Implementation drift detected. The graph Memory compatibility layer currently
derives Channel Namespaces as `f"{platform}/{user_id}"`, and several legacy
Session paths derive ids as `f"{platform}:{user_id}:{chat_id}"`. These keys
partition one Owner's state by transport identity and must not be copied into
new storage or ingress Interfaces. A migration design for existing rows and
files is outside this ADR.

## Context

ADR 0044 establishes one Owner per backend and allows that Owner to configure
several Owner Identities. Treating those identities as storage owners would
silently reconstruct the rejected multi-user model inside Memory, Transcript,
attachment, and Project keys. It would also fragment cross-Surface continuity,
make an identity rotation appear to lose state, and contradict opaque
Conversation identity.

Owner Identity still has two legitimate uses: deciding whether an inbound
message is authorized, and recording minimal provenance about an accepted
message. Neither use requires it to own the resulting durable state.

## Decision

**Owner Identity is an ingress credential and source-attribution value, never a
durable state-partition key.**

Ingress authenticates an external subject as one of the configured Owner
Identities and then resolves the request into the singleton Owner's domain.
After admission:

- Owner Identity must not key or partition Memory, Transcript, attachments,
  Projects, Workspaces, Runs, prompt state, or tool-result storage.
- Owner Identity, Surface, Adapter, and provider message id may be retained as
  non-secret **Source Attribution** metadata where audit or reply behavior needs
  them. Provider tokens, credentials, and raw authentication material are not
  Source Attribution.
- Reconfiguring or rotating an Owner Identity does not move, duplicate, merge,
  or orphan the Owner's existing state.
- Reply Target is also routing metadata, not a state owner.

Durable state uses the narrowest domain identity that actually owns it:

| State | Canonical ownership key |
|---|---|
| Owner preferences and personal Memory | Singleton Owner scope; no synthetic `owner_id` required |
| Transcript and Conversation prompt state | Opaque Conversation ID |
| Inbound Attachment Records | Opaque Attachment ID with explicit owning Turn and Conversation IDs |
| Research context and cross-Surface continuity | Project ID |
| Filesystem-local access and workspace hints | Workspace identity or canonical root |
| Execution lifecycle and artifacts | Run ID with explicit immutable Run Scope |
| Built-in seeds and installation-level state | Explicit system scope |

Storage Modules may use different physical schemas, but they must preserve
these ownership semantics. No universal composite key containing
`platform`, `user_id`, `chat_id`, or Reply Target becomes a substitute for the
domain identities above.

The current `f"{platform}/{user_id}"` Channel Namespace and
`f"{platform}:{user_id}:{chat_id}"` Session key are legacy implementation
facts. New code must not extend them. Their replacement must preserve existing
Owner data and must be designed alongside the unified ingress and Conversation
migration.

Introducing multiple human users later requires a new ADR and an explicit data
migration. OmicsClaw does not pre-install a constant Owner ID or dormant tenant
column merely for that hypothetical future.

## Consequences

- The same Owner can change device, platform identity, or Channel Adapter
  without losing or forking durable research state.
- Cross-Surface continuity flows through Project, while Transcript continuity
  remains scoped to Conversation.
- Owner admission can be replaced or hardened without migrating domain state.
- Source Attribution remains available for audit and reply diagnostics without
  becoming an authorization shortcut or leaking credentials into domain data.
- Existing per-sender Namespaces and composite Session keys require an explicit
  compatibility and migration plan.
- Storage APIs must accept typed domain identities instead of a generic
  transport-derived `user_id` or `chat_id` bag.

## Rejected alternatives

- **One Namespace per Owner Identity.** Rejected because it fragments the same
  person and makes transport configuration determine data ownership.
- **Generate an opaque Owner ID and key every row by it.** Rejected for v1
  because the backend has exactly one Owner; a constant partition adds schema
  and migration cost without isolation value.
- **Put all state into one unscoped global bucket.** Rejected because
  Conversation, Project, Workspace, Run, and system boundaries still have
  distinct lifecycle and continuity semantics even in a single-owner product.
