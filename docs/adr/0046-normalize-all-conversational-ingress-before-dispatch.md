# Normalize every conversational ingress before dispatch

## Status

Accepted (2026-07-14).

Refines [ADR 0006](0006-agent-dispatch-event-stream.md),
[ADR 0044](0044-single-owner-control-plane-and-owner-only-channel-ingress.md),
and [ADR 0045](0045-owner-identity-is-not-a-state-partition.md).

**Interface refinement (2026-07-14):**
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md) makes
Inbound Envelope pure serializable data and moves live execution capabilities
into a per-invocation Dispatch Context.

**Conversation-resolution refinement (2026-07-14):**
[ADR 0048](0048-resolve-conversation-with-active-reply-target-binding.md)
resolves explicit or implicit Conversation selection through one durable
`(Surface, Reply Target) -> active Conversation ID` binding.

**Attachment-normalization refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
requires every Surface to provide side-effect-free Source Attachment
Descriptors. For a novel request, this Normalizer stages and verifies the whole
batch through the shared Attachment Store before Turn acceptance; a duplicate
never downloads or registers another occurrence.

**Terminal Channel delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
keeps ingress and Outbound Delivery as separate directions. A Channel Surface
may render live progress Events, but canonical terminal output is accepted into
the persistent Outbox and sent only by the Delivery Pump.

**Security and Run-scope clarification (2026-07-15):** the Normalizer's Owner
Identity map and Workspace policy are Backend-owned dependencies, not ports
provided by a Surface. [ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md)
also replaces the historical `default`-Project wording below with explicit
`UnassignedScope`.

## Implementation

Partially implemented. Stage 2a provides versioned deep-frozen `RawInboundV1`, canonical semantic
fingerprinting, Backend-owned Owner/local-source admission, read-only identity
planning, bounded admission reservation, durable Turn acceptance and
deep-frozen `InboundEnvelopeV1` construction behind one
`IngressNormalizer.accept(raw)` Interface. The production async Interface adds
strict Source Attachment Descriptors and a separate process-local byte source,
with duplicate-first lookup, Delivery/FIFO reservation and
publish-before-control coordination against the Backend-owned Attachment Store.
File Selections and uncut source shapes remain fail-closed.

The prompt-toolkit REPL, single-shot CLI, Desktop text/multipart-image and Owner-only Telegram
text/single-photo input now submit through this Module. `ControlRuntime` owns durable
acceptance and only its internal Agent Worker Adapter constructs the temporary
legacy `MessageEnvelope`. Textual TUI and non-Telegram Channel Adapters still
construct that legacy envelope independently, so normalization is not yet
shared by every Surface.

## Context

A shared dispatcher unifies how a completed request executes, but it does not
guarantee that the request means the same thing when it came from CLI, Desktop,
Telegram, Feishu, or another Channel Adapter. Surface-owned envelope assembly
has already produced incompatible Conversation keys, identity-derived Memory
partitions, attachment shapes, file-reference behavior, and Project defaults.

OmicsClaw needs CellClaw's useful architectural property—a single normalized
inbound contract—without copying its cross-process chat queue or Worker model.
The local-first single-process control plane can provide this as an in-process
Module boundary.

## Decision

Every **conversational input** from CLI, Desktop, or Channel must pass through
one shared **Ingress Normalizer** before `dispatch()`.

The ordered path is:

```text
Surface / Channel Adapter
  -> Raw Inbound
  -> Ingress Normalizer
  -> Inbound Envelope
  -> dispatch()
  -> Agent Loop
  -> typed Events
  -> Surface renderer
```

### Surface boundary

A Surface owns only its transport and interaction mechanics:

- verify provider/webhook authenticity and decode the transport event;
- expose the external subject, Reply Target, content, side-effect-free Source
  Attachment Descriptors, and transport metadata as **Raw Inbound**;
- acknowledge requests, show typing/progress, and render typed Events;
- implement purely local UI/lifecycle commands that do not enter the Agent or
  mutate Conversation, Project, Memory, or Run state.

A Surface does not generate canonical Conversation IDs, derive Memory
Namespaces, choose Project storage keys, durably stage attachments, or call the
Agent Loop directly.

### Ingress Normalizer boundary

The Ingress Normalizer is one in-process control-plane Module shared by all
Surfaces. In this order it:

1. applies Owner admission—Channel identities must match configured Owner
   Identities; trusted local CLI/Desktop sources resolve to the same Owner;
2. rejects non-Owner or invalid input before durable attachment staging or any
   Conversation, Project, Memory, Transcript, Agent, tool, or reply side effect;
3. resolves or creates the opaque Conversation and enforces its fixed Surface
   and immutable optional Project binding;
4. normalizes text and multimodal content, coordinates all-or-nothing accepted
   attachment publication through the Attachment Store, and validates File
   References into transport-neutral forms;
5. records non-secret Source Attribution and keeps Reply Target as routing
   metadata;
6. produces one immutable, validated **Inbound Envelope**.

The Inbound Envelope is the only conversational request accepted by
`dispatch()`. At minimum it carries the opaque Conversation identity, fixed
Surface, optional Project and Workspace context, normalized content and ordered
Attachment References plus File References, Reply Target, Source Attribution,
and the turn options needed by the runtime. Transport `user_id`, `chat_id`,
provider thread ids, and SDK objects never act as canonical domain identities.

The exact field layout and the treatment of live in-process controls such as
cancellation and approval callbacks are a detailed Interface design beneath
this ADR. They must not weaken the normalized identity, ownership, and content
contract.

`dispatch()` remains in-process and continues to expose the typed Event stream
from ADR 0006. The Agent Loop becomes private behind dispatch; no Surface or
Channel Adapter may import or invoke it as a conversational shortcut.

### Non-conversational Run path

A deterministic, explicitly non-chat operation may bypass conversational
ingress only when an already-authenticated control-plane caller constructs a
typed **Run Request** and invokes the Run Executor facade. Examples include a
direct CLI Skill command or a dedicated Desktop Run action whose inputs and
Project—or explicit Unassigned choice—are already known.

That direct path has no Conversation, writes no Transcript, and produces no
chat reply. A Run requested through natural-language chat still enters through
the Ingress Normalizer and Agent path before tool policy creates the Run.
Conversational or state-mutating Surface commands cannot use the Run facade to
bypass Owner admission, Project rules, approval, or tool policy that would
otherwise apply.

This decision introduces neither a persistent inbound queue nor a separate
chat Worker. A future Run queue remains an execution-plane implementation
behind the Run Executor facade.

## Consequences

- Identity, Conversation, Project, attachment, and file-reference behavior can
  be tested once against every Surface.
- Adding a Channel Adapter becomes a transport adaptation task rather than a
  new state and dispatch implementation.
- Non-Owner content is rejected before it can consume durable storage or Agent
  resources.
- Surface renderers remain free to present different interaction styles while
  execution semantics remain identical.
- Existing `MessageEnvelope` construction sites must migrate behind the
  Ingress Normalizer; the class may evolve into the Inbound Envelope or be
  replaced, but Surfaces no longer own its canonical construction.
- Existing Surface-local state mutation and attachment side channels require
  audit and either a control-plane command Interface or normalized ingress.
- Direct deterministic Runs stay efficient without creating a second chat
  engine or weakening conversational invariants.

## Rejected alternatives

- **Let every Surface construct the final envelope.** Rejected because a shared
  dataclass does not normalize semantics or prevent divergent state keys.
- **Normalize inside the Agent Loop.** Rejected because unauthorized input and
  attachments would cross the admission boundary, and the loop would remain
  coupled to transports.
- **Route every chat turn through a persistent queue and Worker.** Rejected for
  the current local-first control plane; normalization does not require a
  distributed deployment model.
- **Force deterministic direct Runs through a fake Conversation.** Rejected
  because non-chat execution does not need Transcript or reply semantics.
