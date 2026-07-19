# Serialize Outbound Deliveries per Reply Target

## Status

Accepted (2026-07-15).

Refines
[ADR 0049](0049-immutable-conversation-address-ephemeral-response-sink.md)
and
[ADR 0060](0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md).

ADR 0060 remains authoritative for the persistent Outbox, single-attempt
Delivery Adapter, provider-outcome classification, and the separation of
delivery from Turn execution. This ADR adds ordering across Deliveries that
share one immutable Reply Target.

## Implementation

Telegram text live Pump integration implemented (2026-07-16). `control.db`
allocates target-local sequences atomically; due selection plus the transactional
claim barrier exposes only the earliest eligible prefix and permits at most one
active provider call for a Reply Target. The Pump fills independent target
slots concurrently, honors retry waits without globally head-blocking, and
rechecks the barrier at claim time. Pump ownership is scoped by Adapter account,
so a newly authenticated Bot cannot claim another account's target sequence.
If a timed-out provider coroutine does not terminate after cancellation, the
Pump records ambiguity and halts rather than releasing that target barrier.
Failed, retry-exhausted or
acceptance-unknown completion suppresses the remaining same-Delivery suffix in
the same transaction before a later target sequence may proceed. Focused tests
cover same-target serialization, slow-target isolation, safe retry, content
integrity failure, restart-to-unknown, retry-wait restart continuation,
foreign-account isolation, concurrent sequence allocation and suffix
suppression. Media and other
Channel Adapter cutovers remain unimplemented.

## Context

Per-Conversation Turn FIFO does not imply provider-visible reply order. One
Reply Target may have multiple Conversations, and one terminal reply may be
slow or retrying while a later Delivery is immediately sendable. If the Pump
uses only global `Delivery.created_at, Item.ordinal` selection with concurrent
provider calls, a later Turn's reply can become visible before an earlier
reply at the same destination.

Ordering only Items inside each Delivery is also incomplete after an Item
becomes permanently failed or acceptance-unknown. Higher Items must not remain
queued forever while still counting as nonterminal capacity, and sending them
would expose a partial reply after an unknown prefix.

The provider cannot always guarantee exactly-once visibility, especially after
an acceptance-unknown result. The control plane can still guarantee that it
never deliberately starts a later provider call at the same target while an
earlier Delivery has an unresolved sendable prefix.

## Decision

### Every Delivery receives a target-local sequence

When the control transaction creates a canonical terminal Delivery or an
explicit resend, it allocates a monotonically increasing `target_sequence` for
the immutable `(surface, reply_target_key)` address. The database enforces:

```text
UNIQUE(surface, reply_target_key, target_sequence)
```

Sequence allocation and Delivery insertion are one transaction. Timestamps and
opaque IDs are not ordering authority. An explicit resend receives the next
sequence and never jumps ahead of already accepted Deliveries.

### At most one provider call is active per Reply Target

The Delivery Pump may run different Reply Targets concurrently, subject to
Adapter and account limits. For one Reply Target it may have at most one active
provider call, and it selects only the lowest-sequence nonterminal Delivery.

Within that Delivery, ordinal `n` is eligible only after every lower Item is
`delivered`. No later Delivery is eligible until every lower target sequence
has reached a terminal Delivery summary.

This is a target-local causal barrier, not a global Channel FIFO and not a
Conversation execution lock. Provider network I/O never retains a Turn or
Conversation lease.

### An unrecoverable prefix suppresses the remaining Items

When one Item becomes `failed` or `unknown`, the same repository transaction
marks every higher nonterminal ordinal in that Delivery `suppressed` and records
the blocking Item ID. `suppressed` means the provider call was intentionally
not attempted because the ordered prefix could not be established; it is a
terminal Item state, not a retry outcome.

A Delivery summary is terminal when all Items are terminal:

- `delivered` when every Item is delivered;
- `failed` when the first non-delivered terminal Item is failed;
- `unknown` when the first non-delivered terminal Item is unknown;
- later suppressed Items do not override that first blocking outcome.

Once that summary is durable, the next target sequence may proceed. This avoids
an infinite target stall while preserving an audit record that the missing
suffix was never attempted.

An `unknown` result still means the provider may have accepted the earlier
Item. Therefore this policy cannot promise provider-visible exactly-once order;
it promises deterministic call initiation, no concurrent reordering by the
control plane, and explicit ambiguity.

### Capacity and recovery use the same barrier

Outstanding Delivery capacity counts Items in `queued`, `sending`, and
`retry_wait`. `failed`, `unknown`, `delivered`, and `suppressed` are terminal
for capacity purposes while their audit records remain retained.

On startup, any recovered `sending` Item is reconciled or becomes `unknown`,
its suffix is suppressed atomically, and only then may the next target sequence
be selected. A corrupted or missing sequence is an integrity error; the Pump
does not guess order from timestamps.

## Consequences

- The Pump never deliberately shows a later reply first at the same logical
  destination merely because its provider call was faster.
- Separate Reply Targets remain concurrent, so one failing destination does
  not globally block Channel delivery.
- Failed or unknown multipart replies have an explicit unattempted suffix and
  no longer occupy capacity forever.
- Resend order is deterministic and auditable.
- `control.db` needs target-sequence allocation, a `suppressed` Item state, and
  a blocking-Item reference.
- Tests must cover concurrent terminalization, same-target serialization,
  cross-target concurrency, retry waits, failure/unknown suffix suppression,
  restart recovery, and resend ordering.

## Rejected alternatives

- **Allow different Deliveries to progress concurrently at one target.**
  Rejected because per-Delivery Item order does not prevent cross-Turn reply
  reordering.
- **Order by `created_at` and opaque Delivery ID.** Rejected because timestamps
  may collide and neither value is a transactional target-local sequence.
- **Block the target forever after `unknown`.** Rejected because a provider
  ambiguity would make every future reply unavailable; explicit ambiguity and
  Owner resend are safer than an unbounded stall.
- **Send higher Items after a failed or unknown prefix.** Rejected because it
  exposes an incomplete terminal reply in an order the renderer did not plan.
- **Use one global Channel Delivery FIFO.** Rejected because unrelated Reply
  Targets should not share failure or latency.
