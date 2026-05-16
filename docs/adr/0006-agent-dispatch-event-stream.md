# In-process `dispatch(envelope) -> AsyncIterator[Event]` to unify Surface → agent-loop calling convention

## Status

Accepted (2026-05-16).

## Context

After ADR 0005 folded the three Surfaces under `omicsclaw/surfaces/`,
each Surface still calls `core.llm_tool_loop` directly with its own
hand-wired bag of seven positional callbacks:

    progress_fn, progress_update_fn,
    on_tool_call, on_tool_result,
    on_stream_content, on_stream_reasoning,
    on_context_compacted

`runtime/agent/loop.py:519-545` confirms all seven. Three Surfaces × seven
callbacks = 21 hand-wired adapter functions, plus three implementations of
"collect the final string and send it".

A code audit during this grilling session surfaced three problems with that
shape:

1. **`pending_text` is a module-level list that two concurrent chats race on.**
   `runtime/agent/state.py:331` declares `pending_text: list[str] = []`
   (un-keyed, single global). `surfaces/channels/telegram.py:363-366` reads,
   joins, and `.clear()`s it after every turn. Two chats finishing inside the
   same event loop window will corrupt each other's reply. ADR 0003 §
   Consequences flagged this class of bug under the line "per-chat state
   already lives in module-level dicts that the actor refactor — Phase 2 —
   will fix with a different abstraction"; nothing has fixed it yet.

2. **`runtime/agent/loop.py` reverse-imports `surfaces/channels/commands`**
   at lines 62-63 (the slash-command dispatcher). This is exactly the
   reverse direction that ADR 0004 set out to forbid; it survives because
   loop.py predates the boundary work. Adding a Surface→agent indirection
   does not by itself fix this, but it gives the slash dispatcher a natural
   home (an event handled before loop entry).

3. **CellClaw-shaped temptation.** The user proposed mirroring CellClaw's
   `Gateway → Redis queue → Worker → EventBus` four-layer ingress pipeline.
   That pipeline buys: cross-process scaling, crash replay via persisted
   queue, multi-tenant fair scheduling, and decoupled inbound/compute event
   loops. In a single-machine single-user research tool with the explicit
   "Genetic data never leaves this machine" constraint (CLAUDE.md §Safety
   Rules), none of those four payoffs apply. ADR 0003 rejected the same
   shape four days ago as "speculative infrastructure that no caller
   required"; ADR 0005 Q1-B deferred the same shape three days ago as
   "Left as a possible future ADR if a real cross-Surface concern emerges".
   The cross-Surface concern that did emerge is problem (1), not the
   scaling concerns that motivate CellClaw.

CellClaw's own code, read at HEAD: `cli/chat.py:211` calls
`self.worker.run_agent_task(...)` directly, bypassing the queue. The
"unified pipeline" is in fact two-thirds unified (Web + Channel) and one
third in-process direct-call. Even in the reference implementation, CLI
opts out of the queue.

## Decision

Introduce an in-process dispatcher: a pure async-generator function
`dispatch(envelope: MessageEnvelope) -> AsyncIterator[Event]` in
`omicsclaw/runtime/agent/dispatcher.py`. It wraps `llm_tool_loop`,
translates the seven callbacks + return value + exception into a typed
event stream, and owns the per-request state currently scattered across
module-level globals.

The seven design decisions resolved in the grilling session, in dependency
order:

**Q1 — Layer: L1 in-process only.** No Redis, no separate worker process,
no persistent queue, no EventBus across process boundaries. Reasons: the
four payoffs of CellClaw's L2 layer (cross-process scale, crash replay,
multi-tenant scheduling, inbound/compute decoupling) all assume an
environment OmicsClaw explicitly is not. ADR 0003's "no caller required
it" test still applies. The cross-Surface concern that motivates this ADR
is the `pending_text` race in problem (1), which an in-process per-request
context resolves without any of L2's machinery.

**Q2 — Scope: event stream plus per-request state ownership.** The
dispatcher does not merely re-skin the existing seven callbacks; it
takes over the lifetime of the module-level `pending_text` / `pending_media`
buffers. Pure cosmetic re-skinning (event stream only, leave globals)
fails ADR 0003's "no caller required it" test on its own. Going further
to also retire the slash-command reverse-import is left to a follow-up
ADR — that is a boundary cleanup, not part of the dispatch contract.

**Q3 — Lifecycle: per-request, mirroring `MemoryClient`.** CONTEXT.md §
Surfaces already documents the per-request pattern: *"A Surface holds one
`MemoryClient` per request context."* The dispatcher takes the same shape:
no class held across turns, no shared mutable state between turns,
construct-run-discard per inbound message. Session-spanning state
(`pending_preflight_requests`, `received_files`) stays in the existing
session store, which is already chat-keyed and not part of this ADR.

**Q4 — Relation to `llm_tool_loop`: wrap, do not replace.** A grep over
the tree shows 70 test references and 34 package call sites for
`llm_tool_loop`. Replacing the function (option B in the session) would
mean a ~100-site coordinated edit, which violates ADR 0005's "smallest
clear change" rule. Wrapping (option A) requires three Surface-side call
sites changing plus one new module; `llm_tool_loop` itself is untouched.

**Q5 — API shape: pure async generator, `Final` as a terminal event.**
The dispatcher returns `AsyncIterator[Event]` with the final reply
emitted as `Final(text, kind)` and errors emitted as `Error(exception)`;
nothing is raised mid-iteration, nothing returns out-of-band. Two
alternatives (tuple of `events, Future[reply]`; async context manager
with `run.events` + `run.final_reply`) were considered and rejected as
non-uniform across Surfaces — Channel would await the future, Desktop
and CLI would iterate the stream, two code shapes for one operation.

**Q6 — Event taxonomy: exactly ten events, one-to-one with the existing
seven callbacks + return + exception + `pending_media`.** The union:

    ProgressStart(progress_id, text)
    ProgressUpdate(progress_id, text)
    ToolCall(tool, arguments)
    ToolResult(tool, result)
    StreamContent(chunk)
    StreamReasoning(chunk)
    ContextCompacted(payload)
    PendingMedia(items)
    Final(text, kind: "normal" | "preflight")
    Error(exception)

No new capability is added at this layer; the contract is a one-to-one
recast of what `llm_tool_loop` already produces. Adding richer or
fewer events is a follow-up. `Final.kind` is the only discriminator a
Surface uses to branch behaviour (Desktop renders preflight ask as a
button UI; Channel renders it as plain text). `pending_text` does not
appear in the event union — its content is folded into `Final.text` by
the dispatcher, which is how problem (1) is closed.

**Q7 — Module shape: a free function, not a class.** The per-request
state is constructed inside `dispatch(envelope)` and discarded when the
generator completes; no caller is going to hold a `Dispatcher` object
across calls. `dispatch.py`, `envelope.py`, `events.py` — three small
files in `runtime/agent/`. The `MemoryClient` mirror (option B in the
session) is rejected here because `MemoryClient` exposes seven verbs
called repeatedly inside one request; `dispatch` exposes one verb called
once per request — a class would be ceremony without payoff.

**Q8 — Migration: five-step phased, CLI first.** The work is additive
(new code, old code path keeps working); per-Surface migration is
independently shippable. This is ADR 0004's phasing pattern, not ADR
0005's atomic-relocation pattern. The order:

    L0   land  dispatcher.py / envelope.py / events.py + unit tests
         (no Surface touched)
    L1   migrate CLI Surface  (no SSE, no platform SDK, easiest to verify)
    L2   migrate Desktop Surface  (replaces the asyncio.Queue + SSE bridge)
    L3   migrate Channel Surface  (10 adapters, but only _handle_message /
         _on_message change per adapter)
    L4   delete `state.pending_text`  (safe only after L1-L3 land)

Each step is a separate commit, CI green at each step. The L0-L4 ordering
also means: if L4 turns out to need a compat shim for an external script
the audit missed, it can stop at L3 without unwinding the dispatcher.

## Considered Options

- **Option L2 — full CellClaw mirror.** Redis queue + separate worker
  process + EventBus. Rejected as documented in §Context: the four
  payoffs don't apply to a single-machine single-user research tool;
  ADR 0003 already rejected the same shape; CellClaw's own CLI opts out.

- **Option Scope-A — event stream only, leave globals.** Cheaper to
  ship but doesn't fix problem (1); reduces to "different calling
  convention for the same thing," exactly what ADR 0003 §Consequences
  warned against re-introducing without a real caller need.

- **Option Scope-C — also retire the `loop.py` → `surfaces/channels/commands`
  reverse-import.** Bundles a boundary cleanup with the dispatch
  contract. Rejected as scope creep; the slash-command reverse-import
  has its own design tree (move dispatcher up to `runtime/` or fold
  into a new neutral location) and deserves a separate ADR.

- **Option API-Y — `(events, Future[reply])` tuple.** Lets Channel
  skip stream iteration. Rejected because the three Surfaces would
  then have two code shapes for the same operation, and the Channel
  Surface still has to wait for progress events (typing indicator
  updates) so it would iterate anyway.

- **Option API-Z — async context manager.** `async with dispatch(env)
  as run:` + `run.events` + `run.final_reply`. Cleaner cancellation
  semantics. Rejected: nested `async with` + `async for` is heavier
  syntax than callers need given that no Surface today has a real
  cancellation story to plug into the context exit.

- **Option Wrap-B — replace `llm_tool_loop`.** Move its body into the
  dispatcher, drop the seven callback parameters. Architecturally
  cleaner. Rejected on count: 70 test references + 34 package call
  sites = ~100 coordinated edits, which is the kind of cross-cutting
  rewrite ADR 0005 set out to forbid in favour of "smallest clear
  change."

- **Option Migrate-B — atomic single commit.** Land scaffolding +
  three Surfaces + global deletion in one commit. Rejected because
  this work is additive (unlike ADR 0005's physical relocation, where
  atomic was cheaper than partial states); partial states here are
  valid and shippable, so phasing pays for itself in bisectability
  and reviewer load.

- **Option Migrate-C — only migrate the Channel Surface.** Keep the
  forcing function (Channel handler concurrency / state-leak risk)
  but leave Desktop and CLI on the old path. Rejected because a
  dispatcher that serves one Surface is no longer "unifying" anything;
  scope B's per-request state ownership stops paying for itself if
  only one caller exercises it.

## Consequences

**Wins**

- `pending_text` race condition (state.py:331) closes at L4. Two
  concurrent chats stop being able to corrupt each other's reply.
- Three Surfaces stop hand-wiring seven callbacks each; one `async for`
  per Surface replaces 21 callback adapter functions.
- `Final.kind` discriminator gives Desktop a typed signal for preflight
  rendering, replacing the current `pending_preflight_requests` dict
  consultation pattern.
- The dispatcher becomes the natural home for any future cross-Surface
  concern (auth, audit, rate-limit) that materialises after this ADR.
  ADR 0003's "no generic middleware framework on speculation" stance
  stays intact — middleware is not built here, only the entry point
  where it could one day live.

**Costs**

- One new module of ~3 files (dispatcher.py, envelope.py, events.py)
  in `runtime/agent/`. ~200-300 LOC estimated.
- Three Surface-side call sites change. Test files mocking the three
  call sites need parallel updates.
- L4 deletion of `state.pending_text` removes a global symbol that any
  external tool (skill scripts, MCP servers) might import. A pre-L4
  grep over the tree is required to confirm zero external readers.
- Slash-command reverse-import (`runtime/agent/loop.py:62-63`) is *not*
  fixed by this ADR. It survives intentionally and gets its own
  follow-up.

**Alternatives are catalogued under §Considered Options above.**

## Verification

Per-phase verification, mirroring ADR 0004's per-phase test gates:

- **L0:** unit test for `dispatch(envelope)` against an in-memory
  `llm_tool_loop` double. Covers each of the 10 events; covers `Error`
  termination; covers `pending_text` content folded into `Final.text`.
- **L1:** `oc interactive` smoke. Send a message that exercises a
  multi-tool agent loop; confirm same final output as pre-L1 baseline.
- **L2:** `oc desktop-server` SSE smoke. POST `/chat/stream`; confirm
  the SSE frame sequence matches what the legacy `asyncio.Queue` bridge
  emitted, minus the preflight ad-hoc branch which now rides on
  `Final.kind`.
- **L3:** Channel manual smoke against a sandbox Telegram chat
  (`make bot-telegram`), and against a sandbox Feishu workspace
  (`make bot-feishu`). Send three concurrent messages from three
  chats; verify no reply cross-talk (the regression test for the
  `pending_text` race).
- **L4:** `grep -rn "pending_text" omicsclaw/` returns zero hits;
  `grep -rn "pending_text" tests/` returns zero hits; full test
  suite green on the merge commit.

A `/graphify` re-index lands after L0 so that knowledge-graph queries
return the new `runtime/agent/dispatcher.py` anchor, same caveat as
ADRs 0004 and 0005.

## Open questions

Tracked but not resolved here:

- **Slash-command reverse-import.** `runtime/agent/loop.py:62-63`
  still imports `surfaces/channels/commands`. The dispatcher could
  intercept slash commands before delegating to `llm_tool_loop`, but
  that conflates two changes. Follow-up ADR to decide whether to
  promote the slash dispatcher into `runtime/agent/` or relocate it
  to a neutral `runtime/commands/` module.

- **Cancellation contract.** Desktop has `pending_preflight_requests`
  and a chat-stream cancel endpoint; CLI has Ctrl-C; Channel has
  nothing. A unified cancel token would naturally live on the
  `MessageEnvelope`, but no Surface today consumes it consistently
  enough to motivate the contract. Revisit when a real cross-Surface
  cancel concern emerges.

- **`pending_media` event vs return field.** This ADR keeps
  `PendingMedia` as a discrete event for symmetry with the 10-event
  taxonomy, but only the Channel Surface consumes it; Desktop and
  CLI ignore it. If a future Surface lands that needs media, the
  contract is ready; if no such Surface lands within a quarter,
  revisit whether `PendingMedia` should be folded back into
  `Final.attachments` to shrink the taxonomy.
