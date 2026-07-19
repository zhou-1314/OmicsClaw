# Conversational control-plane Stage 2b Ask Codex cross-validation

## Scope and status

This record covers the final-candidate isolated Stage 2b control-plane slice in
OmicsClaw and its compatibility changes in OmicsClaw-App. It does not claim a
production cutover. The Backend health contract continues to report
`authoritative_ingress=false` and `durable_ingress_idempotency=false`; no
production Surface, Agent Worker, Transcript, Attachment Store, Event Hub or
Delivery Pump imports the isolated control Module.

The review used fresh, read-only Ask Codex CLI sessions rather than the
implementing agent or an in-process subagent. Each failing finding was repaired
and regression-tested before the next independent pass.

## Review trail

### Pass 1 — FAIL, two High findings

Ask Codex session `019f64d4-4349-7d71-ac99-9088d3894286` found:

1. the App release pairing validator accepted a Backend with either control
   capability already set to `true`; and
2. a tool timeout still scheduled an implicit new message submission after
   500 ms.

Closure:

- every bundled-runtime release target now requires the pinned Backend's exact
  non-authoritative v1 contract with both flags equal to the Boolean `false`;
  tests independently flip each flag and require rejection;
- the removed `sendMessageFn` callback is no longer part of stream execution;
  both timeout completion paths wait beyond the former 500 ms window and prove
  zero automatic resubmissions.

### Pass 2 — FAIL, one High, one Medium and one Low finding

Ask Codex session `019f64e3-5521-7431-a7d1-22e828b03e1b` independently proved
that the first-pass High findings were closed, then found:

1. **High:** the lowercase-format terminal-code check still allowed a
   credential-shaped value such as `sk_sensitivecredential123` into authority;
2. **Medium:** the committed lifetime-lock test used only the owning process and
   did not prove OS-process exclusion; and
3. **Low:** the immediate timeout status still said `Retrying` or `Running`
   despite the absence of automatic retry.

Closure:

- Turn and Run terminal codes now use typed, status-specific closed
  vocabularies. Migration 2 audits existing rows before installing SQLite
  INSERT/UPDATE triggers; Repository/model validation uses the same sets and
  arbitrary Worker-returned codes map to trusted generic outcomes. Regression
  coverage includes migration, direct SQL, model, Repository and malicious
  Worker paths;
- a real parent/child subprocess probe now proves the lifetime lock rejects the
  child while the parent owns it and accepts the child after release;
- both App send paths use a shared timeout formatter that says the current Turn
  is stopping. The immediate snapshot test rejects both `retrying` and
  `running`, while still proving zero resubmissions.

### Pass 3 — FAIL, one High and one Medium finding

Ask Codex session `019f6510-26ac-7941-9667-7260ef7a5dc9` independently
confirmed the first two passes' closures, then found:

1. **High:** after a tool timeout changed the stream to `stopped`, the proactive
   context compactor could still submit `/compact` automatically when occupancy
   was at least 90%; and
2. **Medium:** migration 2 generated its SQL and checksum from the live terminal
   code maps, so adding a code alongside migration 3 would retroactively change
   migration 2 and prevent an existing version-2 database from upgrading.

Closure:

- the App now maintains a session-keyed automatic-submission quarantine outside
  transient stream snapshots. A tool timeout sets it before any terminal
  snapshot is emitted; it survives stream GC, component unmount and the
  new-chat-to-session route transition. Both automatic compaction call sites
  are ineligible while quarantined and identify themselves as `automatic` at
  the lower stream boundary, which rejects them again. Only an explicit user
  submission or a genuinely new session ID proceeds. Both timeout paths use
  real `/api/chat` request counts and remain at exactly one after an attempted
  automatic `/compact`;
- migration 2 now builds only from immutable literal V2 Turn/Run policy
  snapshots. Migration 1 and 2 SHA-256 values are fixed, module loading fails
  closed on source drift, and a separate latest-schema alignment regression
  prevents the live maps changing without a new migration. The migration-3
  renderer demonstrates audit-before-replace semantics and preserves all V2
  triggers when an audit fails.

## Pre-Pass-4 candidate verification

Backend:

- `tests/control`: 64 passed — 19 ingress, 24 Repository and 21 Turn execution;
- control, Desktop wire/auth and documentation selection: 106 passed;
- Ruff format check: 17 files already formatted;
- Ruff check and Python bytecode compilation: passed.

App:

- TypeScript typecheck plus complete unit suite: 1,428 passed, zero failures;
- bundled Backend pairing regression: 3 passed;
- targeted ESLint: zero errors; four pre-existing `ChatView.tsx` warnings and
  one ignored-Python-file warning remain outside this slice;
- timeout/auto-compaction/ingress/health selection: 30 passed and exits without
  retaining a stale five-minute stream-GC timer;
- the repository-wide lint baseline still has unrelated pre-existing failures,
  so this slice is evaluated with the targeted zero-error gate above.

## Intentionally deferred production boundary

The isolated callback can report `event_published=false`, but durable Event-gap
auditing and reconnectable observation do not exist. The future production
slice still requires a deep Backend Turn Client/Surface seam, Transcript and
Attachment adapters, Event Hub observation, canonical Channel Delivery and the
full cross-store startup barrier. None of those omissions is masked by a
capability flag or a release-pairing claim.

## Latest independent pass and repair status

### Pass 4 — PASS at the Blocker/High gate, with one Medium and three Low findings

Ask Codex session `019f6534-4331-7091-b909-3c1646016940` found no Blocker or
High issue, so the candidate passed the review's stated release threshold. It
did not provide an unqualified closure: the reviewer recorded one Medium and
three Low follow-ups:

1. **Medium:** a custom Worker-originated `BaseException` could escape the
   `Exception` handlers without explicitly quarantining its Conversation;
2. **Low:** `TurnTerminalOutcome` and `RunReport` construction relied on type
   annotations rather than independent runtime status/code validation, while
   downstream normalization, Repository validation and SQL triggers remained
   fail-closed;
3. **Low:** request-count coverage existed at the shared stream boundary, but
   no component-level regression drove the separate new-chat path through
   session creation, timeout, route transition and attempted auto-compaction;
   and
4. **Low:** `OmicsClaw-App/docs/bundled-backend.md` still described release
   integration as future work and incorrectly said every standalone matrix
   cell built and uploaded a runtime.

Post-review repair status:

- the documentation Low is closed: the App document now states that
  `build.yml` builds the four real bundled targets inline from the release-pinned
  Backend SHA, validates the exact non-authoritative V1 capability contract,
  and publishes macOS x64/Windows arm64 only with `SKIPPED`/BYO. It separately
  identifies `backend-runtime.yml` as a mutable-ref-capable verification and
  artifact workflow that is never pairing authority for releasable installers;
- the Medium is closed: a custom Worker `BaseException` now quarantines its
  Conversation while retaining the running Receipt, active lease and capacity,
  then propagates unchanged. Coordinator-owned tasks surface the same failure
  through both `wait_idle()` and `close()`;
- the terminal-model Low is closed without weakening the untrusted Worker seam:
  `TurnTerminalOutcome` remains a deliberately permissive DTO normalized by the
  Sequencer, while typed `RunReport` validates its status/code contract at
  construction and Repository/SQL checks remain authoritative;
- the first new-chat coverage repair exercises the extracted submission
  boundary through session creation, the first `/api/chat`, tool-timeout
  quarantine, route callback and post-unmount automatic `/compact` attempt.
  Its request count remains at one until an explicit user submission clears the
  gate; Pass 5 below records and closes the missing page-to-boundary coupling;
- post-repair Backend selection: 114 passed, including 70 control tests (19
  ingress, 27 Repository, 24 execution); Ruff check/format and compileall pass;
- post-repair App verification: typecheck plus the complete unit suite passes
  1,438/1,438; the control-plane selection passes 40/40; the pairing suite
  passes 3/3; targeted ESLint has zero errors and four pre-existing ChatView
  warnings; and
- a fresh post-repair Ask Codex pass is still required before calling the whole
  cross-validation trail final.

### Pass 5 — PASS at the Blocker/High gate, with one Medium and four Low findings

Ask Codex session `019f6558-4038-7471-87c4-9d5bf84d2ed5` independently
confirmed that the Pass-4 Blocker/High gate remained closed. It found one
Medium and four Low follow-ups:

1. **Medium:** a permissive `TurnTerminalOutcome` could carry a non-string,
   unhashable status such as `[]`; membership normalization raised `TypeError`
   before the Sequencer could durably terminalize or quarantine the Turn;
2. **Low:** the new-chat timeout regression drove the extracted helper but did
   not prove that `NewChatPage` still delegated its only first-turn request to
   that boundary;
3. **Low:** the release-pairing test mainly counted workflow strings and could
   miss a refactor that moved the right commands into the wrong job or order;
4. **Low:** comments in the standalone runtime workflow could be read as
   release-pairing authority even though `build.yml` owns that contract; and
5. **Low:** the one-observation guarantee for coordinator `BaseException`
   failures and replacement-stream GC-timer fencing lacked direct regressions.

Post-Pass-5 repair status:

- Worker outcome normalization now rejects every non-string status before set
  membership and canonicalizes string subclasses to built-in strings. A
  non-string or unsupported status becomes trusted
  `failed/invalid_worker_outcome`; once a legitimate status is canonicalized,
  a malformed code maps to that status's closed generic fallback
  (`worker_failed` or `canceled`). In every case the original Receipt
  terminalizes, its lease and capacity release, its successor runs, and a
  subsequent Turn can be admitted and completed;
- the new-chat regression is now explicitly named a source-wiring contract. It
  proves that `NewChatPage` imports and invokes exactly one
  `runNewChatSubmission` boundary, performs no direct first-turn `fetch`, and
  supplies the session-created, timeout, terminal, navigation and abort hooks
  exercised by the behavioral helper test. This is intentionally not claimed
  as a rendered-DOM component test;
- the pairing suite parses workflow jobs and ordered steps. It proves all six
  package jobs depend on `ci`, the four bundled targets validate a pinned
  `$OMICSCLAW_REF` before packaging, the two BYO targets never build a runtime,
  and the gate accepts only an exact lowercase 40-hex SHA under release/tag
  conditions;
- `backend-runtime.yml` now describes itself only as a diagnostic/manual
  artifact workflow and explicitly disclaims release-pairing authority;
- direct regressions prove that whichever of `wait_idle()` or `close()` first
  observes a custom `BaseException` records and raises it exactly once, while
  the second observer returns cleanly. A separate fake-timer regression proves
  a replacement stream clears its predecessor timer and stale predecessor
  callbacks cannot delete or re-arm GC for the active replacement;
- post-repair Backend selection: 119 passed, including 75 control tests (19
  ingress, 27 Repository, 29 execution); Ruff check/format and compileall pass;
- post-repair App verification: TypeScript plus the complete unit suite passes
  1,440/1,440; the focused control-plane selection passes 50/50; semantic
  release-pairing tests pass 4/4; targeted ESLint has zero errors and the same
  four pre-existing `ChatView` warnings; and
- a sixth fresh, read-only Ask Codex pass remains required for the final
  cross-validation verdict.

### Pass 6 — FAIL, one High and two Low findings

Ask Codex session `019f6575-3d98-70e1-a6b0-4412098dc7f6` independently
confirmed the Backend outcome, supervisor, release-pairing, capability and
frozen-migration closures, but found one new High in the App's replacement
stream lifecycle:

1. **High:** after a new stream replaced an active stream for the same session,
   the predecessor's late abort catch still emitted `completed` to the
   session-keyed listener registry. `ChatView` could therefore replace the new
   active snapshot with the old stopped snapshot, re-enable the composer and
   automatic compactor, and abort the real replacement with another request;
2. **Low:** the page wiring regression used regular expressions whose direct
   `fetch` check could be bypassed by another JavaScript spelling; and
3. **Low:** the Pass-5 prose overstated malformed-code normalization, and the
   correct built-in canonicalization of hostile `str` subclasses had no formal
   regression.

Post-Pass-6 repair status:

- the stream manager now owns an object-identity generation fence:
  `getStreamsMap().get(sessionId) === stream`. Non-current streams may clean up
  their private timers but cannot emit listener/window events, mutate
  session-visible state, schedule timeout fallbacks, terminalize, refresh UI
  resources, change mode, publish termination or ask-user state, or quarantine
  automatic submissions. Both normal completion and catch paths recheck owner
  identity after cleanup and return silently when superseded;
- the replacement regression now deliberately keeps the predecessor reader
  alive after abort, sends late `mode_changed`, `tool_timeout` and `done` frames,
  and proves zero predecessor terminal events, mode/termination callbacks,
  timeout quarantine or GC handles. It then completes the current owner and
  proves exactly one new listener/window completion and one current-owner GC
  handle;
- the new-chat wiring gate now parses the TSX AST, locates the actual
  `sendFirstMessage` callback, counts its sole boundary call, and rejects
  identifier, property, element-access or aliased `fetch` references plus
  embedded chat endpoint literals. The separate behavioral helper test still
  drives real request counts; this remains honestly described as a source
  wiring contract rather than a rendered-DOM test;
- the review prose now distinguishes invalid status from status-specific code
  fallback. A hostile `str` subclass whose `__hash__`, `__eq__` and `__str__`
  all raise is canonicalized without invoking them, durably terminalizes with
  built-in strings, releases capacity and lets successors complete;
- post-repair Backend selection: 120 passed, including 76 control tests (19
  ingress, 27 Repository, 30 execution); Ruff check/format and compileall pass;
- post-repair App validation: TypeScript and targeted ESLint pass; the complete
  control-plane selection passes 50/50 (including the focused stream/new-chat
  9/9) and semantic release pairing remains 4/4. The current dirty App
  worktree's full suite is 1,446/1,447 because a concurrently added,
  control-plane-unrelated Skill Validation Review test
  (`review actions remain frozen after uncertain decision state until
  authoritative reload recovers`) leaves its decision dialog open before
  querying the hidden page action. The failure reproduces in that file alone,
  so this record does not claim the entire shared worktree is green; and
- a seventh fresh, read-only Ask Codex pass is required because Pass 6 found a
  High and the ownership repair materially changed runtime behavior.

### Pass 7 — FAIL, one High finding

Ask Codex session `019f6593-8168-7451-98e5-98c256764703` independently
confirmed every Backend, submission-quarantine, release-pairing, capability,
migration and production-scope closure, but reproduced a synchronous
re-entrancy hole in the App stream owner fence:

1. **High:** `emit()` checked owner identity only at entry. Its listeners and
   window handlers are synchronous, so an earlier listener could call
   `startStream()` for the same session and install a replacement while the
   predecessor's stack continued. Later listeners/window consumers could then
   receive the predecessor terminal snapshot; the old path could also start a
   displaced request, refresh resources, schedule a timer, or clear the shared
   Backend SDK-session binding after the replacement became current.

The reviewer reproduced later-observer `completed:completed` after the current
map already held `phase=active`, and separately verified stale resource effects.
The prior regression covered asynchronous late frames and GC, but deliberately
did not replace from inside a listener callback.

Post-Pass-7 repair status:

- `emit()` now returns whether the exact stream still owns the session after
  the entire broadcast. It rechecks before and after every listener, before the
  window boundary, and after synchronous window dispatch; once ownership moves,
  no later observer receives the predecessor event;
- `startStream()` validates ingress before mutating a healthy owner and returns
  when its phase broadcast loses ownership. `runStream()` repeats the guard at
  entry, so a re-entrant replacement cannot be followed by a displaced fetch;
- every post-broadcast side-effect seam now consumes the owner result: tool and
  terminal resource refreshes, connected-status timers, tool-timeout interrupt,
  idle-timeout SDK-session PATCH, GC, and permission submission/cleanup. The
  tracked timeout helper also refuses to create a timer for a non-owner;
- two synchronous regressions place the replacing listener before an observer.
  The phase case proves only the replacement request starts and only its phase
  reaches later listener/window observers. The terminal case proves no old
  completion or refresh escapes, then completes the replacement and observes
  exactly one completion and one refresh; and
- post-repair validation is Backend 120/120, App control-plane 52/52,
  semantic release pairing 4/4, clean TypeScript and targeted ESLint, plus clean
  diff checks. The shared App worktree still contains concurrent, unrelated
  Skill Validation/Catalog work, so no whole-worktree green claim is made.

An eighth fresh, read-only Ask Codex pass is required because Pass 7 found a
High and the repair changes synchronous lifecycle behavior.

### Pass 8 — FAIL, two High and one Medium finding

Ask Codex session `019f65ab-79b5-76d2-96b6-7fb03ed8f831` independently
confirmed the Backend control, migration, release-pairing and submission-gate
closures, then reproduced three cross-generation gaps:

1. **High:** native `EventTarget.dispatchEvent()` could not recheck owner
   identity between real DOM handlers. A handler that installed a replacement
   still allowed the predecessor event to reach later handlers; the reverse
   listener order could also arm delayed stale UI reconciliation first;
2. **High:** cancellation was still session-addressed across the App proxy and
   Backend registry. A delayed predecessor interrupt or either predecessor
   finalizer could signal, cancel, remove or terminalize the replacement; and
3. **Medium:** idle-timeout SDK-session cleanup checked owner state only before
   starting its PATCH, so a delayed request could clear a replacement's resume
   identity.

Post-Pass-8 repair status:

- manager-owned stream events and resource signals no longer use opaque DOM
  broadcast. A controlled global/signal bus rechecks exact stream ownership
  before and after every listener. `ChatView` carries the event's
  `source_request_id` into both delayed reconciliation timers and refuses stale
  callbacks;
- the App's `ActiveStream`, stop registry, interrupt helper/route, new-chat
  submission owner and conditional SDK-session PATCH all carry the same exact
  source generation. Stale or unscoped interrupts cannot mark a modern
  replacement; identity fields are written after arbitrary reason metadata;
- the Backend replaced parallel task/envelope maps with one indivisible
  `_ActiveDesktopExecution(task, cancel_event, source_request_id)`. Replacement
  signals before task cancellation, while abort, disconnect and both finalizers
  operate on a captured owner and use identity compare-and-remove;
- remote App Job creation and Backend SSE connection establishment are explicit
  await fences: a predecessor that resumes after a replacement is retired
  before it can become the last Backend writer. A delayed predecessor SDK PATCH
  is rejected with 409, and its mutation follows the check without another
  event-loop yield;
- Backend SSE completion is signalled idempotently from both `_run_loop` and a
  task-done callback, so abort after the first frame but before the task's first
  coroutine turn still produces `done` rather than permanent keep-alives; and
- Desktop health now reports an independent `interrupt_schema_version=1`.
  App health parsing and bundled-runtime smoke require request V1, SSE V1,
  interrupt V1 and both capability flags exactly false. An old session-only
  Backend can no longer masquerade as compatible merely because it accepts the
  request fields. This remains transport compatibility, not production
  control-plane authority.

Pre-Pass-9 verification:

- Backend combined control/Desktop-server selection: 209 passed, one optional
  dependency skip; its narrower generation/wire/auth/docs selection is 121/121
  and control remains 76 (19 ingress, 27 Repository, 30 execution);
- App complete unit suite: 1,461/1,461; TypeScript passes and targeted ESLint
  reports zero errors (existing warnings remain outside this slice);
- semantic bundled-release pairing: 4/4;
- Python Ruff check, compileall and both repositories' candidate diff checks
  pass; and
- the production capability flags remain false and production code still does
  not import the isolated `omicsclaw.control` Module.

A ninth fresh, read-only Ask Codex pass is required because Pass 8 found High
issues and the repair materially changes both repositories' runtime seams.

### Pass 9 — INCOMPLETE, transport failure and no verdict

Ask Codex session `019f660f-90b2-7802-a506-672e66736d22` inspected the paired
Backend/App generation fences, release workflow and current tests, but its
WebSocket and HTTPS response streams failed after repeated TLS reconnects. It
exited without writing the requested report. This pass therefore establishes
neither PASS nor FAIL and is not used as closure evidence.

Before the transport failure, the reviewer independently ran the focused
Backend abort/owner/wire selection (18/18) and the semantic bundled-release
pairing suite (4/4). Its wider Python invocation was initially blocked by the
read-only sandbox's pytest cache/temp setup, and the shell-visible App Node was
version 10 rather than the repository's required Node 20; those environment
failures are not recorded as candidate failures. The primary validation used
the configured Node 20 and writable test environment instead.

The post-interruption local audit found one additional generation boundary:
remote `/jobs` creation was fenced after a successful await, but its `catch`
path could still resume after a replacement and persist a stale
Backend-unreachable assistant message. The catch path now performs the same
exact `source_request_id` owner check and retires the predecessor with 204.
The new regression holds predecessor Job creation open, establishes the
replacement Backend stream, rejects the predecessor promise and proves the
failure writes no assistant error into the replacement Transcript.

Post-repair verification is App 1,462/1,462 with clean TypeScript, targeted
ESLint at zero errors (four pre-existing `ChatView` warnings), semantic release
pairing 4/4 and candidate diff checks. The paired Backend selection remains
209 passed with one optional dependency skip; Ruff, compileall and candidate
diff checks pass. A fresh tenth Ask Codex session is required for the actual
post-repair closure verdict.

### Pass 10 — FAIL, one High finding

Ask Codex session `019f662a-7689-7c33-a84a-b1df78d5b881` completed a fresh
source review and confirmed the Backend captured-owner, abort, first-frame,
wire/capability and production-isolation boundaries. It found one remaining
High in the App proxy's remote continuation ordering:

1. **High:** after awaiting `/jobs`, a predecessor inserted its assistant
   placeholder before the post-await generation fence. If a replacement had
   registered during that await, predecessor cleanup converted the new,
   out-of-order row to `generation stopped`. Failed Backend connection and
   delayed non-2xx response-body paths likewise persisted terminal errors
   without rechecking ownership after their respective awaits.

Post-Pass-10 repair status:

- successful remote Job creation now checks exact generation before inserting
  the placeholder. The later common fence remains as a defense before Backend
  execution starts;
- both the Backend connection catch and the non-2xx body-read continuation
  recheck exact generation before any Transcript or Job terminal mutation;
- the established Backend reader now rechecks after every `reader.read()`
  resolution and at the reader rejection boundary. A stale frame is not
  decoded, so it cannot mutate Transcript accumulation, Run links or local Job
  output before retirement;
- superseded cleanup is non-blocking with respect to a hostile stream
  `cancel()` promise and operates only on captured resources. It may replace a
  placeholder that was created while that generation still owned the session,
  but it cannot add a placeholder after replacement or address the successor's
  Job/registry entry; and
- deterministic tests hold each boundary across replacement: late Job success
  creates no predecessor assistant row; late connection rejection and delayed
  non-2xx body completion write no stale error; an established predecessor
  reader rejection cannot terminalize the replacement. The focused route file
  passes 12/12 and the complete App suite passes 1,465/1,465. TypeScript,
  targeted ESLint and candidate diff checks pass.

A fresh eleventh Ask Codex pass is required because Pass 10 found a High and
the repair changes every remote proxy post-await terminal seam.

### Pass 11 — PASS at the Blocker/High gate, with one Medium and two Low findings

Ask Codex session `019f663e-832e-7e41-b091-18a16a9ffdfe` completed a fresh
paired source audit and targeted validation. It found no Blocker or High and
independently confirmed the Pass-10 remote continuation repair at Job success
and failure, Backend connection, non-2xx body, established reader and captured
owner cleanup boundaries. It reported three residual findings:

1. **Medium:** detached auto-title generation checked `title_source` only
   before awaiting `/chat/title`, then wrote unconditionally. A delayed result
   could overwrite a manual rename or a newer turn's title;
2. **Low:** delayed transcript reconciliation checked the source generation
   before starting its messages fetch, but not after the response arrived, so
   an old response could temporarily remove a replacement's optimistic row;
   and
3. **Low:** the current replacement/abort fence treats `source_request_id` as
   the stream incarnation. Reusing the same value makes two generations
   indistinguishable. Current clients generate a fresh random 128-bit value for
   every submission and do not retry, so reuse is outside the V1 capability
   contract.

Post-Pass-11 repair status:

- detached title refresh captures both the current auto-owned title and a
  bounded Transcript fingerprint before the LLM await, re-reads the same
  window after it, and commits through a single SQLite compare-and-set that
  requires the exact old title plus auto ownership. Manual rename, newer Turn
  or another completed title writer therefore wins;
- message reconciliation is isolated behind a small client helper that checks
  the exact generation after response arrival and again after body decoding.
  `ChatView` changes state only for accepted data and clears the live snapshot
  only after that generation's persisted window was applied;
- deterministic regressions hold the title request across a manual rename and
  a newer Transcript row, and hold a messages response across generation
  replacement. The focused selection passes 16/16; the complete App unit suite
  passes 1,469/1,469 with clean TypeScript and targeted ESLint at zero errors
  (four pre-existing `ChatView` warnings);
- the paired Backend selection passes 218 with one optional dependency skip;
  semantic bundled-release pairing remains 4/4, Ruff/compileall and both
  repositories' candidate diff checks pass; and
- V1 documentation now states that `source_request_id` is fresh per submission
  and cannot be reused for retry while durable ingress idempotency is false.
  Retry-capable stable binding remains a future coordinated capability cutover,
  not a silent semantic change to this preparatory wire.

A fresh twelfth Ask Codex pass is required because the Medium repair changes a
detached persistence path and the Low repair changes UI reconciliation timing.

### Pass 12 — PASS at the Blocker/High gate, with three Medium and two Low findings

Ask Codex session `019f665c-e5b8-7e01-b5a7-a13e59f23035` again found no
Blocker or High and confirmed the remote proxy, Backend exact-owner,
non-authoritative capability and release-pairing closures. It found that the
Pass-11 repairs still left three reachable concurrency gaps:

1. **Medium:** the detached title path re-read the Transcript and then executed
   a title-only CAS in separate SQLite statements. A writer in another App
   process could insert a message between them;
2. **Medium:** the provisional first-turn title still used an unconditional
   auto-title update after reading a possibly stale `New Chat` session, so a
   concurrent manual rename could be reclaimed as auto-owned;
3. **Medium:** rewind carried neither a Transcript revision nor a live stream
   owner. A confirmed request delayed across a new submission could delete the
   new user row and reset SDK identity while its Backend execution continued;
4. **Low:** reconciliation checked generation after its awaits but not again at
   the React consumer seam, while rewind invoked it with no generation; and
5. **Low:** unsupported/corrupt `title_source=NULL` was treated as auto-owned.

Post-Pass-12 repair status:

- delayed title commit now executes title ownership check, bounded Transcript
  re-read/fingerprint comparison and update inside one better-sqlite3
  `transaction(...).immediate()`. The write reservation is acquired before the
  re-read, eliminating the cross-process message-insert/rename window;
- provisional title adoption is an exact `New Chat + title_source=auto`
  compare-and-set. All title refresh eligibility and CAS paths fail closed on
  missing, NULL or unknown ownership rather than interpreting it as auto;
- rewind preview returns the exact current tail message identity. The actual
  mutation requires that identity, rejects a registered live/terminalizing App
  proxy owner, and rechecks target plus tail before delete/SDK reset inside one
  immediate SQLite transaction. A submission persisted after preview produces
  409 with no deletion or SDK mutation; a cross-process writer is serialized by
  the same database reservation;
- every reconciliation captures both a monotonically increasing component
  request epoch and the current client stream incarnation, including the NULL
  no-stream state. The helper checks both network awaits and `ChatView` checks
  again immediately before React state writes; a newer refresh, new stream,
  keyed unmount or unowned rewind response invalidates the old result;
- new regressions cover delayed JSON decoding, no-stream-to-new-stream and old
  epoch rejection, provisional-title/manual ownership, NULL fail-closed title
  eligibility, rewind happy path, changed Transcript rejection and active proxy
  rejection. The focused selection passes 30/30 and the complete App unit suite
  passes 1,475/1,475; TypeScript is clean and targeted ESLint has zero errors
  (existing `ChatView`/virtualizer warnings remain); and
- the paired Backend selection remains 218 passed with one optional dependency
  skip, release pairing 4/4, Ruff and both candidate diff checks pass.

A fresh thirteenth Ask Codex pass is required because these repairs change
cross-process SQLite ordering and the destructive rewind contract.

### Pass 13 — FAIL, two High, one Medium and two Low findings

Ask Codex session `019f6677-74cd-7280-a9d3-daaa5e74da07` independently
confirmed the atomic title, provisional-title CAS, remote proxy/Backend owner,
wire capability and release-pairing repairs, but did not clear the gate. It
found two reachable High paths in the App-owned Transcript:

1. rewind consulted only the process-local stop registry, so another Next.js
   process sharing the SQLite file could delete the user row of a live Backend
   execution even when the preview tail still matched; and
2. session deletion cascaded through all message rows without checking or
   interrupting any live execution.

The same review reported one Medium where React could reduce already-queued
reconciliation setters after a replacement installed its optimistic row, plus
two Lows: rewind UI treated some HTTP failures as success, and the preview tail
id was forgeable rather than an opaque target-bound preview authorization.

Post-Pass-13 repair status:

- App SQLite now contains an explicit, expiring `chat_execution_leases` owner
  record keyed by Session and exact `source_request_id`, with a per-process
  owner id and heartbeat. `/api/chat` acquires it under `BEGIN IMMEDIATE`
  before inserting the user Turn. A live foreign-process owner cannot be
  replaced; same-process generation replacement remains exact-source fenced;
- rewind, message clearing and session deletion all check that shared lease in
  the same immediate transaction as their destructive mutation. Thus either a
  mutation commits before a later submission begins, or the submission lease
  commits first and the mutation returns 409. Local live/terminalizing owners
  remain an additional fast fail-closed fence;
- rewind preview now persists a short-lived, random one-shot intent bound to
  `(session, target user message, exact tail)`. The mutation consumes that
  intent in its guarded transaction; a known tail id alone is insufficient;
- reconciliation rechecks epoch plus stream incarnation inside every React
  functional state reducer, not only before enqueueing it. Rewind UI accepts
  success only for `res.ok && canRewind === true`, and deletion is disabled in
  the local streaming UI while the server remains authoritative; and
- these App-side leases are only a shared Transcript-mutation safety fence.
  They do not make App SQLite the Backend control plane, enable ingress replay,
  or change `authoritative_ingress=false` /
  `durable_ingress_idempotency=false`.

Pre-Pass-14 verification is App 1,481/1,481 with the new execution-lease,
cross-process submit, rewind, delete and clear regressions included; TypeScript
is clean and targeted ESLint has zero errors (the same five pre-existing
ChatView/virtualizer warnings remain). The paired Backend/control selection is
216 passed with one optional dependency skip, semantic bundled release pairing
is 4/4, Ruff and both repositories' candidate diff checks pass. A fresh
fourteenth Ask Codex pass is required because the repair introduces a shared
cross-process mutation owner.

### Pass 14 — FAIL, one High, two Medium and one Low finding

Ask Codex session `019f6695-211f-7301-a1d5-96d019c7e949` confirmed the shared
lease acquisition, exact release, destructive-operation fence, Backend owner,
wire capability and release-pairing boundaries. It nevertheless found one
production-reachable High: the one-shot rewind intent bound only the last row
id. A remote assistant placeholder or `PUT /api/chat/messages` could update
content in place while preserving every id and rowid, after which the old
preview token still authorized deletion.

The review also found two Mediums and one Low:

1. exceptions after lease acquisition but before `ReadableStream.start`,
   including local Job writes and error Transcript persistence, could leave the
   heartbeat running indefinitely in a healthy Next.js process;
2. `clear_messages` committed before later PATCH validation and could therefore
   return an error after deleting the Transcript, or restore a stale
   `sdk_session_id` in the same request; and
3. a delayed older-page response could retain the oldest 300 rows and
   temporarily trim a replacement optimistic Turn.

Post-Pass-14 repair status:

- rewind preview now re-reads the target, complete ordered suffix and SDK resume
  identity under one immediate transaction. It stores a canonical SHA-256
  fingerprint over row order, ids, roles, content, timestamps, token usage and
  heartbeat metadata. The destructive transaction consumes the intent once,
  recomputes the same fingerprint, and rejects any in-place Transcript or SDK
  identity change before delete/reset. Legacy intents migrate with an empty
  fingerprint and cannot match;
- the App proxy now has one idempotent `finishProxyRun()` and an outer
  ownership-aware `try/finally` covering every post-acquisition path. Ownership
  transfers to `ReadableStream.start` synchronously; before that point the route
  scope always stops the registry owner and lease heartbeat, while afterwards
  the stream pump persists terminal state and releases in its existing finally;
- session PATCH validates the entire body before writing. `clear_messages` is
  explicitly exclusive, remains lease-guarded, resets SDK identity, and returns
  immediately, so no later field can fail or restore stale resume state;
- older-page application now carries a pagination epoch, exact stream
  incarnation and captured tail id. The functional reducer rechecks all three
  against live state before prepending or trimming; stale responses cannot
  change messages or `hasMore`; and
- deterministic regressions cover same-id suffix content replacement, SDK
  identity replacement, local Job insertion failure, terminal assistant write
  failure, mixed clear rejection and optimistic-tail replacement.

Pre-Pass-15 verification is App 1,487/1,487, TypeScript clean and targeted
ESLint at zero errors (only existing `ChatView` warnings). The paired Backend
selection is 218 passed with one optional dependency skip; semantic bundled
release pairing is 4/4 (24 subtests), Ruff and both repositories' candidate
diff checks pass. A fresh fifteenth Ask Codex pass is required because Pass 14
found a High and the repair changes destructive authorization plus proxy lease
ownership transfer.

### Pass 15 — PASS at the Blocker/High gate, with one Medium finding

Ask Codex session `019f66b4-f86e-7a01-9b58-eb20e261d411` completed a fresh
read-only paired audit and found no Blocker or High. It confirmed the rewind
state fingerprint, one-shot destructive transaction, pre-stream/stream-pump
lease ownership transfer, exact Backend owner, React pagination fence,
non-authoritative wire flags and immutable release pairing.

The sole Medium was a concrete request-contract gap: `clear_messages` was
tested by truthiness. A caller could send the string `"false"`; exclusivity
would pass, the destructive branch would run, and the route would return 200
after deleting the Transcript and clearing SDK identity.

Post-Pass-15 repair status:

- `clear_messages` is now accepted only when it is a real boolean and the
  destructive branch requires exact `=== true`;
- every recognized string-valued session PATCH field is type-checked before
  any write, preventing a valid early field from partially committing before a
  later malformed field fails; and
- regressions prove string `"false"` leaves both Transcript and SDK identity
  unchanged, and an invalid later field cannot partially commit an earlier
  title update.

Pre-Pass-16 verification is 14/14 for the focused session/permission/generation
selection and 1,489/1,489 for the complete App unit suite. TypeScript and
targeted ESLint are clean, candidate diff checks pass, and the unchanged paired
Backend/release evidence remains 218 passed with one optional skip plus 4/4
release-pairing tests (24 subtests). A fresh sixteenth review is required so
the final code snapshot, including the Medium repair, is independently
cross-validated.

### Pass 16 — PASS, zero findings at every severity

Ask Codex session `019f66c0-c207-74b3-86da-fbde76524f02` completed a fresh,
read-only paired audit of the exact post-Pass-15 snapshot and reported zero
Blocker, High, Medium or Low findings. It did not treat the prior verification
counts or README claims as proof; it re-read the relevant App and Backend
source, regressions and production callers.

The review independently confirmed that:

- every present non-boolean `clear_messages` value is rejected before any
  write, exact `true` is the only destructive value, `false` is a
  non-destructive no-op, and clear remains exclusive, shared-lease guarded,
  SDK-resetting and immediately returning;
- every recognized later-processed session PATCH field is validated before the
  first write, while the current production callers all send compatible
  shapes;
- rewind still binds its one-shot authorization to the target, complete ordered
  suffix and SDK resume identity; route-to-stream cleanup still has exactly one
  owner; rewind, clear and delete remain protected by local generation and
  shared SQLite lease fences; and delayed pagination remains
  epoch/source/tail guarded inside the live reducer;
- request, SSE and interrupt v1 remain explicitly non-authoritative, with
  `authoritative_ingress=false` and
  `durable_ingress_idempotency=false`; production code still does not import
  the isolated `omicsclaw.control` package; and immutable 40-character Backend
  SHA pairing remains enforced for release builds.

The final Stage 2b repair snapshot therefore closes the independent review gate.
Its architectural boundary is unchanged: the App lease protects App
Transcript/Session mutations across App processes, but it is not Backend
control-plane authority, durable ingress idempotency, replay protection or a
production cutover.
