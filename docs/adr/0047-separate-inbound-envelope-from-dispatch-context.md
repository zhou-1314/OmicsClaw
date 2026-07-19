# Separate the serializable Inbound Envelope from process-local Dispatch Context

## Status

Accepted (2026-07-14).

Refines [ADR 0006](0006-agent-dispatch-event-stream.md) and
[ADR 0046](0046-normalize-all-conversational-ingress-before-dispatch.md).

**Delivery refinement (2026-07-14):**
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md)
keeps serialized Reply Target in Inbound Envelope and places the live Response
Sink in Dispatch Context.

**Turn-identity refinement (2026-07-14):**
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md)
places the control-plane-generated opaque Turn ID in Inbound Envelope while
keeping Turn Execution handles in Dispatch Context. Explicit retry creates a
new Turn ID and never reopens a terminal Turn Receipt.

**Attachment-reference refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
requires Inbound Envelope to carry only structured Attachment References for
accepted records. Base64 payloads, provider handles, signed URLs, temporary or
Workspace paths and process-global file registries are neither Envelope facts
nor restorable Dispatch capabilities.

**Terminal Channel delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
narrows Response Sink to live observation and progress. Neither Inbound
Envelope nor Dispatch Context is a durable delivery payload; terminal Channel
Delivery is planned from committed Transcript/artifact content and persisted
in Control Plane State.

## Implementation

Partially implemented in the isolated Stage 2a slice. `RawInboundV1` and
`InboundEnvelopeV1` recursively detach/freeze JSON-shaped containers and
contain no callbacks, locks, policy state or cancellation handles. The accepted
path checks V1 Surface/ReplyTarget/subject shapes, bounded text and total JSON,
strict Source Attachment Descriptors, and scalar allow-lists for
requested/transport options. File Reference schemas and the broader target
option schema remain deliberately unavailable rather than being accepted
incompletely. The bounded admission FIFO carries only that Envelope shape.

For prompt-toolkit/single-shot CLI, Desktop text/multipart-image and Owner-only Telegram
text/single-photo Turns, fresh `ControlRuntimePorts` now
hold Response Sink, history preparation, approval, policy, accounting and other
live capabilities. They are registered after acceptance and consumed only when
the Turn activates. The internal Agent Worker Adapter still translates the pure
Envelope plus those ports into the frozen legacy `MessageEnvelope`, because
`dispatch()` has not yet adopted the final two-argument Interface. Textual TUI
and non-Telegram Channel Adapters have not crossed this seam.

## Context

ADR 0046 makes Inbound Envelope the normalized conversational contract. If the
contract contains callbacks, locks, mutable Events, accumulators, SDK objects,
or effective authorization state, it cannot be serialized deterministically,
validated independently, or safely used for audit and replay. More
importantly, deserializing such an object could appear to restore authority
that should exist only in the current process and policy decision.

OmicsClaw still needs cancellation, approval prompts, usage accounting, and
effective policy during an in-process turn. Those are invocation capabilities,
not facts about the accepted message.

## Decision

Conversational dispatch has two explicit inputs:

```python
dispatch(envelope: InboundEnvelope, context: DispatchContext) -> AsyncIterator[Event]
```

### Inbound Envelope

Inbound Envelope is pure, immutable, versioned, JSON-compatible domain data.
It contains only normalized facts and requested options needed to describe the
accepted turn, including:

- opaque Conversation ID and fixed Surface;
- optional Project and Workspace references;
- normalized content blocks, ordered Attachment References, and validated File
  References;
- Reply Target and non-secret Source Attribution;
- validated, serializable turn options such as requested model, output style,
  mode, stage, and MCP server references where applicable;
- an explicit envelope schema version.

Inbound Envelope contains no callback, coroutine, lock, file handle, mutable
Event, accumulator, SDK object, database session, credential, provider token,
or effective authorization capability. Requested options are data; the
effective policy that decides whether they may run is not.

Serialization must preserve the Envelope's semantic values and reject unknown
or invalid fields according to its schema version. Serializability does not by
itself authorize execution or promise automatic replay.

### Dispatch Context

Dispatch Context is a process-local, per-invocation capability container
created by the control plane after ingress admission. It may hold:

- cancellation token or `threading.Event`;
- tool-approval port/callback;
- usage accumulator or accounting sink;
- effective policy state and authorization capabilities;
- tracing, clock, and other process-local runtime ports when needed.

Dispatch Context is never serialized as part of Inbound Envelope, written to a
Transcript, included in an envelope digest, or reconstructed from untrusted
input. It may contain mutable handles because its lifecycle is exactly one
`dispatch()` invocation.

Dispatch Context cannot change Conversation identity, Project binding,
normalized content, Source Attribution, or Reply Target. It controls how the
already accepted turn executes; it does not redefine what the turn is.

### Replay and recovery

Any explicit replay or recovery operation must:

1. deserialize and revalidate the versioned Inbound Envelope;
2. re-check that the referenced Conversation, Project, attachments, and files
   still exist and are allowed;
3. create a fresh Dispatch Context from current policy and runtime ports;
4. never reuse a prior approval result or serialized authorization capability.

This separation makes future persistence or queue transport technically
possible but does not introduce either one. The current architecture remains
an in-process single-owner control plane.

## Consequences

- Inbound Envelope fixtures can be compared, audited, and fuzz-tested without
  booting Surface SDKs or runtime callbacks.
- Cancellation and approval remain responsive in-process without polluting the
  domain contract.
- Serialized requests cannot smuggle effective policy or approval authority.
- Envelope schema evolution becomes explicit and independently testable.
- Current `MessageEnvelope` call sites and `dispatch()` tests must be migrated
  to construct data and process context separately.
- Replaying the same Envelope under a fresh Context may produce a different
  authorization outcome when policy has changed; this is intentional.

## Rejected alternatives

- **Keep live handles in a frozen dataclass.** Rejected because frozen field
  references do not make the referenced Events, callbacks, or accumulators
  immutable or serializable.
- **Serialize callbacks or policy snapshots.** Rejected because process
  capabilities and effective authority must not be restored from message data.
- **Put all turn options in Dispatch Context.** Rejected because requested,
  validated options are part of the accepted turn and should remain auditable;
  only effective authorization and live ports belong in Context.
- **Create a queue now because Envelope is serializable.** Rejected because
  Interface portability is not a deployment requirement.
