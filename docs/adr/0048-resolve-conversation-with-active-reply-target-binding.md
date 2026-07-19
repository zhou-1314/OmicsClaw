# Resolve Conversation through a durable active Reply Target binding

## Status

Accepted (2026-07-14).

Refines
[ADR 0045](0045-owner-identity-is-not-a-state-partition.md) and
[ADR 0046](0046-normalize-all-conversational-ingress-before-dispatch.md).

**Routing-policy refinement (2026-07-14):**
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md)
defines `(Surface, Reply Target)` as the Conversation's immutable logical
address. An explicit Conversation ID is valid only at that same address; the
Conversation is never implicitly transferred to another target.

**Turn-ordering refinement (2026-07-14):**
[ADR 0050](0050-serialize-turns-per-conversation-with-bounded-fifo.md) resolves
the concurrency question deferred below: one bounded process-local FIFO
serializes whole Turns per Conversation without defining a global or persistent
chat queue.

**State-ownership refinement (2026-07-14):**
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md)
makes Project Records, Conversation Records and Active Conversation Bindings
authoritative Control Plane State. Memory and filesystem projections cannot
create or retarget those identities.

**Project-lifecycle refinement (2026-07-14):**
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md)
keeps an Active Conversation Binding unchanged when its Conversation's Project
is archived. Novel Turns fail closed until that same Project is restored; the
binding is never silently cleared or retargeted.

## Implementation

Partially implemented. The production prompt-toolkit and single-shot CLI use a
stable local installation/profile/slot Reply Target and let the Repository
resolve its opaque active Conversation binding. CLI `/new` submits a structured
control Turn on the same `slot=main`, atomically moves that binding to a fresh
Conversation, and never sends the command through the Agent. The legacy short
session ID remains presentation state rather than canonical identity.

Channel `SessionManager.get_or_create()` still derives
`session_id = f"{platform}:{user_id}:{chat_id}"`, so transport identity, reply
destination, and conversational continuity collapse into one composite key.
Desktop and Textual TUI also maintain Surface-specific session selection. The
control-plane-generated record and active binding are therefore authoritative
for the first CLI slice, not yet shared by all Surfaces.

## Context

An opaque Conversation ID cannot be inferred from a Channel message that
carries only a sender and reply destination. Conversely, using Reply Target as
the Conversation ID would make `/new` impossible without changing transport
destination and would contradict the decision that one Reply Target may carry
several consecutive Conversations.

The Ingress Normalizer therefore needs a durable navigation pointer from each
reply location to the Conversation currently selected there. That pointer must
not become the Conversation's identity or reintroduce Owner Identity into
storage keys.

## Decision

The control plane persists one **Active Conversation Binding** for each
normalized `(Surface, Reply Target)` key:

```text
(Surface, Reply Target) -> active Conversation ID
```

The binding is a navigation pointer, not ownership, membership, execution
liveness, or Transcript identity. A Reply Target may retain any number of
historical Conversations but has at most one active Conversation at a time.
A Conversation that is no longer active remains durable and independently
addressable; changing the pointer never merges or deletes its Transcript.

Reply Target is a structured Surface address and must include the Adapter or
provider-account namespace, destination id, and thread/topic component needed
to be unique within that Surface. Owner Identity and Project ID are never part
of the binding key.

### Resolution algorithm

For each accepted Raw Inbound, the Ingress Normalizer resolves Conversation in
this order:

1. **Explicit Conversation ID.** If the input explicitly selects a Conversation,
   validate that it exists, belongs to the same fixed Surface, and is valid for
   the current Reply Target under the routing policy. Use it for the turn and
   atomically make it the target's active Conversation.
2. **Existing active binding.** Without an explicit selection, load the durable
   `(Surface, Reply Target)` binding and use its Conversation.
3. **No active binding.** Create a new opaque Conversation, with the explicitly
   selected Project if one was supplied or otherwise unbound, and atomically
   install it as active.

Conversation selection happens before Inbound Envelope construction. The
Envelope receives the resolved opaque Conversation ID, not the binding key.
Changing the active binding after dispatch begins does not retarget an in-flight
turn.

### New Conversation and Project selection

`/new` and equivalent Desktop or CLI actions are control-plane Conversation
commands, not Surface-local Transcript mutations. They create a new opaque
Conversation and atomically replace the target's active binding. The previous
Conversation remains available as history.

Project selection follows the immutable binding rule:

- if the active Conversation is unbound, its first Project binding is written
  once and the Conversation remains active;
- if it is already bound to the selected Project, it remains active;
- if it is bound to a different Project, create a new Conversation bound to the
  selected Project and atomically make that Conversation active.

Project ID does not enter the active-binding key. This permits one Reply Target
to move between Project-specific Conversations without changing destination.

### Atomicity and concurrency

Conversation creation, first Project binding, and active-pointer replacement
must use a transaction or compare-and-swap operation protected by a unique
constraint on `(Surface, Reply Target)`. Concurrent first messages, `/new`, or
Project switches must not create two active pointers or partially bind one
Conversation to two Projects.

The resolver returns a stable Conversation ID snapshot for the accepted turn.
Ordering concurrent turns inside the same Conversation is a separate dispatch
and Transcript concern; this ADR does not define a global message queue.

Binding state is restart-resilient control-plane state. It is not reconstructed
from Transcript contents, provider history, Owner Identity, or display names.

## Consequences

- `/new` can start clean continuity without changing a chat, group, thread, CLI
  terminal, or Desktop view.
- A Project switch preserves immutable Conversation-to-Project binding while
  keeping the same Reply Target.
- Identity rotation does not affect Conversation selection because Owner
  Identity is absent from the binding key.
- Restart recovery restores which Conversation each Reply Target was using.
- Explicit Conversation selection and implicit continuation converge on one
  resolver and one persisted state model.
- Current composite Session IDs and Surface-local selection stores require a
  migration and compatibility plan.

## Rejected alternatives

- **Use `(platform, user_id, chat_id)` as Conversation ID.** Rejected because it
  mixes transport, Owner admission, reply routing, and durable continuity.
- **Use Reply Target itself as Conversation ID.** Rejected because the target
  could not retain several consecutive Conversations.
- **Include Project in the active-binding key.** Rejected because Project
  switching would create several simultaneously active Conversations for one
  destination rather than one explicit active pointer.
- **Keep the binding only in memory.** Rejected because restart would silently
  select or create the wrong Conversation.
