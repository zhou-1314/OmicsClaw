# Serialize Turns per Conversation with a bounded in-process FIFO

## Status

Accepted (2026-07-14).

Refines
[ADR 0040](0040-restart-resilient-transcript-persistence.md),
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md),
[ADR 0048](0048-resolve-conversation-with-active-reply-target-binding.md), and
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md).

**Turn-identity refinement (2026-07-14):**
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md)
keys each process-local Turn Execution by an opaque Turn ID and persists a
minimal lifecycle receipt. The FIFO remains non-durable and is never rebuilt
from those receipts.

**Observer-loss refinement (2026-07-14):**
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md)
supersedes only this ADR's sink-loss terminalization clause. Response Sink loss
now detaches an observer without canceling or failing the Turn; the Turn releases
its lease after publishing its terminal Event to the live Turn event boundary.
The single-writer and bounded non-durable FIFO decisions remain unchanged.

**Terminal Channel delivery refinement (2026-07-14):**
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md)
requires a Channel Turn's canonical Outbound Delivery to be atomically accepted
with its terminal Receipt before the terminal Event and FIFO release. Provider
network delivery happens afterward and never retains the Conversation lease.

## Implementation

Partially implemented across production Surfaces. The process-local
`TurnSequencer` owns reservation, immutable Envelope FIFO, one active lease per
Conversation, Worker activation, cooperative Turn cancellation, durable
terminalization and exact capacity release. Capacity includes reserved,
waiting, active and terminal-publication phases. Same-Conversation Turns
execute in FIFO order across the complete Worker lifetime; different
Conversations execute concurrently. Multiple `IngressNormalizer` instances use
one Sequencer-owned plan-to-commit admission guard, so durable acceptance and
FIFO append order cannot invert. Duplicate ingress observes the original Turn
and never wakes or executes it again.

`ControlRuntime` now composes the Sequencer with prompt-toolkit/single-shot CLI,
the Desktop text/multipart-image paths, Owner-only Telegram text and single-photo Turns, the canonical Turn-attributed
Transcript Store and a bounded `TurnEventHub`. The active lease covers candidate preparation, atomic
terminal Receipt + Transcript-ref commit, candidate promotion and terminal
Event publication. A Response Sink, renderer or SSE connection is only an
observer; slow/disconnected observers detach without changing execution or
FIFO ownership. Consequently the whole-Turn single-writer guarantee is active
for canonical CLI, Desktop text/multipart-image and Telegram text/single-photo Conversations, including
structured CLI `/new`, same-key Desktop/Telegram retry and Turn-ID cancellation.
Telegram terminalization atomically persists its canonical Outbound Delivery
before releasing the lease. The ADR remains partially rather than Surface-wide
implemented because Textual TUI and non-cut-over Channel handlers retain legacy
dispatch paths; the production runner and `ChannelManager` gate those Adapters
off until they migrate.

## Context

A Conversation is one ordered continuity stream. One Turn appends its user
message, repeatedly reads the current Transcript, may append assistant tool
calls and tool results, may replace history during compaction, and finally
appends a terminal response. Protecting individual list or SQLite mutations
does not make that read-plan-call-write sequence atomic.

If two Turns execute concurrently in one Conversation, the second can enter
the Transcript while the first Agent Loop is still running. The first loop may
then read the second Turn's message during a later tool iteration, compaction or
sanitization may operate on the other Turn's incomplete tool bundle, responses
can be delivered out of order, and the Stable prefix invariant no longer has a
well-defined history prefix. A lock around `append_*` only prevents a torn
write; it does not prevent overlapping reasoning over one history.

A global persistent chat queue would solve more than this problem requires and
would contradict the accepted local-first single-process control plane. The
required ordering boundary is the Conversation, while expensive Runs remain a
separate extensible execution-plane concern.

## Decision

The single-process control plane owns one bounded **Turn Sequencer** per active
Conversation:

```text
Conversation ID -> one running Turn + bounded FIFO waiting Turns
```

At most one Turn for a Conversation may execute at a time. Turns admitted for
that Conversation start in FIFO admission order. Turns belonging to different
Conversations may execute concurrently.

### Whole-Turn single-writer boundary

The sequencer acquires the Conversation's execution lease before the first
Transcript read or mutation, prompt assembly, model call, Agent tool action, or
context compaction for that Turn. It releases the lease only after all terminal
Transcript/control mutations are committed and the terminal Event has been
published to the live Turn event boundary. Delivery to any particular Response
Sink is not a precondition, and observer loss neither cancels nor fails the
Turn. Approval waiting and synchronous tool execution remain part of the active
Turn and retain the lease.

This is a whole-Turn single-writer rule, not a mutex inside `TranscriptStore`.
TranscriptDB's write lock remains useful for database integrity but does not
define conversational ordering.

### Waiting and activation

A waiting entry retains the immutable Inbound Envelope and only the ephemeral
process handles required to wait, cancel, and eventually render it. It performs
no Transcript, prompt-state, Agent, tool, or Run side effect before activation.
A fresh Dispatch Context is created when the Turn becomes active so effective
policy, approval and cancellation capabilities are current at execution time.

The FIFO is bounded. Its concrete capacity is an implementation and deployment
setting, but it may never become an unbounded backlog. When full, ingress
returns an explicit Conversation-busy/backpressure outcome that the Surface
renders; it never silently drops, merges, or indefinitely buffers the message.

### Durability boundary

The Turn Sequencer is process-local scheduling inside the control plane. It is
not a persistent inbound queue, cross-process EventBus, task center, or chat
Worker. Waiting Turns do not promise restart recovery and must not be written
to the Transcript before they start. A process failure may interrupt the one
active Turn under ADR 0040's partial-history recovery rules; it does not create
durability claims for the waiting FIFO.

### Cancellation and Conversation selection

Submitting a new Turn never implicitly cancels the active Turn. Cancellation
is explicit and targets an execution instance rather than deleting or
cancelling the Conversation as a whole. A future cancellation command may
define how waiting Turns are selected, but ordinary message admission never
uses last-write-wins cancellation.

Conversation resolution still happens before sequencing. Once an Inbound
Envelope contains a Conversation ID, a later `/new`, Project switch, or Active
Conversation Binding update cannot retarget that accepted Turn. This ADR does
not make `/new` an implicit cancellation command.

### Relation to Run execution

The sequencer protects conversational state, not all computation owned by the
Owner. Different Conversations may dispatch concurrently. Run execution may
also use subprocess, remote, parallel, or future Worker implementations behind
the Run Executor Seam; those execution-plane concurrency limits are separate
from the one-writer Transcript invariant.

## Consequences

- Transcript order, tool-call bundles, compaction, prompt assembly and terminal
  responses have one unambiguous Turn order per Conversation.
- A long Turn does not block unrelated Conversations owned by the same Owner.
- Burst traffic receives bounded local backpressure without introducing Redis,
  a persistent chat queue, or a separate chat Worker.
- Dispatch cancellation must be keyed by an execution instance rather than an
  overwrite-prone `Conversation ID -> latest task` dictionary alone.
- Surface-specific busy behavior and the concrete FIFO capacity need an
  implementation design, but cannot weaken the shared ordering contract.
- In-flight Agent Turns remain non-recoverable across process failure; this ADR
  deliberately does not expand the durability boundary.

## Rejected alternatives

- **Allow concurrent Turns and lock only Transcript writes.** Rejected because
  whole Agent Loops would still reason over overlapping, changing histories.
- **One global FIFO for all Conversations.** Rejected because an unrelated long
  analysis would block every other Conversation.
- **Persistent or cross-process queue for all chat Turns.** Rejected because it
  adds distributed ownership and replay semantics not required by the
  single-process control plane.
- **Unbounded per-Conversation queue.** Rejected because a stalled approval,
  model call, or Run could create unlimited memory growth and stale intent.
- **New message automatically cancels the active Turn.** Rejected because it can
  strand partial tool state and silently reinterpret the Owner's intent.
- **Merge waiting messages into one prompt.** Rejected because admission order,
  attachment ownership, cancellation, audit, and reply semantics become
  ambiguous.
