# Single-owner control plane with owner-only Channel ingress

## Status

Accepted (2026-07-14).

Supersedes
[ADR 0043](0043-local-first-control-plane-extensible-run-execution.md).

**Storage refinement (2026-07-14):**
[ADR 0045](0045-owner-identity-is-not-a-state-partition.md) makes Owner Identity
an ingress and Source Attribution value only, never a state-partition key.

**Ingress refinement (2026-07-14):**
[ADR 0046](0046-normalize-all-conversational-ingress-before-dispatch.md)
centralizes Owner admission and conversational normalization before dispatch.

**Security-dependency clarification (2026-07-15):** a Channel Adapter verifies
provider/webhook authenticity and extracts Source Attribution, but the
Backend-owned Ingress Normalizer compares that subject with Backend-owned Owner
Identity configuration. A Surface never supplies or overrides the
authorization policy used to admit itself.

## Implementation

Partially implemented. OmicsClaw already runs as a local-first backend for one
person, and some Channel Adapters have sender allowlists. Those allowlists are
optional and default-open in existing paths, while other Adapters lack the same
enforcement. The isolated Stage 2a Normalizer now has a fail-closed,
Backend-owned Owner map scoped by adapter, account and subject kind; it also
cross-checks the Channel Source Namespace against the Reply Target. It still
rejects every valid Channel event before acceptance because terminal-Delivery
capacity is unavailable, and no production Adapter calls it. Owner enforcement
is therefore not yet one production ingress contract.

The Desktop routes reuse the remote Bearer gate. Token-free startup is allowed
only for a loopback bind; the official launcher refuses empty, wildcard,
external-address and non-loopback-hostname binds unless
`OMICSCLAW_REMOTE_AUTH_TOKEN` is configured. Installation/profile wire fields
remain transport facts, never authorization. Conversation identity and Project
binding still remain inconsistent across production Surfaces. The
single-process control plane exists; the replaceable Run Executor Seam remains
a target.

## Context

ADR 0043 briefly selected a single-process control plane that could serve
multiple isolated Users. That model requires a user registry, identity linking,
Conversation membership, authorization checks, private-versus-shared Memory
rules, and tenant-safe storage and observability. Those costs do not serve
OmicsClaw's local-first product: one researcher operates one backend instance,
even when that researcher reaches it through several Surfaces or devices.

Channel groups create a narrower ambiguity. A group or thread may be a useful
place for the Owner to invoke OmicsClaw, but that does not make every sender in
the group a supported user or a participant in the durable Conversation.

## Decision

Each OmicsClaw backend instance serves exactly one human **Owner**.

- OmicsClaw v1 has no multi-user accounts, user registry, Conversation
  membership, roles, per-user ACLs, or tenant-isolation contract.
- The Owner may have multiple explicitly configured **Owner Identities** across
  Surfaces and Channel Adapters. They are authorization credentials for the
  same Owner, not separate Users that require linking or merging.
- Local CLI and Desktop access is Owner access under the deployment boundary.
  If remote access later uses login state, it is an **Authentication Session**
  for the same Owner; it does not introduce another user model.
- The Channel Surface accepts user-authored messages only after the Adapter has
  verified provider authenticity and the Backend-owned Ingress Normalizer has
  matched the extracted subject to a configured Owner Identity.
- Missing, empty, or invalid Owner Identity configuration fails closed for an
  externally reachable Channel Adapter. It never means "accept every sender."
- A message from any other Channel sender is ignored before Conversation or
  Project resolution: it creates no Conversation, writes no Transcript or
  attachment content, invokes no Agent or tool, and sends no reply. Minimal
  content-free rejection telemetry may be recorded for operations and abuse
  detection.
- A direct chat, group, channel, or thread may be a **Reply Target**, but people
  other than the Owner are not Conversation participants. Platform members may
  still see OmicsClaw's replies according to that platform's visibility rules.
- All non-system state belongs to the Owner. Conversation, Project, Workspace,
  and Namespace may organize or constrain that state, but none represents a
  tenant or a second person.

OmicsClaw remains a **local-first modular monolith with a single-process control
plane and an extensible Run execution plane**. The control plane owns ingress,
Owner admission, Conversation and Project resolution, context, in-process
dispatch, typed Events, policy, and durable control state. Skill, Workflow, and
autonomous-analysis Runs may execute locally or remotely behind a replaceable
Run Executor Seam.

Chat turns do not require a persistent cross-process queue, separate chat
Worker, cross-process EventBus, or horizontally scaled control plane. A future
execution queue may be introduced behind the Run Executor Seam. Supporting
multiple human users requires a new explicit ADR and product mode; it must not
emerge implicitly from Channel group membership or storage keys.

## Consequences

- Identity is an ingress-admission concern, not a tenancy model.
- Cross-Surface continuity can serve the same Owner without account linking,
  automatic identity merging, or per-user privacy partitions.
- Group collaboration through OmicsClaw is intentionally out of scope. Only
  the Owner can cause state changes or Agent execution.
- Using a group as a Reply Target can expose responses to its members; Owner
  admission does not make the transport destination private.
- Restart recovery remains required for committed Owner state, Conversations,
  Projects, and Runs.
- Existing `user_id`, per-sender Namespace, Session, and participant-shaped
  implementation fields are legacy facts to audit, not evidence of a current
  multi-user domain model.
- ADR 0006's in-process dispatch remains current. ADR 0040's transcript
  durability remains current but now protects one Owner's Conversations.
