# Conversational control-plane Phase 2 reclosure audit

Date: 2026-07-15

## Outcome

Phase 2 is reclosed at the design level after an adversarial review of ADR
0043 onward, the integrated design, current runtime seams, resource metadata,
and focused contract tests.

This remains **design closure, not full code completion**. Later Stage 1 and
Stage 2a vertical slices on the same date added the isolated
`omicsclaw/control/` Repository foundation plus a test-only text/control-command
Ingress Normalizer. The current production runtime still constructs
`MessageEnvelope` independently at each Surface, does not call that Repository
or a Delivery Pump, and routes only Candidate plans through the shared resource
scheduler. Surface migration remains deliberately later work.

## Why the 2026-07-14 closure was reopened

| Finding | Risk | Resolution |
|---|---|---|
| Surface-supplied `owner_authenticator` and `workspace_resolver` | A Surface could supply the policy used to authorize itself | Owner admission and Workspace policy are Backend-owned Normalizer dependencies; a Surface provides only transport facts and attachment bytes |
| Live dynamic parent plus child global Resource Lease | Strict-FIFO parent/child deadlock and global head-of-line stall | ADR 0062 reserves one aggregate governed envelope and forbids nested global acquisition |
| Concurrent Deliveries at one Reply Target | Later replies can become provider-visible before earlier replies | ADR 0063 adds target-local sequence, one active call per target, and failed/unknown suffix suppression |
| Project archive versus delayed Memory projection, plus transport-partitioned dataset rows | Archive can either lose accepted research or permit unrestricted post-archive mutation; one dataset can fork by Surface identity | ADR 0064 adds explicit Memory scopes and frozen digest-bound Project Projection Intents |

## Effective integrated decisions

### Ingress security ownership

- Adapters verify provider/webhook authenticity and decode `RawInbound`.
- The Backend-owned `IngressNormalizer` owns Owner Identity configuration,
  Workspace resolution, limits, repositories, and admission order.
- The only request-local capability provided by a Surface is an
  `InboundAttachmentSource`; it cannot select policy or authority.

### Dynamic resource safety and contract alignment

- Canonical Skill compute metadata remains the five integer fields already used
  by `resources.compute`; one process slot is implicit and execution timeout is
  not a capacity field.
- Fixed plans obtain one global Lease per scientific process.
- Dynamic Runs obtain one global aggregate envelope that includes the live
  kernel and child processes, then use bounded Run-local allocations.
- Only 6/95 Skills currently have complete declared reservations. Canonical
  Run admission stays fail-closed for unready Skills; no optimistic migration
  default is introduced.

### Delivery causal order

- Canonical and resend Deliveries receive a monotonic sequence at their
  immutable Reply Target.
- At most one provider call is active per Reply Target; different targets stay
  concurrent.
- `failed` or `unknown` atomically suppresses all higher unattempted Items in
  that Delivery, allowing the next target sequence to proceed without claiming
  exactly-once provider visibility.

### Scientific Memory and Project archive

- Owner preferences/persona use Owner scope; Project knowledge and references
  use Project scope; local-file dataset observations use Workspace scope;
  accepted uploads retain Attachment identity.
- Projects reuse a Workspace observation or Attachment through an explicit
  Dataset Reference rather than copying or retagging canonical provenance.
- Novel Project Memory mutation requires `active`. One content-free Projection
  Intent created while active may later apply exactly its frozen,
  digest-verified effect after archive.

## ADR consistency corrections

The effective-decision index in `docs/adr/README.md` now makes the cumulative
chain explicit. Dated clarifications or later-ADR links also remove these stale
implementation traps:

- ADR 0046's historical `default` Project is explicitly Unassigned.
- ADR 0050/0051 no longer make a particular Response Sink a Turn-success
  precondition.
- ADR 0054 lists Run Assignments, Deliveries, and Projection Intents in the
  narrow Control Database and uses terminal-candidate recovery ordering.
- ADR 0055's Project-busy set includes `cancel_requested` Runs.
- ADR 0056 makes Run Receipt authoritative for Scope/lifecycle and Manifest the
  matching scientific provenance record.
- ADR 0061 explicitly delegates dynamic nested-resource semantics to ADR 0062.

## Architecture-document status

`docs/ARCHITECTURE.md` is now an architecture ledger, not an unqualified
current-state narrative. It labels the control-plane diagram as **accepted
target**, states the smaller production convergence on `dispatch()`, records
the isolated Repository/Normalizer slices as partial implementation, and treats
their production integration plus the missing Attachment Store, Outbox,
canonical Run admission, and unified scheduling as drift to implement.

## Validation performed at reclosure time

- All three SQL schema blocks in the integrated design execute successfully in
  empty SQLite databases with foreign-key checking enabled.
- Local Markdown links resolve across README, the architecture/context ledger,
  ADR 0040–0064, the integrated design, and both Phase 2 audit records (32
  files checked).
- Focused Repository, documentation, scheduler, and Candidate-plan contracts
  pass: 40 tests, including 12 new control-state tests and six documentation
  guards.
- Whitespace validation passes for every file touched by this reclosure.
- Residual scans find no effective-design use of Surface-supplied
  `IngressPorts`, the old resource field shape, concurrent same-target
  Deliveries, or the stale Run Scope/Response Sink clauses. The 2026-07-14
  closure retains those phrases only inside a clearly marked historical
  snapshot.

## Phase 3 entry sequence

1. **Completed 2026-07-15:** implement one deep `ControlStateRepository` Module:
   migrations, lifetime lock, atomic typed commands, invariants, and
   crash/fault tests, without connecting a production Surface.
2. **Stage 2a completed 2026-07-15:** add a test-only acceptance harness and the
   Backend-owned text/control-command Ingress Normalizer, with unsupported Attachment/File
   inputs and Channel acceptance rejected before side effects. Immutable
   Transcript/Attachment integration and Channel Delivery reservation remain
   future production work.
3. **Stage 2b completed 2026-07-15:** replace the admission-only handoff with an
   isolated whole-Turn Sequencer/Coordinator, exact active capacity, shared
   cross-Normalizer admission ordering, cooperative cancellation, failure
   quarantine, terminal-callback ordering and local no-replay startup
   reconciliation. It remains Worker- and Surface-agnostic.
4. Migrate CLI, then split Desktop submit/observe/cancel routes.
5. Migrate Telegram first to prove Owner admission, ingress idempotency,
   attachment handling, target-sequenced Outbox delivery, and restart recovery;
   migrate remaining Adapters only after that slice is green.
6. Introduce canonical Run admission and Assignment fencing. Generalize the
   global resource scheduler only after the resource SSOT is aligned and each
   enabled Run kind is calibrated; implement the ADR 0062 envelope before
   routing a persistent dynamic kernel through it.
7. Add explicit legacy import/cutover and remove composite Session identity,
   direct terminal sends, mutable attachment registries, observation-triggered
   execution, and runtime fallback to legacy stores.

The release gates remain process-kill fault injection at every named durable
commit, SQL migration/invariant tests, provider-ambiguity recovery, Project
archive/projection races, and no-replay verification.

## Post-reclosure Stage 1 evidence

`omicsclaw/control/` now implements the initial strict-SQLite schema, checksum
validation, integrity/foreign-key checks, symlink and owner-private path guards,
cross-process lifetime ownership, and domain commands for Project, Turn, Run,
Assignment, Delivery, Attempt and Projection Intent state. Tests cover
same-key concurrency, duplicate/conflict outcomes, migration tampering,
second-owner rejection, Project busy including `cancel_requested`, Assignment
fencing, target-local Delivery barriers, failure suppression, transaction
rollback, state reopen, and a real `os._exit` before commit. No Surface import
or production runtime call was added.

## Post-reclosure Stage 2a evidence

`IngressNormalizer.accept(raw)` is now the only test-slice admission Interface.
Backend configuration—not the Surface—owns trusted CLI/Desktop namespaces,
adapter/account-scoped Channel Owner Identity maps and Workspace identity. The
Module validates a versioned recursively frozen `RawInboundV1`, bounded source,
text and total JSON size, exact V1 local/Channel addressing, and the accepted
requested-option schema; it computes a canonical semantic fingerprint, returns
duplicates/conflicts before capacity checks, reserves bounded
per-Conversation/global admission capacity, atomically accepts the proposed
128-bit opaque Turn/Conversation identities, and queues a recursively frozen
`InboundEnvelopeV1`. A transaction-time Conversation mismatch causes bounded
replanning; a local post-commit enqueue failure terminalizes the original Turn
as `failed/dispatch_enqueue_failed`. Capacity rejection creates no
Conversation, Receipt or ingress binding. Attachments and File Selections are
rejected explicitly rather than converted into mutable paths. Valid Channel
Owner input is also rejected before acceptance until terminal-Delivery capacity
can be reserved.

At the Stage 2a closure, sixteen focused ingress tests plus fifteen Repository
tests passed. They covered
two-Normalizer stale-plan races, concurrent same-key duplicate collapse,
post-commit enqueue failure, identity/address binding, deep immutability,
bounded parsing, strict Backend authority configuration,
capacity-without-receipt and structured commands. The then-current object was an
admission FIFO only: `pop_next` released occupancy and did not grant the future
whole-Turn active execution lease superseded by Stage 2b below.

## Post-reclosure Stage 2b evidence

`TurnSequencer` now replaces the admission-only queue at the isolated seam. Its
bounded accounting includes reservations, waiting Envelopes and active Turns
until durable terminalization and the optional terminal callback finish. A
shared `serialize_admission` command covers every Normalizer's complete
plan-to-Envelope sequence, while Worker execution uses a separate lock so
different Conversations remain concurrent. Same-Conversation activation is
FIFO and non-bypassable; Context and Worker effects begin only after the Receipt
changes to `running`.

Failure paths are fail closed. A Worker exception yields only an allowlisted
generic terminal code. Turn/Run terminal codes are typed and status-specific;
schema migration 2 audits old rows and SQLite INSERT/UPDATE triggers reject
unknown or status-incompatible values even when the Repository is bypassed.
Arbitrary Worker-returned codes are mapped to a trusted generic outcome.
Explicit Owner cancellation is cooperative and wins atomically before
terminalization; external task cancellation becomes `interrupted` and is
re-raised instead of running successors as an Owner action. A terminal commit
failure retains the active lease and quarantines the Conversation. If Envelope
commit and durable failure compensation both fail, the reservation and capacity
remain held and the Conversation is quarantined; a partially appended Envelope
is removed only after durable compensation succeeds. Optional terminal callback
failure does not replay or retain a durably terminal Turn, but durable Event-gap
auditing is not implemented at this stage.

The explicit startup gate atomically changes prior-process local
`queued|running` Receipts to `interrupted/control_plane_restarted`, leaves
terminal Receipts unchanged, never rebuilds the FIFO and never calls the Worker.
It rejects nonterminal Channel state until Transcript and Delivery adapters
exist. Sixty focused control tests pass: nineteen ingress, twenty Repository
and twenty-one Turn-execution tests. They include a real parent/child
OS-process lifetime-lock probe, migration rejection of a legacy secret-shaped
code, direct-SQL trigger enforcement, malicious Worker-code normalization,
two-Normalizer order,
duplicate under pressure, partial commit, enqueue-plus-compensation failure,
capacity retention, same/cross-Conversation execution, cancel/completion races,
durable-start/local-transition failure, terminal commit/publisher failure,
runner supervision including pre-schedule cancellation, observer cancellation,
terminal-code validation, startup idempotence and a real `os._exit` after
Receipt commit before enqueue.

No production Surface or Agent Worker imports this Module. Transcript,
Attachment, Event Hub, reconnectable observation, Channel Delivery and the full
cross-Store startup barrier remain unimplemented; Backend health therefore
continues to advertise both control capabilities as false.

### Dual-repository compatibility remains unchanged

The companion OmicsClaw-App change establishes only a forward-compatible wire:
both real send paths create one source request ID per submission; the Next.js
proxy persists server-owned installation identity, fixes the Owner profile and
forwards all four v1 fields to local or remote Backend connections. Older Apps
may omit the additive fields and older Backends may ignore them. The Backend
health descriptor deliberately keeps `authoritative_ingress` and
`durable_ingress_idempotency` false, so the App neither retries nor treats this
wire preparation as a production cutover. The source request ID is not yet
persisted across renderer reload.

The production Desktop chat write routes now reuse the existing conditional
remote Bearer gate: when `OMICSCLAW_REMOTE_AUTH_TOKEN` is configured, missing or
incorrect credentials fail before `/chat/stream`, abort, permission, profile or
title handling; when unset, local behavior remains compatible. This closes an
existing remote execution-entry gap but does not make the preparatory four
ingress fields authorization facts.
