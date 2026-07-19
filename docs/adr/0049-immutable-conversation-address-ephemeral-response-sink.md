# Give each Conversation an immutable logical address and an ephemeral Response Sink

## Status

Accepted (2026-07-14).

Refines
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md) and
[ADR 0048](0048-resolve-conversation-with-active-reply-target-binding.md).

**Turn-ordering refinement (2026-07-14):**
[ADR 0050](0050-serialize-turns-per-conversation-with-bounded-fifo.md) gives
each Conversation a bounded process-local FIFO and a whole-Turn single-writer
boundary. It does not allow one Transcript to execute concurrently merely
because Response Sinks are recreated per invocation.

**Observer-lifecycle refinement (2026-07-14):**
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md)
allows a Response Sink to detach and a new observer to attach to the same live
Turn. Physical delivery remains ephemeral and never owns Turn cancellation or
terminal status.

**Attachment-ownership refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
keeps inbound attachments under the Conversation content boundary while making
each immutable Attachment Record belong to one originating Turn. Reply Target
and Response Sink never key, retain or delete attachment state.

**Outbound-delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
keeps live progress and observation in the ephemeral Response Sink but routes
canonical terminal Channel replies through a persistent Outbound Delivery
Outbox. Delivery still targets the Conversation's immutable Reply Target and
never owns or changes Turn status.

## Implementation

Partially implemented for production prompt-toolkit/single-shot CLI, Desktop
text and Owner-only Telegram text/single-photo input.
Its serialized Reply Target is stable and stored with the opaque Conversation;
the terminal renderer is a fresh `ControlRuntimePorts.response_sink` that is
never persisted and cannot choose the destination. The Agent history bridge is
keyed by canonical Conversation ID rather than the short legacy session ID.

Desktop reconnects observation by canonical Turn ID without resubmission;
Telegram binds one immutable Reply Target and delivers terminal text only from
the persistent Outbox. Textual TUI and non-Telegram Channel Adapters still
overload `chat_id` or `session_id` across continuity, reply destination and
storage lookup, so the contract is not yet shared production-wide.

## Context

Permanently binding a Conversation to a physical response mechanism would be
wrong: Desktop opens a fresh SSE response for a turn, CLI processes and
terminals restart, and Channel SDK clients reconnect. Those objects are
short-lived execution capabilities.

Allowing one Transcript to move freely among logical destinations is also
wrong. A private-chat Conversation resumed in a group could expose its context;
two threads using one Transcript can interleave turns and make routing,
ordering, prompt caching, `/new`, and active selection ambiguous. Project
already provides the intended research continuity across Conversations.

The architecture therefore separates the stable logical place where a
Conversation may continue from the temporary mechanism used to deliver one
turn's Events.

## Decision

Every Conversation has one immutable **Conversation Address**:

```text
Conversation Address = (Surface, Reply Target)
```

The Conversation record stores both values explicitly. The opaque Conversation
ID remains independent and carries no encoded Surface or Reply Target
semantics. Transcript and attachment state remain keyed by Conversation ID,
not by Conversation Address.

### Reply Target is logical and stable

Reply Target is a normalized, serializable logical destination, not a network
connection or process object. Each Surface must define a stable form:

- Channel includes Adapter/provider-account namespace, destination, and
  thread/topic components needed to prevent collisions;
- Desktop uses a stable logical local application/profile target, never an SSE
  request, socket, browser connection, or process id;
- CLI uses a stable logical local client/profile target, never a terminal file
  descriptor, TTY, or process id.

The exact Desktop and CLI target identifiers belong to the Inbound Envelope
Interface design, but they must satisfy the stable logical-address contract.

### Response Sink is process-local

The live mechanism that renders or sends one turn is **Response Sink**. It may
be an SSE queue/writer, terminal renderer, Channel SDK sender, callback bridge,
or test collector. Response Sink belongs to the per-invocation Dispatch Context
and may be recreated on every turn.

Response Sink is never serialized into Inbound Envelope or stored as the
Conversation Address. It cannot override the Envelope's Reply Target or send a
response to a different logical destination without a separately authorized
control-plane action.

### Conversation selection and continuity

An explicit Conversation ID is valid only when its stored Conversation Address
exactly matches the current normalized `(Surface, Reply Target)`. The
Conversation Resolver must reject a mismatch; it never silently moves,
re-homes, aliases, or attaches that Transcript to another target.

To continue related work at another Reply Target—even within the same
Surface—the control plane creates a new Conversation, normally bound to the
same Project when research continuity is desired. Transcripts remain separate
and are never copied or merged automatically. Cross-Surface continuity follows
the same Project-based rule already accepted for Conversation.

The Active Conversation Binding key from ADR 0048 is exactly the Conversation
Address. `/new`, explicit selection, and Project switching choose among
Conversations whose stored address matches that key. A Reply Target can carry
many historical Conversations but only one active pointer.

Administrative import or migration may preserve historical provenance, but
runtime Conversation movement to another address is not supported. Any future
re-home feature requires a new ADR with explicit privacy, ordering, and
Transcript semantics.

## Consequences

- Desktop reconnection, CLI restart, and Channel client reconnection replace
  Response Sink without changing Conversation identity.
- A private or thread-specific Transcript cannot be resumed accidentally in a
  broader-visibility target.
- One Conversation has one unambiguous logical reply destination and one prompt
  history, while a Reply Target can still rotate among historical
  Conversations.
- Project remains the continuity boundary across Reply Targets and Surfaces.
- Surface adapters must define stable logical Reply Targets and stop using
  connection/process identifiers as durable addresses.
- Dispatch Context gains a Response Sink port; Inbound Envelope retains only
  the serializable Reply Target.

## Rejected alternatives

- **Bind Conversation to a physical SSE connection, terminal, or SDK client.**
  Rejected because those capabilities are ephemeral and non-serializable.
- **Allow one Conversation to be active at several Reply Targets.** Rejected
  because it creates Transcript concurrency, routing ambiguity, and privacy
  leakage that Project-scoped continuity avoids.
- **Support implicit Conversation transfer.** Rejected because an explicit ID
  must not be enough to expose prior Transcript context in a new destination.
- **Copy the Transcript when changing target.** Rejected because it forks
  history and makes future context, audit, and deletion behavior ambiguous.
