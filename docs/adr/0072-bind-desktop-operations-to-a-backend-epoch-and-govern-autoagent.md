# Bind Desktop operations to one Backend epoch and govern AutoAgent workers

## Status

Accepted.

Implementation: local candidate implemented across OmicsClaw and
OmicsClaw-App on 2026-07-19. Final verification gates and the mandatory
independent review are pending; neither local evidence nor this ADR is a
`SHIP` verdict.

Refines [ADR 0071](0071-authenticate-the-desktop-api-before-routing.md). It
does not change the Backend/App ownership boundary established there.

## Context

ADR 0071 authenticated each Desktop request and froze one authority snapshot
for a multi-request operation. That was not enough for resources observed by
later, independent HTTP requests. A Job created through Backend A could be
queried or cancelled after the active profile moved to Backend B. A Chat
permission response could likewise reach a different process from the one that
paused the tool. AutoAgent added a harder failure window: Backend could accept
`/autoagent/start` while the App lost the response, leaving neither a safe
replay nor a safe session-only cancellation.

Process-local tunnel state had a related ambiguity. Loss, delayed callbacks,
or local-port reuse could make a later request address a different SSH target
through the same loopback port. Terminal EOF and transport failure also did not
prove that a Backend resource had reached a lifecycle terminal, so retention
could either leak indefinitely or delete active authority.

AutoAgent execution itself ran in a Backend thread. A thread has no durable
process-tree owner, cannot be reconciled after Backend restart, and cannot
provide a stopped-tree proof before publishing terminal evidence.

## Decision

### Persist routing and Control identity, never credentials

OmicsClaw-App persists a bounded operation binding for each independently
addressable Job, Chat generation, Chat permission alias, and AutoAgent session.
The binding freezes target kind, profile identity, profile authority revision,
non-secret target fingerprint, Backend process epoch, and a random binding
epoch. AutoAgent bindings additionally freeze the immutable Backend
`control_authority_id`. It never stores a bearer value, credential digest,
request payload, scientific result, or permission capability.

Migration 12 creates `control_authority_id` as an exactly 64-character
lowercase hexadecimal singleton in `control.db`. Restarting a Backend over the
same database preserves it; a new database receives a different value; copying
or restoring the database preserves its identity. The authenticated full
`/health` and `/autoagent/capabilities` wires expose it when the Control Runtime
is bound. The public minimal health response does not.

Every later status, events, artifact, cancel, permission, result, promotion, or
save request reconstructs that exact target and proves its current profile and
tunnel publication before dispatch. A profile switch affects only later novel
operations. An authenticated Backend process-epoch mismatch fails closed,
except for the receipt-bound AutoAgent reconciliation described below.

AutoAgent may re-attest a changed process epoch only when the authenticated
Backend presents the exact stored `control_authority_id`. A legacy AutoAgent
binding without that identity is quarantined content-free with
`legacy_control_authority_unavailable`; it cannot status, abort, reconcile, or
retarget by guessing the active profile. Job and Chat operation bindings remain
strictly process-epoch-bound.

SSH publication is generation-bound and includes the profile, resolved-target
fingerprint, profile revision, local port, and App process epoch. Active sockets
and channels are destroyed on loss. A retired rejecting listener holds the port
until the exact SSH child exit is observed, preventing a stale operation from
silently reaching a new tunnel after port reuse. Active plus retired tunnel
capacity is bounded.

### Retire bindings only from exact terminal evidence

Job and Chat proxies recognize only their canonical Backend terminal wires.
EOF, disconnect, malformed frames, HTTP failure, and transport failure do not
invent lifecycle state. Permission aliases are linked to the exact Chat parent
before the prompt is exposed. A Backend `done` closes the parent and linked
permissions; a Chat error closes only the parent so a still-pending decision is
not falsely resolved.

The Backend emits a process-local HMAC approval capability in each permission
request. The App validates and relays it without persistence; Backend compares
it in constant time before resolving the pending Future. Request IDs and tokens
use closed, high-entropy wire formats.

Routine and near-capacity cleanup delete only accepted bindings with exact
terminal evidence and no pending cancellation. A terminal Chat parent with a
live linked permission child is protected from foreign-key cascade. Admission,
collision detection, bounded terminal cleanup, and insertion share one SQLite
`IMMEDIATE` transaction. Job and Chat forwarding is demand-driven and both
producer and observer frames have explicit byte limits with delimiter recovery.

### AutoAgent creation is receipt-bound and durable

The App generates the 128-bit session ID and a private 256-bit creation receipt
before the first `/autoagent/start` side effect. It reserves the immutable
routing binding, verifies the Backend capability contract on the same
epoch-fenced context, and overwrites any Renderer-supplied receipt. Only an
exact receipt-confirmed start response can promote the binding from `reserved`
to `accepted`.

Reservation is novel-only: a duplicate resource key conflicts before
`/autoagent/start` and cannot mutate the existing binding. One SQLite
`IMMEDIATE` transaction inserts the immutable binding and an untouched
`reserved + pending` creation guard whose first cancellation attempt becomes
eligible after 12 seconds. The App's start deadline is 10 seconds only until
response headers arrive; it is cleared after headers so it does not terminate a
valid long-lived SSE body. Exact normal start acceptance may clear only the
still-untouched guard with zero attempts. Explicit Stop, an unknown outcome, or
a reconciler claim makes the cancellation intent durable and prevents that
acceptance path from erasing it.

An abort or unknown start result first persists a cancellation intent. Delivery
uses the original immutable target plus creation receipt and calls only the
idempotent `/autoagent/abort-receipt/{session_id}` Interface; it never invokes
start or `/reconcile`, and never guesses from the active profile. A short
SQLite claim lease, exponential backoff, App startup hook, recovery
observation, and restored-tunnel notification drive retries across requests
and process restarts. An exact pre-accept tombstone is a terminal success, not
an unknown error.

Backend `control.db` is authoritative for AutoAgent session state. Acceptance
stores immutable scientific identity, output authority, receipt digest, and
process-tree owner reference in one bounded schema. It does not store provider
credentials or an executable replay payload. Restart reconciles the recorded
owner, then terminalizes an interrupted session only after tree-absence proof;
it never recreates or replays the optimization.

AutoAgent SSE terminal frames are compact receipts containing only session,
terminal status, and a closed error code. Full scientific results are read
through the bounded `/results` Interface after durable terminal validation.
The App recovery index is content-free and bounded. Renderer transport failure
keeps the durable session authoritative and polls until an explicit terminal
or caller cancellation.

The App terminalizes from SSE only on exact matching
`done {session_id,status}` or `error {session_id,status,error_code}` terminal
shapes with status-appropriate closed values. Status polling terminalizes only
an exact `{error,result,session_id,status}` envelope with matching Session and
valid terminal semantics. `/results` never terminalizes, even if its payload
contains a `status` field. EOF, HTTP or transport failure, malformed or
oversized input, and a nonterminal stream end likewise never invent terminal
evidence. Accepted/reserved non-pending status recovery may use Backend
`/reconcile`; that observation path is distinct from the receipt-bound
cancellation reconciler above.

### Bound every AutoAgent transport

Backend `/autoagent/start` reads at most 1 MiB within 60 seconds after the
route-wide bearer gate, then requires exact Content-Length agreement, strict
UTF-8 and duplicate-key-free finite JSON with maximum depth 12, 21,000 nodes
and 128 decimal digits per integer. Duplicate Content-Length is invalid. App
command readers allow at most 1 MiB for start and 64 KiB for abort, promote,
commit, and Chat permission commands, with a 10-second whole-body deadline by
default.

Worker IPC permits a 1 MiB request, 256 KiB progress events, at most 8192
events and 16 MiB aggregate progress. A terminal/result frame is bounded by
the configured result ceiling plus 64 KiB, currently about 4 MiB + 64 KiB.
Backend SSE uses finite Unicode JSON with 256 KiB per datum and a 17 MiB
aggregate ceiling. The App start proxy uses zero high-water-mark demand
forwarding; its 64 KiB terminal observer discards an oversized progress frame
and resumes at the next delimiter. Status and result bodies are capped at
4 MiB + 256 KiB. Provider credentials cross only the authenticated nonce-bound
worker IPC after peer-credential proof.

### AutoAgent execution requires a governed owner

On Linux, Backend pre-generates a user-systemd scope reference before durable
acceptance and launches the AutoAgent worker through the existing bubblewrap
Adapter. The exact writable output directory is the only mutable project path;
stdout and stderr are discarded, while progress and terminal evidence use a
bounded framed-JSON Unix-socket protocol. Peer credentials and a nonce
handshake precede delivery of the request. Provider/model/credential resolution
must succeed before acceptance; the credential crosses only this bounded IPC
boundary and is absent from the database, argv, environment, logs, and result.

The worker result is provisional. Backend first validates result identity and
proves the exact scope process tree absent, persists owner-stop evidence, then
commits the durable terminal and emits the compact terminal receipt.
The receipt-confirmed SSE response is only an observer: downstream disconnect,
iterator cancellation, EOF, or observer bounds release that observation and
never request execution cancellation. Only the explicit receipt-bound abort
Interfaces may persist cancellation intent or signal the governed owner.
If that final SQLite commit is temporarily unavailable, the same governed
worker task retains at most one bounded terminal intent, emits one content-free
transport-loss notice without closing the stream, and enters a process-wide
admission quarantine. It retries with exponential backoff capped at five
seconds; status observation, receipt cancellation and shutdown may wake that
existing loop but cannot create another one. No terminal receipt or finished
marker is published until the durable commit succeeds. Shutdown instead wakes
the loop and hands the already-stopped owner to the existing durable
`backend_shutdown_interrupted` reconciliation; neither path reconstructs an
executable request.
Cancellation, protocol failure, startup failure, shutdown, and restart all use
the same exact owner reconciliation. Hosts without user-systemd plus bubblewrap
report zero governed capacity and reject before acceptance; there is no thread
or ordinary-process fallback.

Manual promotion and evolved-config publication remain Backend mutations.
AutoAgent workers always run with `auto_promote=false`; the App is only a thin
command and rendering adapter.

Backend therefore owns Control identity, receipt verification, durable
capacity, raw-body parsing, provider resolution, worker/owner lifecycle,
terminal result authority, promotion, save, and scientific policy. The App
owns non-secret target routing, private receipt retention, bounded retry
scheduling, thin proxies, recovery projections, and UI interaction. It is not a
second scientific lifecycle or persistence authority.

## Consequences

- Switching profiles or replacing a Backend process cannot retarget an already
  bound Job, Chat permission, or AutoAgent session.
- An App or transport crash cannot turn an unknown AutoAgent start into a
  duplicate replay or an uncancellable orphan.
- Terminal retention is evidence-driven and bounded rather than EOF-driven.
- AutoAgent restart recovery is interruption plus reconciliation, not execution
  replay. Users must explicitly start a new optimization after interruption.
- Production AutoAgent execution is currently Linux-only. macOS and Windows
  fail closed until they gain an equally observable process-tree owner.
- The systemd scope proves ownership and absence; it is not a calibrated
  CPU/memory quota. Bubblewrap and `SO_PEERCRED` do not claim protection from a
  malicious process running as the same OS user.
- Native packaged Windows/macOS lifecycle and Windows evolved-config safety
  still require platform smoke testing where those capabilities are enabled.
- This milestone hardens Desktop routing and one Skill-evolution execution
  Surface. It does not by itself complete Skill representation, acquisition,
  retrieval, validation, promotion, demotion, or scientific calibration for
  the full Skill audit system.

## Alternatives considered

- **Resolve every request from the currently active profile.** Rejected because
  a UI setting is not resource authority and can splice two Backends into one
  lifecycle.
- **Persist bearer credentials with each binding.** Rejected because target
  continuity requires identity and revision evidence, not another secret
  store.
- **Retry `/autoagent/start` after an ambiguous response.** Rejected because a
  non-idempotent retry can create duplicate scientific work.
- **Treat EOF or proxy failure as terminal.** Rejected because transport state
  is not Backend lifecycle evidence.
- **Keep the in-process AutoAgent thread as a fallback.** Rejected because it
  cannot supply durable owner identity or stopped-tree proof.
- **Move lifecycle policy or scientific mutation into OmicsClaw-App.** Rejected
  because Backend remains the sole execution, persistence, governance, and
  mutation authority.
