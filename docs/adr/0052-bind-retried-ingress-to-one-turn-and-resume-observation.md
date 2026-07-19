# Bind retried ingress to one Turn and reconnect observers without re-execution

## Status

Accepted (2026-07-14).

Refines
[ADR 0006](0006-agent-dispatch-event-stream.md),
[ADR 0009](0009-wire-cancel-event-through-dispatch.md),
[ADR 0046](0046-normalize-all-conversational-ingress-before-dispatch.md),
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md),
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md),
[ADR 0050](0050-serialize-turns-per-conversation-with-bounded-fifo.md), and
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md).

It supersedes only ADR 0050's rule that Response Sink loss may terminalize a
Turn as canceled or failed. The per-Conversation single-writer boundary and
non-durable FIFO remain accepted.

**Project-lifecycle refinement (2026-07-14):**
[ADR 0055](0055-model-project-lifecycle-as-reversible-archive-and-restore.md)
keeps duplicate lookup first, then requires a novel request's Project lifecycle
check before Conversation/binding mutation, FIFO reservation or Turn
acceptance. A duplicate still returns its original Turn after archive; a novel
request to an archived Project creates no Turn.

**Run-idempotency refinement (2026-07-14):**
[ADR 0058](0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md)
adds the separate top-level Run contract anticipated by this ADR's guarantee
boundary: a Run Submission Binding prevents duplicate Run acceptance and one
fenced Execution Assignment prevents duplicate executor start. Turn and Run
bindings remain distinct, and neither claims exactly-once scientific side
effects.

**Attachment-idempotency refinement (2026-07-14):**
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md)
defines the attachment identities used here. The request fingerprint contains
ordered Source Attachment Descriptors; a matching duplicate returns the
original Turn and Attachment Records before download or staging, while a novel
equal-byte submission creates new Records that may share one Blob.

**Outbound-delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
resolves the outbound concern deferred below. A duplicate inbound delivery
creates neither another Turn nor another reply; the original Turn's persistent
Outbound Delivery independently retries safe provider failures without entering
`dispatch()`.

## Implementation

Partially implemented across cut-over production paths. The Repository owns
durable Ingress Bindings and the Normalizer performs duplicate/conflict lookup
before capacity reservation, Conversation mutation or novel-side effects.
Prompt-toolkit/single-shot CLI generate a fresh request ID per explicit
submission; `ControlRuntime` returns the original Receipt without re-execution
when the same key and fingerprint are resubmitted. CLI still has no external
transport retry that preserves an ID across process loss.

Desktop now requires a bounded explicit `source_request_id` plus its
Backend-owned installation/profile namespace. Its wire contract reports
`authoritative_ingress=true`, `durable_ingress_idempotency=true`,
`source_request_id_required=true`, and `attachments_supported=false`. A
matching retry while the Turn is live attaches an observer and does not cancel
or dispatch the original again; a retry after terminal state replays retained
EventFrames or a fully verified canonical terminal Transcript snapshot. Reuse
with changed content, files or execution options is an idempotency conflict,
and novel-only policy/workspace/thread side effects run only after acceptance.
The compatibility `POST /chat/stream` still combines initial submission and
observation, but `GET /turns/{turn_id}`, `GET /turns/{turn_id}/events`, and
`POST /turns/{turn_id}/cancel` now provide independent receipt, bounded
Last-Event-ID replay/gap, and explicit cancellation. Disconnect or renderer
failure detaches the Response Sink and never owns Turn lifecycle.

The separate Desktop `POST /v1/turns` multipart-image contract requires a
32-hex `Idempotency-Key` and returns immediately after durable acceptance.
Novel input returns `202`; a matching retry returns `200` with the original
Turn and does not open its upload source; changed descriptors conflict before
source access. Versioned receipt/Event/cancel routes alias the same control
operations. The compatibility chat contract continues to advertise
`attachments_supported=false`; health advertises multipart capability under a
separate `desktop_turn_submission` contract so legacy JSON `files` cannot be
mistaken for supported input.

The Desktop V1 receipt/Event/cancel observation subset is now complete behind
one deep `ControlRuntime` observation Interface. Its Event Hub assigns
one-based `EventFrameV1` sequences plus emission timestamps and atomically
captures replay/gap state while registering the live observer. SSE first emits
an unnumbered `snapshot`, maps the closed Event union to stable typed names,
and flushes the terminal frame before closing. The live Event seam opens before
the durable snapshot read, so a terminal commit/publication race is either in
that snapshot or captured by the observer. An evicted cursor receives a
structured `gap` containing the retained inclusive range and current verified
Receipt projection; the incomplete retained suffix is skipped and the same
atomically opened observer follows frames published after that point. A cursor
ahead of the current sequence is rejected instead of being treated as
eviction. Restart observation has no invented Event history: a terminal Turn
returns its Receipt/Transcript snapshot and closes. Explicit cancel returns a
single versioned shape for changed and idempotent outcomes, and observer close
only detaches that observer. The Hub binds each Turn to its producer event loop,
bounds live observers per Turn, wakes an in-flight observer on close, and its
failure cannot become Turn lifecycle authority. The Desktop response closes its
observer on every ASGI exit, including response-send failure. V1 Event JSON
never invokes arbitrary `str`; it redacts credential-shaped keys, normalizes
non-finite built-in floats explicitly, and rejects unsupported/circular values.

Production Channel duplicate detection remains adapter-specific and Channel
does not use the shared Event Hub. The Event Hub is process-local and currently
bounds Turn count, frame count, observer count and each observer queue, so
restart observation falls back to the durable terminal Receipt and verified
Transcript rather than claiming a durable Event log. It does not yet enforce a
retained-byte quota. Full ADR 0052 coverage therefore remains incomplete until
Interaction resolution, byte-bound Event retention, Channel observation where
required, and remaining Surface protocols cut over.

## Context

A Channel provider may deliver the same message more than once. Desktop may
retry a submission after losing the HTTP response even though the control plane
already accepted it. If either delivery attempt creates a fresh Turn, an
expensive or side-effecting scientific tool can run twice.

Turn ID cannot solve this by itself. It is generated only after the control
plane accepts a Turn, while a Surface retry needs a stable identity that exists
before acceptance. Conversely, a transport message ID cannot become Turn ID:
its namespace, trust, availability and reuse rules vary by Surface.

SSE reconnection is a different concern again. Once Desktop knows Turn ID, a
new connection is only another observer of that Turn. Resubmitting the prompt
to reconstruct a stream would conflate observation with execution and recreate
the duplicate-tool risk.

## Decision

### Durable ingress idempotency binding

Every retryable conversational submission carries a Surface-scoped **Ingress
Idempotency Key**:

```text
Ingress Idempotency Key =
    (Surface, Source Namespace, Source Request ID)
```

- Channel uses the provider message, event or update ID. Source Namespace
  identifies the Channel Adapter instance and provider account or bot
  installation needed to make that ID unambiguous.
- Desktop generates one opaque `client_request_id` when the Owner presses Send
  and reuses it for every network retry of that submission. Its Source
  Namespace is a stable local application installation/profile identity.
- A non-retrying local Surface may generate a fresh Source Request ID for every
  submission. It never infers identity from content equality.

Retry-capable Surface Adapters must provide a stable Source Request ID. An
adapter may derive it only from a provider-defined immutable event identity,
not from message text, arrival time or a content hash. The key is untrusted
input and must be length- and format-bounded. It is not authorization; Owner
admission always happens before lookup.

The control-plane store persists one **Ingress Idempotency Binding** under a
unique constraint:

```text
Ingress Idempotency Key -> (Turn ID, Request Fingerprint)
```

Request Fingerprint is a versioned canonical digest of the source-declared
execution semantics: normalized content blocks, attachment identities or
digests, explicit Conversation or Project selection, and requested execution
options. It excludes arrival timestamps, Turn ID, the current Active
Conversation Binding, dynamically resolved Conversation or Project defaults,
effective policy, authorization, tracing, cancellation and Response Sink.

The binding follows the corresponding Turn Receipt's retention and is not a
short-lived TTL cache. It is removed only by the same explicit data purge that
removes the receipt.

### Duplicate and conflict semantics

After Owner admission and side-effect-free normalization of the key and
fingerprint, ingress checks the binding before attachment staging,
Conversation resolution, Active Conversation Binding mutation, Transcript or
Memory writes, replies, Agent execution, tools or Runs.

- Same key and same fingerprint returns the existing Turn ID and current Turn
  Receipt status. It creates no new side effect, regardless of whether the Turn
  is `queued`, `running`, `succeeded`, `failed`, `canceled` or `interrupted`.
- Same key and different fingerprint is an idempotency conflict. Desktop
  receives a conflict response; Channel acknowledges the provider delivery,
  records the conflict and drops it. Neither creates another Turn.
- Different keys with equal content are distinct Owner submissions and create
  distinct Turns. Content equality is never identity.

Automatic Channel redelivery is transport-acknowledged without producing a
second conversational reply. If the original outbound delivery failed,
re-delivering the already-computed result is an outbound-delivery concern; it
must not re-run the Turn.

An explicit Owner retry always receives a fresh Source Request ID and a fresh
Turn ID, optionally linked through `retry_of_turn_id`. A terminal receipt is
never reopened.

### Atomic acceptance

For a novel key, ingress resolves the Conversation and reserves its Turn
Sequencer capacity before acceptance. It then generates Turn ID and commits the
queued Turn Receipt plus Ingress Idempotency Binding in one control-plane state
transaction before enqueueing the process-local Turn Execution.

If capacity is unavailable or the transaction fails, ingress releases the
reservation and returns backpressure. There is no accepted Turn, receipt or
binding, so the same Source Request ID may safely retry later. A per-key
process-local guard may avoid duplicate work, but the durable unique constraint
is authoritative if concurrent attempts race.

If the process fails after commit but before enqueue, startup reconciliation
changes the queued receipt to `interrupted` under ADR 0051. The binding remains;
a repeated delivery returns that interrupted Turn and never silently executes
it.

### Submission and observation are separate

Desktop separates the Interface that submits a Turn from the Interface that
observes an existing Turn. The target semantics are:

```text
POST /conversations/{conversation_id}/turns
Idempotency-Key: <client_request_id>
-> Turn ID + Turn Receipt status

GET /turns/{turn_id}/events
Last-Event-ID: <event_sequence>
-> SSE observation of the existing Turn
```

The route spelling may evolve, but the separation may not: opening or
reopening an observer never submits an Inbound Envelope and never enters the
Turn Sequencer.

Every typed Event for a live Turn has a monotonically increasing per-Turn event
sequence, rendered as SSE `id:`. Turn Execution keeps a bounded process-local
event buffer:

- reconnect within the same process replays buffered Events after
  `Last-Event-ID` and then follows live Events;
- a cursor older than the buffer produces an explicit stream-gap indication
  plus the current Turn status and any pending interaction snapshot, never
  re-execution;
- after control-plane restart the buffer is gone and the durable receipt shows
  `interrupted`; Events are not reconstructed or replayed;
- terminal content remains available through Transcript and Turn Receipt
  correlation rather than a durable progress-event log.

The event buffer is observability state, not a persistent EventBus, chat queue
or executable replay record.

### Observer loss is not cancellation

`dispatch()` publishes terminal Events to the live Turn event boundary whether
or not a Response Sink is currently attached. A Response Sink observes and
renders those Events; it does not own Turn lifecycle.

Desktop SSE disconnect, terminal loss or Channel client disconnect detaches
that observer only. The Turn continues, retains its Conversation single-writer
lease, and can be observed through a new Response Sink. Only explicit
`cancel(Turn ID)` may cancel it.

The Turn Sequencer releases after terminal Transcript state is committed and
the terminal Event is published to the Turn event boundary. Turn success or
failure reflects conversational/scientific execution, not transport delivery
or acknowledgement. This replaces ADR 0050's sink-loss terminalization clause
and clarifies ADR 0051's terminal Response Sink wording.

## Guarantee boundary

This decision provides at-most-one accepted Turn per Ingress Idempotency Key;
it does not claim end-to-end exactly-once scientific side effects. If a process
fails after a tool changes external state but before recording completion, the
receipt becomes `interrupted` and duplicate transport delivery still does not
re-run it. An explicit Owner retry may repeat that tool effect. Stronger
guarantees require a separate Run- or tool-level idempotency contract.

## Consequences

- Channel redelivery and Desktop request retry converge on the same original
  Turn across control-plane restarts.
- A change to `/new`, Project selection or Active Conversation Binding after
  original acceptance cannot retarget a duplicate delivery.
- Desktop can reconnect to long analyses without canceling or re-submitting
  them.
- Turn progress remains bounded and process-local; only identity, lifecycle and
  ingress binding cross restart.
- Outbound delivery retry and tool-side-effect idempotency remain separate
  concerns and cannot be implemented by replaying an accepted Turn.

## Rejected alternatives

- **Deduplicate by content hash.** Rejected because the Owner may intentionally
  send the same request twice, while equal text may carry different files or
  execution options.
- **Keep the existing in-memory TTL caches.** Rejected because they disappear
  on restart, vary by Adapter and eventually permit an old delivery to execute
  again.
- **Let the client or provider choose Turn ID.** Rejected because canonical
  Turn identity belongs to the control plane and external ID namespaces are not
  uniform or trusted.
- **Include current Conversation or Project resolution in the key.** Rejected
  because a delayed retry after `/new` or a Project switch must still find the
  original Turn.
- **Reconnect by repeating the streaming POST.** Rejected because stream
  observation would remain capable of creating execution.
- **Cancel when the Response Sink disconnects.** Rejected because a transient
  network failure is not an explicit Owner cancellation and may terminate an
  expensive analysis mid-tool.
- **Persist and replay every Turn Event.** Rejected because reconnect needs a
  bounded live observation buffer and durable terminal state, not another
  persistent chat EventBus or replayable task log.
