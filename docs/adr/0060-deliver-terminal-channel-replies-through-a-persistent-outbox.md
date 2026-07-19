# Deliver terminal Channel replies through a persistent Outbox

## Status

Accepted (2026-07-14).

Refines
[ADR 0006](0006-agent-dispatch-event-stream.md),
[ADR 0043](0043-local-first-control-plane-extensible-run-execution.md),
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md),
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md),
[ADR 0050](0050-serialize-turns-per-conversation-with-bounded-fifo.md),
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md),
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md),
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md),
[ADR 0054](0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md), and
[ADR 0059](0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md).

**ReplyTarget-ordering refinement (2026-07-15):**
[ADR 0063](0063-serialize-outbound-deliveries-per-reply-target.md) assigns every
Delivery a target-local sequence, permits at most one active provider call per
Reply Target, and suppresses an unattempted suffix after a failed or unknown
Item. Different Reply Targets remain concurrent.

Every accepted Channel Turn produces at most one canonical terminal
**Outbound Delivery**. Its lifecycle and ordered provider-call plan are
persisted in the Control Database atomically with the Turn's terminal
transition, while its text and media content remain in their specialized
stores. A restart-resilient in-process Delivery Pump retries only delivery; it
never re-enters `dispatch()`, the Agent, a scientific tool or a Run.

## Implementation

Telegram text production slice implemented (2026-07-16). `ControlRuntime`
deterministically freezes the terminal canonical Transcript entry into bounded
text Items, then commits the terminal Turn, immutable Transcript reference,
canonical Delivery, target sequence and Items in one `control.db` transaction.
The Transcript candidate is promoted before the Delivery Pump is woken. The
Pump resolves only committed Transcript content, verifies the frozen codepoint
range and SHA-256, claims one durable Attempt, and invokes a single-attempt
Telegram Adapter without entering `dispatch()`, the Agent, a tool or a Run.

The Adapter classifies Telegram success, known `RetryAfter`, permanent
`BadRequest`/`Forbidden`, and acceptance-unknown exceptions. The Pump applies
bounded provider-hint/exponential retry with jitter only to proven safe non-acceptance;
startup changes every open `sending` Attempt to `unknown` and never blindly
replays it. Adapter resolution is bound to `(adapter, account_namespace)`;
foreign-account Items remain queued and cannot be sent by the current Bot.
A timed-out Adapter must prove cancellation within a bounded grace period;
otherwise its Item becomes `unknown` and the Pump halts fail closed before any
later target sequence can start. Novel Channel ingress is bounded by durable future/actual Delivery
capacity while duplicate lookup bypasses that gate. Telegram terminal handlers
no longer send `Final.text` directly or drain `pending_media`.

**Operator lifecycle slice (2026-07-20).** The control plane now completes the
non-media half of this ADR's Decision. An over-long terminal reply no longer
fails terminalization closed: `freeze_terminal_text_delivery` collapses it into
one bounded fallback Item that keeps the reply's start plus a deterministic
truncation notice, anchored to the immutable Transcript prefix and digest —
without inventing a new store. Explicit Owner action is implemented as
`ControlStateRepository.insert_resend_delivery` / `expedite_delivery_retries`
and surfaced through `ControlRuntime.resend_delivery` / `retry_delivery`:
`retry_delivery` only expedites a `retry_wait` backoff (the schema keeps
`failed`/`unknown`/`delivered` Items terminal), while `resend_delivery` freezes
a new `purpose=resend` Delivery that reuses the immutable content references,
links through `resend_of_delivery_id`, honours the outstanding-delivery capacity
bound and never re-enters `dispatch()`, the Agent, a tool or a Run.
`ControlRuntime.list_deliveries` / `describe_delivery` give the Owner/operator a
read of each Delivery and its rolled-up state.

**Feishu text-only cutover (2026-07-20).** Feishu is the second authoritative
Channel, proving this ADR's platform-independence claim. Only the platform seams
are new: a `RawInboundV1` normalizer keyed on the globally unique Feishu
`message_id` as Source Request ID, a Reply Target of
`adapter=feishu, account_namespace=<app_id>, destination_id=<chat_id>` plus the
new optional `destination_kind`, and a single-attempt `FeishuDeliveryAdapter`
over `im.v1.message.create`. Everything else — `ControlRuntime`, `control.db`,
target sequencing, the Delivery Pump, Transcript freezing and digest
verification — is reused unchanged.

`destination_kind` is a small additive extension to the Channel Reply Target:
Feishu addresses one send by chat, open, union or user ID and a bare identifier
is not self-describing. It is optional, so an existing Telegram Reply Target's
canonical-JSON key is byte-identical to before.

One Feishu-specific hazard has no Telegram analogue and is worth naming: the
lark SDK is synchronous, so the provider call runs in an executor thread, and
**cancelling an executor future does not stop that thread**. A naive
`asyncio.to_thread` await would complete promptly on the Pump's timeout
cancellation, which the Pump reads as proof of termination -- it would then
record `unknown` and release the ADR 0063 Reply Target barrier while the real
send was still in flight, letting a later reply overtake or duplicate it. The
Adapter therefore shields the executor future and does not finish until the
provider thread actually stops, so an unresponsive call leaves the Attempt
unterminated and the Pump halts fail closed as ADR 0063 requires.

Two further Feishu-specific hazards are closed explicitly. The legacy
`_send_text_sync` retried three times internally; a retried send whose first
attempt reached Feishu duplicates a reply the control plane believes is
ambiguous, so the new Adapter performs exactly one call and reports transport
failure as `acceptance_unknown`. And the legacy parser downloads images/files as
a side effect, then synthesizes `[image]` text or embeds `[local path] /tmp/...`
when a download yields nothing; the handler therefore gates on the provider
message type *before* parsing, so no non-text message can reach a Turn, trigger
a download, or reintroduce the local-path side channel ADR 0059 retires. The
legacy Feishu direct-send and direct-dispatch methods are deleted rather than
left unused, along with the inbound download/parse helpers that produced them,
and `AUTHORITATIVE_CHANNELS` is now `{telegram, feishu}`.

The Feishu Adapter also supplies the opaque Delivery Item ID as Feishu's
request-dedup `uuid`, satisfying this ADR's "provider idempotency key wherever
supported" rule that the Telegram Adapter cannot satisfy. The key is per-Item
rather than per-Attempt, so a retry — including one issued after an
`acceptance_unknown` result — cannot produce a second visible message. An older
lark-oapi without the field degrades to non-idempotent rather than refusing to
deliver.

Group chats are admitted only when the message @-mentions this Bot, proved by
comparing the mention's open ID against a configured `FEISHU_BOT_OPEN_ID`. A
non-empty mention list is not sufficient — the Owner mentioning another human
would otherwise produce an unsolicited reply — and an unconfigured Bot open ID
fails group chats closed rather than guessing from member counts.

Cross-validated by an independent `codex exec gpt-5.5 xhigh` read-only review
(2026-07-20). It returned 1 P0, 4 P1 and 2 P2; all seven are resolved. The P0
was the executor-cancellation barrier release described above, which had no
Telegram precedent and was not covered by the reused Pump tests.

**Shared composition root (2026-07-20).** The one-authoritative-Channel-per-
process limit is lifted. `control.db` takes an exclusive lifetime lock, so one
Backend process owns exactly one control plane; the `ControlRuntime` is now
composed by the runner from every Channel's binding rather than by each Channel.

Channel startup is two-phase. `Channel.prepare_control_binding()` authenticates
far enough to name the account namespace and build the single-attempt Delivery
Adapter, returning a `ChannelSurfaceBinding`. The runner collects every binding
first — so a Channel that cannot authenticate fails the whole start rather than
leaving a half-composed control plane — then builds and starts one runtime via
`ControlRuntime.for_channel_surfaces`, injects it with
`bind_control_runtime()`, and only then calls `start()` so no Channel can submit
into an unstarted runtime. `for_channel_surface` remains as a single-binding
wrapper.

Little of the control plane had to change: the Delivery Pump already resolved
adapters by `(adapter, account_namespace)`. Two rules did need making explicit.
Owner scopes merge into one map but confer no cross-Adapter authority — a
Feishu Owner's subject presented on the Telegram Adapter is still
`owner_denied`. And attachment input is enabled **per Adapter**
(`attachment_input_adapters`) rather than as one process-wide switch, because a
shared runtime serves Adapters at different cutover stages: Telegram has an
Attachment Store cutover and Feishu does not, and OR-ing one flag would have
silently opened inbound bytes for Feishu.

Lifecycle ownership follows: the runner closes the shared runtime after every
Channel has stopped, and an individual Channel only *detaches* on stop or
startup rollback. A Channel that closed the runtime because its own polling
failed would tear down every other Channel's control plane.

This slice remains text-only. Telegram photo/document ingress fails before
download, Feishu inbound attachments/rich post/cards and outbound media fail
closed, outbound media Items are not implemented (so the over-long fallback
keeps the reply's start but does not yet attach the full text as a durable
artifact reference), and the official Channel runner rejects every remaining
Adapter until it has an equivalent ControlRuntime plus persistent Delivery
Adapter cutover. The legacy Adapter implementations remain source material, not
enabled production paths.

Outbound media remains outside the implemented slice. Legacy tools may still
write local paths into the process-global `pending_media` side channel, but the
authoritative Telegram path never drains it. Feishu no longer consumes it — its
`pending_media` drain and every legacy direct-send helper were deleted with the
2026-07-20 cutover — but Desktop retains legacy consumers outside this cutover.
A future media slice must replace those with verified durable artifact
references rather than treating the side channel as Delivery authority.

`omicsclaw/surfaces/desktop/outbox.py` is unrelated: it executes KG
HandoffPackets to close the idea-to-analysis-to-verdict workflow. It is a
scientific handoff queue/executor, not an Outbound Delivery Outbox, and must not
be reused as the delivery authority.

## Context

ADR 0052 guarantees that repeated inbound delivery resolves to the original
Turn and must not execute it again. That rule prevents duplicate scientific
work but also exposes a second failure domain: the Turn can finish while the
Channel reply fails to reach its Reply Target. Treating a provider redelivery
as a reason to rerun the Turn would destroy the idempotency guarantee.

Turn outcome, live observation and provider delivery are different facts:

- Turn Receipt says whether conversational/scientific execution reached a
  terminal outcome;
- Response Sink and Desktop SSE observe live or durable Turn state;
- Outbound Delivery says whether a frozen terminal reply or notice was accepted
  by an external Channel provider.

A persistent Outbox is required to retain the third fact across Backend
restart. It does not require a persistent chat queue, a second control-plane
process or an external broker.

## Decision

### One canonical terminal Delivery per accepted Channel Turn

An **Outbound Delivery** is the durable intent and operational lifecycle for
making one terminal Channel reply visible at the immutable Reply Target of an
accepted Turn. It has a control-generated globally unique opaque **Delivery
ID** that contains no Turn, Conversation, Surface, provider, target, ordinal or
timestamp semantics.

Control Plane State enforces at most one canonical terminal Delivery through a
unique `(turn_id, purpose=terminal)` relation. The Delivery freezes Turn ID,
Conversation ID, Surface, Reply Target, terminal kind and creation evidence. It
never follows the current Active Conversation Binding or recomputes a target
from current navigation.

Every accepted Channel Turn that becomes `succeeded`, `failed`, `canceled` or
`interrupted` receives a durable terminal reply or sanitized terminal notice.
Non-Owner input, rejected ingress and transport acknowledgements create no
Turn and no Outbound Delivery. Pure Desktop and CLI Conversations use
Transcript/status/Event observation and do not create a delivery merely to
reprint output after reconnection or process restart.

### Progress remains ephemeral

Typing indicators, token streaming, tool progress, temporary placeholders and
approval/interaction capabilities remain live Response Sink behavior. They are
not placed in the persistent Outbox and are not replayed after restart.

A terminal Channel reply cannot depend on successfully editing or deleting a
progress placeholder. v1 sends canonical terminal Delivery Items independently;
placeholder cleanup is best effort. Durable non-terminal approval or
interaction recovery, if required later, is a separate decision because the
current approval capability remains process-local Dispatch Context state.

### A Delivery is an ordered immutable provider-call plan

One Outbound Delivery contains one or more ordered **Delivery Items**. Each Item
represents exactly one intended provider send operation, such as one bounded
text chunk or one outbound media artifact. Item identity is opaque; ordinal is
stored separately. This permits a later Item to retry without resending Items
already confirmed as delivered.

Before terminal commit, a deterministic Surface renderer freezes the Item plan,
including content-source references, content digests, text ranges or bounded
media references, formatting/chunking version, media kind, caption reference
and ordinal. The exact schema belongs in the Phase 2 control-plane design.

Reply text remains in a Turn-attributed immutable Transcript entry. Outbound
media remains in a durable Run Artifact, ToolResult Blob or another explicitly
owned content store. `control.db` stores only their stable references, verified
digests and delivery-control metadata; it stores no Transcript body, media
bytes, absolute path, provider credential, signed URL or SDK object. Inbound
Attachment Reference and outbound media/artifact reference remain distinct.

The Delivery Pump resolves each reference and verifies its digest before an
attempt. Missing or changed content is an integrity failure; it never triggers
Turn or Run replay and never falls back to `pending_media` or a filesystem scan.

### Turn terminalization and Outbox insertion are one control transaction

For a Channel Turn, terminalization follows this ownership order:

1. durably commit and verify the terminal Transcript entry and every referenced
   outbound artifact outside `control.db`;
2. deterministically prepare the bounded Delivery Item plan;
3. in one `control.db` transaction, validate the non-terminal Turn, transition
   its Turn Receipt to the scientific/conversational terminal status, and
   insert the unique queued Outbound Delivery plus all Item control records;
4. publish the terminal Event to the live Turn event boundary and release the
   Conversation's Turn Sequencer slot;
5. wake the independent in-process Delivery Pump.

The Turn waits for durable delivery intent, not for the provider network call.
Provider success or failure therefore cannot change the Turn's terminal status.

If the process crashes after content commit but before the control transaction,
startup conservatively interrupts the Turn and ignores uncommitted terminal
content as delivery authority. If it crashes after the control transaction but
before provider send, the queued Delivery survives. Startup interruption
reconciliation creates its deterministic sanitized notice and canonical
Delivery idempotently in the same terminal control transition.

### The persistent Outbox stays inside the single-process control plane

The **Outbound Delivery Outbox** is the logical set of non-terminal Delivery
Items in Control Plane State. A restart-resilient **Delivery Pump** in the one
Backend process claims due Items, invokes the appropriate Delivery Adapter and
records the result. It is not a FIFO of executable Turns, a scientific Run
queue, the KG handoff queue or an external Worker protocol.

The Pump may schedule different Adapters and Reply Targets concurrently while
preserving Item ordinal within one Delivery. v1 relies on the same Backend
lifetime lock as the rest of `control.db`; a future multi-process delivery
fleet requires its own claim/fencing decision.

### Adapters perform one classified attempt

A **Delivery Adapter** resolves the configured provider client for the frozen
Surface and Reply Target, performs one provider request, and returns a
structured result. It contains no hidden retry loop and never receives a Turn
payload or authority to call `dispatch()`.

Each Item follows the control lifecycle:

```text
queued -> sending -> delivered
                  -> retry_wait -> queued
                  -> failed
                  -> unknown
```

An attempt result is classified as:

- `accepted`: provider acceptance is confirmed; store bounded non-secret
  provider message/reference evidence and mark the Item `delivered`;
- `not_accepted_retryable`: the provider is known not to have accepted it;
  increment attempt metadata and schedule bounded exponential backoff with
  jitter;
- `rejected_permanent`: a stable target, authorization, payload or provider
  rejection makes the Item `failed` until an explicit corrective action;
- `acceptance_unknown`: the request may have been accepted but no proof was
  received, so the Item becomes `unknown`.

A process crash while an Item is `sending` is treated as `unknown` unless a
provider idempotency key or reconciliation query proves acceptance or proves a
safe retry. Wherever the provider supports it, the opaque Delivery Item ID is
supplied as the client idempotency key.

Unknown Items are never blindly retried against a provider lacking idempotency
or reconciliation, because that would silently trade message loss for duplicate
replies. The record remains visible for Owner/operator action.

### Retry, resend and inbound redelivery remain separate

Automatic retry reuses the same Delivery and Item IDs only after a result that
proves the provider did not accept the Item, or through provider-supported
idempotency/reconciliation. It never reloads an Inbound Envelope.

`retry_delivery(delivery_id)` may resume the same non-delivered Delivery only
when safe non-acceptance is known. An explicit Owner **resend** after an unknown
or already-delivered outcome creates a new opaque Delivery ID linked through
`resend_of_delivery_id`; it reuses the immutable content references but creates
no new Turn or Run. This makes intentional duplicate visibility auditable.

Automatic Channel redelivery of the inbound provider message creates neither a
second Turn nor a second Delivery. It returns the original Turn; the original
Delivery independently remains queued, delivered, failed or unknown.

### Delivery capacity is bounded before scientific execution

The Backend enforces bounded outstanding delivery capacity per Adapter and a
bounded total. Each accepted non-terminal Channel Turn consumes one logical
terminal-delivery capacity unit until it is replaced by its Delivery Record;
each Delivery keeps that unit until it reaches a retained terminal delivery
state. Novel Channel ingress that cannot reserve capacity is rejected with
`delivery_backpressure` before Turn acceptance and before Agent, tool or Run
side effects. Duplicate lookup still returns the original Turn before this
gate.

Delivery Item count, text chunks and media items are also bounded during plan
construction. `delivered`, `failed` and `unknown` records retain identity,
attempt summary and provider evidence for audit and explicit action; content
retention follows the referenced Transcript/artifact and future governed purge
rules rather than an Adapter TTL.

### Migration removes direct terminal-send side channels

Channel renderers stop sending `Final.text` or `pending_media` directly. They
may continue rendering ephemeral progress Events, but terminal output is
created by the control-plane terminalization path and sent only by the Delivery
Pump.

The existing KG HandoffPacket `outbox.py` should be renamed to a handoff
queue/executor term during implementation so `Outbox` unambiguously means the
Outbound Delivery Outbox in control-plane architecture. Existing process-local
send attempts and media paths cannot be imported as delivered history unless
provider evidence proves their identity and outcome; ambiguous history is
reported rather than fabricated.

## Consequences

- A completed scientific Turn and a failed Channel send become independently
  observable facts.
- Provider outages and Backend restart no longer require rerunning the Agent or
  scientific tools to recover a reply.
- `control.db` gains narrow Outbound Delivery lifecycle and Item-plan metadata,
  but not reply or media bodies.
- Adapters become single-attempt transport ports with shared retry semantics.
- Partial multi-item delivery can resume after confirmed Items instead of
  resending the whole reply.
- Exactly-once provider visibility is not claimed. Unknown outcomes remain
  explicit where a provider offers neither idempotency nor reconciliation.
- Progress may disappear after restart by design; only terminal Channel
  delivery is durable in v1.

## Rejected alternatives

- **Rerun the Turn when reply sending fails.** Rejected because delivery and
  scientific execution are different effects and rerun may duplicate costly or
  irreversible tools.
- **Use inbound redelivery to resend the reply.** Rejected because a provider
  retry is not new Owner intent and must resolve to the original Turn.
- **Treat Desktop SSE as an Outbox consumer.** Rejected because SSE is
  observation of existing state; reconnecting must not create a new delivery or
  replay stale terminal output.
- **Persist progress and typing messages.** Rejected because they become stale
  after interruption and would turn observability into a durable workflow.
- **Let each Adapter implement its own retry loop.** Rejected because hidden
  retries lose cross-restart state and cannot distinguish safe retry from an
  unknown provider acceptance.
- **Blindly retry an unknown provider result.** Rejected because it can produce
  an unannounced duplicate reply; explicit resend records that choice.
- **Store reply and media bodies in `control.db`.** Rejected because the narrow
  control store owns identity/lifecycle while Transcript and artifact stores own
  content.
- **Put Outbound Delivery in a separate broker or Worker fleet.** Rejected for
  v1 because one Backend process and `control.db` already provide the required
  restart durability without broadening the control-plane deployment model.
- **Reuse the KG HandoffPacket outbox.** Rejected because it schedules
  scientific execution and records hypotheses, whereas delivery Outbox Items
  may only transmit already-computed terminal content.
