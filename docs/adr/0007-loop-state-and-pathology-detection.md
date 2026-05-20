# Explicit `LoopState` and soft pathology signals inside `run_query_engine`

## Status

Accepted (2026-05-16).

## Context

A grilling session compared OmicsClaw's `run_query_engine`
(`runtime/agent/query_engine.py:480`, 460+ line body inside a 945-line module)
with CellClaw's `DecisionExecutionEngine`
(`/home/weige/project/repo_learn/cellclaw_source/agent/execution_engine.py:49`,
inside a 1348-line module). The goal was to identify which of CellClaw's
machinery actually pays off in OmicsClaw's single-user single-machine
shape, given the constraints already pinned by ADRs 0003 / 0005 / 0006.

The audit produced a sharper map of what already exists versus what is
genuinely missing.

**Already present in OmicsClaw — re-introducing it would be duplication:**

1. **Variation points.** `query_engine.py:106-119` defines
   `QueryEngineCallbacks` — an eight-field callback dataclass
   (`on_stream_content`, `before_tool`, `after_tool`,
   `on_context_compacted`, …). This is functionally the equivalent of
   CellClaw's Template Method hooks
   (`_before_iteration` / `_after_decision` / `_handle_terminal_decision`),
   only delivered as composition rather than inheritance. Introducing a
   subclass hierarchy on top would be a parallel mechanism for the same
   concern.

2. **Clarification protocol.** `runtime/tools/builders/agent_executors.py:360-366`
   already implements "agent reaches back to the user mid-loop" via the
   *clarification-as-tool-result* pattern: when a skill needs disambiguation
   (`_maybe_require_batch_integration_workflow`,
   `_maybe_require_batch_key_selection`), it returns a clarification
   string as the tool's result; the LLM reads it on the next turn and
   produces a natural-language ask. This is the OmicsClaw analogue of
   CellClaw's `DecisionType.ASK_USER`. No Surface today renders
   clarifications differently from final replies, so the explicit
   decision-type machinery would add ceremony without UI payoff.

3. **Single execution mode.** A grep over the tree confirms
   `run_query_engine` has exactly one caller (`engine/loop.py:190`),
   `run_engine_loop` has exactly one caller (`runtime/agent/loop.py:614`).
   CellClaw's `DecisionExecutionEngine` / `DecisionExecutionRuntime`
   split exists because they run the same loop in two modes — direct
   in-process execution and worker-queue execution. OmicsClaw runs only
   the direct mode (ADR 0006 closed the door on queue-backed execution).
   The subclass split serves a need OmicsClaw does not have.

**Genuinely missing — the one gap this ADR closes:**

CellClaw passes a `LoopState` instance through every iteration of
`execute_decision_loop` and reads `state.tools_executed`,
`state.recent_errors` from `before_iteration` / `after_decision` hooks.
OmicsClaw's equivalent counters live as **local variables inside the
460+ line body of `run_query_engine`**, unreachable from any callback,
unobservable to any future detector, and unsharable with telemetry.

The practical consequence: OmicsClaw cannot today detect three classes
of unhealthy loop without invasive surgery into the function body:

- **Ping-pong**: the LLM oscillates between two tools (A-B-A-B) or
  re-issues the same tool with the same arguments (A-A-A-A) without
  the result actually advancing the answer. CellClaw counts these via
  `tool_loop_pingpong_threshold = 6` against `tool_call_history`.

- **Repeated failure**: the LLM keeps invoking a tool that keeps
  failing the same way (`tool_failure_loop_threshold = 8` against
  `recent_errors`). The current `MAX_TOOL_ITERATIONS = 20` cap
  (`query_engine.py:551`) catches this only after burning 20× the
  budget — fourteen iterations of wasted spend before it stops.

- **Cross-cutting future detectors** (wall-clock timeout, per-tool
  token budget, retry-storm): every future detector will want the same
  data. Adding them one-at-a-time as local-variable patches grows the
  function past readability.

Cellclaw's `tool_call_history: list[str]` records only tool **names** —
a stricter design would track names with argument digests so
`grep(pattern="A")` followed by `grep(pattern="B")` is not falsely
classified as ping-pong. This ADR adopts the stricter variant.

The session also flagged what to **not** import from CellClaw:

- `DecisionExecutionEngine` base + `DecisionExecutionRuntime` subclass —
  Template Method serves their dual execution mode; OmicsClaw has only
  the direct mode.
- Explicit `DecisionType` enum (`RESPOND` / `ASK_USER` / `WAIT` /
  `TOOL_CALL`) — clarification-as-tool-result already covers
  `ASK_USER`; no Surface differentiates the other states visually;
  three Surfaces × four decision-type renderers = ceremony without
  payoff. ADR 0006's "no Surface today consumes it consistently
  enough" test fails for `DecisionType`.
- ACP / JSON-RPC desktop transport — ADR 0006 §Costs already chose
  typed Python events over a wire protocol; ACP is the wire-protocol
  alternative not picked.
- Redis queue + stateless worker — ADR 0006 §Q1 rejected the entire
  multi-process layer.

## Decision

Introduce **`LoopState`** as a typed carrier of loop progress, and a
pure-function **`loop_pathology.detect(state)`** that returns
`PathologySignal | None`. Wire detection into `run_query_engine` at
three points and surface signals as a new `PathologyDetected` event
through `dispatch(envelope)`. The reaction model is *soft correction*:
inject the signal as a synthesized tool result into the next LLM call;
let the LLM decide whether to recover or finalize; the existing
`MAX_TOOL_ITERATIONS = 20` cap remains the terminal backstop.

The five design decisions resolved in the session, in dependency order:

**Q1 — Service shape: single-user local tool.** No multi-tenant
SaaS, no IDE-protocol integration. This kills any motivation for
the heavier CellClaw machinery (ACP, queue, worker, DecisionType for
UI differentiation). Aligns with ADR 0006 Q1 and CLAUDE.md §Safety
Rules.

**Q2 — Depth: small refactor.** Not a surgical 50-line patch
(too short-sighted — the next detector forces another surgery into
the same function), not a full Template Method overhaul (too much
ceremony for the single-mode reality). The middle option: introduce
the data carrier and the detector, leave the function as a function.

**Q3 — Form: `LoopState` dataclass, not a class hierarchy.**
`QueryEngineCallbacks` already handles variation via composition;
making `run_query_engine` a class with `_before_iteration` /
`_after_decision` methods would create a parallel variation
mechanism (callback **and** override) for the same concern — future
contributors would not know which to use. The data carrier alone is
the missing piece; the function stays a function.

**Q4 — Field set: minimal.** `LoopState` carries only what
ping-pong + repeated-failure detection require: an `iteration`
counter, a bounded `tool_calls` deque, a bounded `errors` deque,
an unbounded `signals` list. No `started_at` (wall-clock timeout is
a separate future detector with its own ADR), no `total_tokens_used`
(the existing token budget tracker at `query_engine.py:532` is the
authoritative single source). YAGNI applied per field.

**Q5 — Reaction: soft correction + observable event.** When the
detector fires, three things happen in lockstep:

1. The signal is appended to `state.signals` (telemetry-visible).
2. `callbacks.on_pathology_signal(signal)` fires; the dispatcher
   translates it into a `PathologyDetected` event in the
   `dispatch(envelope)` stream.
3. Before the next LLM call, the signal is materialized as a
   synthesized tool result inserted into `messages` with role
   `"tool"`, content
   `"Loop detector: tool 'X' was called N times with same arguments.
   Reconsider your approach or finalize with current information."`

The LLM then either revises its plan or returns a final reply. If it
ignores the correction and keeps oscillating, `MAX_TOOL_ITERATIONS = 20`
remains the terminal hard cap. No two-stage soft-then-hard escalation
ladder is built; one cap is sufficient.

**Injection as a tool result, not a system message.** System prompts
are stable identity / tool descriptions; runtime events should not
pollute them. Tool results are already part of the natural LLM
conversation flow; injecting one is `grep`-able for postmortems
(`grep "Loop detector"` over `messages`) and aligns with how Claude
Code performs similar mid-loop self-correction.

### Threshold defaults

Adopt CellClaw's numbers for the first ship; tune in a follow-up PR
if real traces motivate changes.

    pingpong:           same (tool_name, args_digest) appears ≥ 4 times
                        in the last 6 entries of state.tool_calls
    repeated_failure:   same tool_name appears ≥ 4 times in the last 8
                        entries of state.errors

`args_digest` is the SHA-1 hex of a JSON-canonicalised argument dict.
Storing the digest rather than raw arguments keeps `LoopState` from
growing under tools that return or accept large payloads
(`read_file`, MCP image arguments) while preserving the granularity
needed to distinguish `grep(pattern="A")` from `grep(pattern="B")`.

### Module shape

Two new files, both in `runtime/agent/`:

    runtime/agent/loop_state.py        # LoopState, ToolCallRecord,
                                       # ToolErrorRecord, PathologySignal
    runtime/agent/loop_pathology.py    # detect(state) -> Signal | None

`LoopState` is constructed inside `run_query_engine` and discarded
when the call returns. Per-request lifetime, mirroring ADR 0006's
per-request `MessageEnvelope` lifecycle. No module-level state is
introduced.

### Migration phases

Three commits, each independently green on CI.

    L0   land loop_state.py / loop_pathology.py + unit tests
         (no caller touched)
    L1   wire LoopState through run_query_engine; replace local
         iteration / tool_call_history counters with state fields;
         insert detection call after each tool execution; insert
         synthesized tool-result injection before each LLM call.
         QueryEngineCallbacks gains one optional field
         `on_pathology_signal`.
    L2   add PathologyDetected to runtime/agent/events.py;
         dispatcher.py wires on_pathology_signal into the event
         queue; the three Surfaces render the warning line
         (one line each: telegram.py, server.py, interactive.py)

Each step is a separate commit. L0 is purely additive (no existing
caller is touched); L1 is structurally additive (the function shape
changes but no external semantics change — same final reply, same
events, plus optionally `PathologyDetected`); L2 is the user-visible
surfacing. If a Surface-side render decision needs more design
(e.g. Desktop wants a button instead of a line), L2 can split per
Surface without unwinding L0/L1.

## Considered Options

- **Option Engine-Class — promote `run_query_engine` to a class with
  Template Method hooks.** Mirrors CellClaw's
  `DecisionExecutionEngine`. Rejected because
  `QueryEngineCallbacks` already implements composition-style
  variation; adding a parallel inheritance mechanism creates two
  ways to do one thing.

- **Option Engine-Class-Plus-DecisionType — Engine-Class plus
  explicit `DecisionType` enum.** Mirrors CellClaw end-to-end.
  Rejected on top of Engine-Class because the three Surfaces today
  render every reply as a stream of tokens regardless of decision
  type — there is no UI differentiation to consume the enum.
  Reintroduce only when a Surface lands that wants per-decision-type
  rendering (e.g. a card-based desktop UI where ASK_USER is a
  distinct card colour).

- **Option Local-Patch — add ping-pong counters as local variables
  inside `run_query_engine` without `LoopState`.** Cheapest possible
  fix, ~50 LOC. Rejected because the next detector
  (timeout / per-tool budget) forces another local-variable injection
  into the same function body; this is exactly the accretion pattern
  that pushed `run_query_engine` past 460 lines in the first place.
  A typed carrier amortises the structural cost across all current
  and future detectors.

- **Option Hard-Stop — detection raises an exception, loop exits.**
  Rejected as rude: the LLM frequently recovers on the next turn
  given that the corrective context shifts. ADR 0006's `Final.kind`
  discriminator was added precisely so that recoverable terminations
  do not look like errors; the same instinct applies here.

- **Option UI-Escalation — `PathologyDetected` blocks the loop and
  the Surface prompts the user with continue / abort.** Most
  respectful of user agency. Rejected as scope creep under Q2's
  "small refactor" depth: blocking semantics require new round-trip
  protocol on every Surface, and Channel adapters (10 of them) have
  no UI affordance for mid-stream prompts. Reintroduce if a Surface
  lands that has a natural blocking-prompt primitive (e.g. an
  Electron modal).

- **Option Inject-as-System-Message — corrective text is appended
  to the system prompt rather than synthesised as a tool result.**
  Rejected because system prompts are stable identity; mutating
  them mid-loop introduces a hidden coupling between the runtime
  state and what the LLM thinks its persona is. Tool results are
  the right channel because they are already conversational and
  already greppable.

- **Option Args-Digest-Omitted — track tool names only, mirroring
  CellClaw exactly.** Rejected because the false-positive rate is
  unacceptable: `grep(pattern="A")` then `grep(pattern="B")` would
  count as ping-pong. SHA-1 over canonicalised args is cheap
  (microseconds) and bounds the state size by digest cardinality
  rather than payload size.

- **Option Started-At-Field — preemptively add `started_at: float`
  to `LoopState`** so a future wall-clock timeout detector finds it.
  Rejected per Q4: speculative fields rot. Add when the timeout
  detector lands.

- **Option Token-Counter-Field — preemptively add
  `total_tokens_used: int` to `LoopState`.** Rejected because the
  existing token budget tracker (`query_engine.py:532`) is already
  the authoritative single source; duplicating the counter creates
  drift risk.

## Consequences

**Wins**

- Loop pathology detection becomes feasible without further surgery
  into `run_query_engine`'s function body. Ping-pong stops at ~4-6
  iterations instead of 20; repeated failure stops at ~4-8 instead
  of 20. At current API prices, the worst-case savings are
  meaningful per session, but the more important win is faster
  user feedback (the loop visibly aborts before the user starts
  wondering whether it has hung).
- `state.signals` accumulates per-request telemetry that any future
  observability surface (a `/diagnostics` slash command, a desktop
  trace pane) can consume without re-instrumenting the loop.
- The pattern generalises: a future `TimeoutDetector`,
  `PerToolBudgetDetector`, or `RetryStormDetector` is a new pure
  function over `LoopState`, not another surgery into
  `run_query_engine`.
- `LoopState`'s per-request lifetime is consistent with ADR 0006's
  per-request `MessageEnvelope` lifetime; both reinforce the
  no-module-level-mutable-state direction this codebase is moving in.

**Costs**

- Two new modules (`loop_state.py`, `loop_pathology.py`),
  ~150-200 LOC combined.
- `run_query_engine` gains a `state: LoopState` parameter and three
  detection / injection touchpoints inside its body. The function
  itself does not shrink — `LoopState` is additive surface, not a
  refactor of the existing body. The 460+ line readability problem
  is left for a separate ADR.
- `QueryEngineCallbacks` grows by one field
  (`on_pathology_signal`). External code that constructs the
  dataclass positionally would break; a grep over the tree shows
  all constructions are keyword-style, so the additive field is
  safe.
- `dispatch(envelope)`'s event union grows from nine to ten events.
  Surface code that exhaustively matches on event type
  (`isinstance` chains in the three render paths) needs one new
  arm each. The match-or-ignore stance is per-Surface — Channel
  may choose to ignore `PathologyDetected` initially if the
  Telegram render budget is tight.
- The corrective tool-result injection occupies an LLM tool slot
  that the model did not request. Some providers may complain; a
  unit test will pin the exact `messages` shape that survives
  OpenAI, DeepSeek, and Anthropic round-trips. If a provider
  rejects the synthesised result, fall back to a system-message
  append on a per-provider basis — but only if forced.

**Alternatives are catalogued under §Considered Options above.**

## Verification

Per-phase gates, mirroring ADR 0006's L0-L4 pattern.

- **L0:** Unit tests for `loop_pathology.detect`:
  - empty `LoopState` → `None`
  - 3 same-`(name, digest)` entries in last 6 → `None` (below threshold)
  - 4 same-`(name, digest)` entries in last 6 → `PathologySignal(kind="pingpong")`
  - 4 same-`tool_name` failure entries in last 8 →
    `PathologySignal(kind="repeated_failure")`
  - mixed populations → correct discrimination
  - args-digest sensitivity: `grep("A")` + `grep("B")` repeated four
    times does not fire `pingpong`

- **L1:** Integration test against a mock LLM + a mock tool that
  always emits the same `(name, args)`:
  - confirm `PathologySignal` is appended to `state.signals` at
    the expected iteration
  - confirm a synthesised tool-result message is inserted into
    `messages` before the next LLM call, with role `"tool"` and
    content starting `"Loop detector:"`
  - confirm the LLM (mock) sees the synthesised message and can
    respond to it
  - confirm `MAX_TOOL_ITERATIONS = 20` still terminates the loop
    if the mock LLM ignores the correction

- **L2:** End-to-end smoke against each Surface:
  - `oc interactive` — induce ping-pong via a deliberately
    looping skill; verify the CLI prints the warning line; verify
    Final still emerges (soft correction works).
  - `oc desktop-server` — POST `/chat/stream`; verify the SSE
    frame sequence contains a `PathologyDetected` event before
    `Final`.
  - `make bot-telegram` against a sandbox chat — verify the
    warning line appears as a message edit or a separate reply
    line; verify no reply cross-talk (regression for the
    ADR 0006 §Wins guarantee).

A `/graphify` re-index lands after L0 so that knowledge-graph
queries return `runtime/agent/loop_state.py` and
`runtime/agent/loop_pathology.py` as anchors, same caveat as
ADRs 0004, 0005, and 0006.

## Open questions

Tracked but not resolved here:

- **Threshold tuning.** CellClaw's numbers (4-of-6 ping-pong,
  4-of-8 failure) are inherited as v1 defaults. Whether they are
  right for the OmicsClaw skill mix (where some legitimate
  workflows iterate a single tool with stepping arguments — e.g.
  scanning a parameter sweep) needs trace data after L2 ships.
  Revisit with a follow-up PR, not a follow-up ADR.

- **`DecisionType` revisited.** If a future Surface ships a
  card-based render where ASK_USER is visually distinct,
  reintroduce the enum then. The clarification-as-tool-result
  protocol is forward-compatible — promoting it to an explicit
  decision type later adds a discriminator without rewriting the
  carrying mechanism.

- **Wall-clock timeout detector.** Out of scope here; will land
  as a separate detector when a real session-length pain point
  surfaces. `LoopState` will then gain a `started_at` field, and
  `loop_pathology.detect` will gain a `timeout` branch — both
  additive.

- **Per-tool budget detector.** Out of scope. Reuses the same
  `LoopState.tool_calls` deque plus a side counter keyed on tool
  name. Same additive pattern.

- **Surfacing signals to skills.** Skills today have no way to
  read `state.signals` mid-execution. If a skill wants to adapt
  its behaviour based on detected pathology (e.g. switch to a
  fallback algorithm after 4 failures), a future ADR can promote
  signals into the skill runtime context. Not motivated by any
  current skill.
