# Decompose `run_query_engine` into four private helpers

## Status

Accepted (2026-05-16).

## Context

ADR 0007 §Costs deferred a known problem:

> *`run_query_engine` gains a `state: LoopState` parameter and three
> detection / injection touchpoints inside its body. The function
> itself does not shrink — `LoopState` is additive surface, not a
> refactor of the existing body. The 460+ line readability problem
> is left for a separate ADR.*

This is that ADR.

`omicsclaw/runtime/agent/query_engine.py:520` (`run_query_engine`)
now spans **491 lines of body** (lines 520–1011 in the post-L1 file).
The function is the single entry point that drives one chat turn
through "LLM call → tool exec → result handling" until a final reply
or the iteration cap. Every Surface, every detector, every
cross-cutting concern ultimately bottoms out here.

A structural read of the function identifies **seven natural phases**:

| # | Phase | Location | Lines | Density |
|---|---|---|---|---|
| 0 | Setup (callbacks, hooks, transcript priming, `LoopState`) | 520–591 | 63 | Linear |
| 1 | Per-iteration message preparation | 592–620 | 28 | Linear |
| 2 | LLM call + reactive-compact retry loop | 621–696 | 75 | **Dense** — nested `try/while/break`, 3-way control flow |
| 3 | Assistant response persistence | 697–714 | 17 | Linear |
| 4 | No-tool-calls → terminate | 715–728 | 13 | Linear |
| 5 | Tool execution request build | 729–793 | 64 | Mid — nested loop over `tool_calls` |
| 6 | Tool execution + approval resolution | 794–875 | 81 | **Dense** — 4-level nesting in approval flow |
| 7 | Per-result post-processing + pathology | 876–1000 | 124 | **Dense** — hook events, store, callbacks, LoopState recording, interruption check, pathology detect + inject |

Three of the seven phases (2, 6, 7) plus the long build phase (5)
account for the bulk of cognitive load. They share a pattern: dense
nested control flow + state that crosses phase boundaries.

The pain surfaces during routine maintenance. Wiring the L1 pathology
hook in ADR 0007 required reading every phase to find a safe
insertion point, even though the actual change was 7 lines. The next
detector (wall-clock timeout, per-tool token budget — both listed in
ADR 0007 §Open questions) will repeat the same archaeology against
a function that is now 491 lines instead of 460.

### Why not the CellClaw approach

CellClaw's analog `DecisionExecutionEngine`
(`agent/execution_engine.py:49`) reaches **1348 lines** and uses
class methods + Template Method hooks (`_before_iteration`,
`_after_decision`, `_handle_terminal_decision`) to split concerns.
ADR 0007 §Q3 explicitly rejected importing that shape because
OmicsClaw runs a single execution mode (no direct-vs-queue split
that motivated CellClaw's class hierarchy), and because
`QueryEngineCallbacks` already provides composition-style variation.
Reaffirming here: this ADR does **not** promote `run_query_engine`
to a class. The decomposition is into free private async functions
in the same module.

## Decision

Extract **four private async helpers** in `query_engine.py`,
covering the four densest sub-flows identified in §Context. The
main `run_query_engine` body shrinks from 491 → ~250 lines while
remaining a single async function with the same external signature.

The four design decisions resolved in the grilling session, in
dependency order:

**Q1 — Goal: pure readability.** Not testability (we already have
65 close-net tests covering every external behaviour through
`run_query_engine`'s seam), not extension surface (ADR 0007 §Q3
explicitly rejected designing for hypothetical future hooks). The
single forcing function is "the function is too long to navigate
when adding a 7-line hook." Picking one driver keeps the refactor
narrow; testability and extension-friendliness are byproducts that
arrive for free without dictating the cut lines.

**Q2 — Strategy: surgical extraction, not blanket per-phase
decomposition.** Three alternative strategies were considered:

- **Per-phase (7–8 helpers)** — rejected because phases 3 (17 lines)
  and 4 (13 lines) are short and linear; extracting them is
  ceremony with no payoff.
- **Grouped-by-verb (4 helpers: setup / plan / execute / process)** —
  rejected because the cross-phase state pressure forces a private
  `_RunContext` dataclass to keep signatures sane (the "plan turn"
  group spans 3 phases and reads/writes 7+ variables). Such a
  carrier is one step away from the class promotion ADR 0007 §Q3
  rejected.
- **Surgical (extract only the dense lumps)** — accepted. Targets
  exactly the four sub-flows whose density makes the function hard
  to navigate. Each extracted helper has narrow inputs (≤13
  parameters) and clear single-value or tuple returns. The leftover
  main body shrinks proportionally without inheriting any new
  abstraction.

**Q3 — Module location: same file, `_`-prefixed private functions.**
`query_engine.py` already hosts 19 underscore-prefixed module-level
helpers (`_extract_completion_tokens`, `_materialize_message`,
`_persist_prepared_compaction`, …) — adding four more aligns with
the existing convention. Splitting to `query_engine_helpers.py` or
to topical files (`_llm_call.py`, `_approval.py`, `_tool_outcome.py`)
was rejected: the helpers are tightly coupled to the same private
types (`MaterializedMessage`, `ToolExecutionRequest`,
`QueryEngineCallbacks`) and live within the same conceptual unit;
fragmenting them costs import overhead without buying any module
boundary.

**Q4 — Migration: one helper per commit (4 commits) + ADR
doc commit = 5 commits total.** Single-commit landing (~250 line
diff) was considered and rejected — bisectability matters when
mechanical refactors silently break a subtle control-flow assumption
(the `has_attempted_reactive_compact` flag, the `current_policy_state`
mutation chain, the `interruption_message` early-return semantics
are all the kind of state that a careless extraction can drop on
the floor). Per-helper commits also mirror the phased pattern
established in ADRs 0004 / 0006 / 0007, which a future reader can
bisect through `git log` without context switching.

### The four helpers

#### 1. `_call_llm_with_reactive_compact_retry`

Extracts phase 2 (`query_engine.py:621–696`, ~75 lines).

```python
async def _call_llm_with_reactive_compact_retry(
    *,
    llm,
    config: QueryEngineConfig,
    callbacks: QueryEngineCallbacks,
    system_prompt: str,
    history: list,
    chat_id: int | str,
    tool_result_store: ToolResultStore,
    transcript_store: TranscriptStore,
    compaction_metadata: dict | None,
    compaction_workspace: str | None,
    request_tools: list,
    on_usage_delta: Callable,
    has_attempted_reactive_compact: bool,
) -> tuple[MaterializedMessage, bool]:
    """One LLM turn with on-demand reactive compaction.

    Returns the materialised assistant message plus the (possibly
    flipped) ``has_attempted_reactive_compact`` flag.
    """
```

13 inputs is the largest signature of the four. Every input is
load-bearing — reactive compaction needs `compaction_metadata`,
`compaction_workspace`, and the `context_compaction` portion of
config; the inner `try/except llm_error_types` branch needs both
the LLM client and the on-error callback. Accepting the wide
signature is the cost of NOT introducing a `_RunContext` carrier;
the carrier would have hidden the same dependencies behind a single
parameter without removing them.

The `(message, flag)` tuple return preserves the one-shot semantics
of reactive compact: once the flag is set in this run, the helper
will not retry compaction on a subsequent LLM error. The caller
mirrors the flag back into its own local before the next iteration.

#### 2. `_resolve_tool_approval_flow`

Extracts phase 6 (`query_engine.py:796–875`, ~80 lines — the body
of the `if callbacks.request_tool_approval is not None:` block).

```python
async def _resolve_tool_approval_flow(
    *,
    execution_results: list[ToolExecutionResult],
    callbacks: QueryEngineCallbacks,
    context: QueryEngineContext,
    current_policy_state: ToolPolicyState | None,
) -> tuple[list[ToolExecutionResult], ToolPolicyState | None]:
    """Drive ``request_tool_approval`` for any REQUIRE_APPROVAL
    results. Returns (resolved_results, possibly_updated_policy_state)."""
```

4 inputs, 2 outputs — the cleanest signature of the four, which
itself validates the surgical strategy: the densest visible
sub-flow turns out to have the narrowest natural seam. The
4-level nesting in the original (loop over results → check policy
→ call approval callback → conditional re-execute) collapses into
one helper that reads as "process this list of results through the
approval pipeline."

#### 3. `_record_tool_outcome`

Extracts the body of phase 7's per-result loop
(`query_engine.py:877–950`, the body of
`for execution_result in execution_results:` ~74 lines, **excluding**
the `interruption_message` early-return check and the pathology
detect+inject that follow the loop and stay in the main body).

```python
async def _record_tool_outcome(
    *,
    execution_result: ToolExecutionResult,
    context: QueryEngineContext,
    callbacks: QueryEngineCallbacks,
    tool_runtime_context: dict | None,
    tool_result_store: ToolResultStore,
    transcript_store: TranscriptStore,
    hook_runtime,
    tool_state: Any,
    state: LoopState,
) -> str:
    """Persist one tool result, fire hooks and after_tool callback,
    record into LoopState. Returns the per-result candidate
    interruption_message ('' if none)."""
```

9 inputs is "medium" — the inflation comes from the three coexisting
"after-tool" mechanisms this code path has accreted (hook_runtime
events, pre-call rule preamble injection from
`tool_runtime/execution_hooks.py`, the legacy `after_tool` callback).
Untangling those three is **out of scope** for this ADR — it is its
own historical-debris cleanup that needs ADR 0009 or later.

The interruption-message check itself stays in the main body; the
helper returns the candidate string and the caller decides what to
do with it (preserving the original "first non-empty wins"
semantics).

#### 4. `_build_execution_requests`

Extracts phase 5 (`query_engine.py:741–794`, ~52 lines). The
"bonus" fourth helper agreed on in Q3.1 of the session.

```python
async def _build_execution_requests(
    *,
    tool_calls: list,
    context: QueryEngineContext,
    callbacks: QueryEngineCallbacks,
    tool_runtime: ToolRuntime,
    tool_runtime_context: dict | None,
    current_policy_state: ToolPolicyState | None,
    hook_runtime,
) -> tuple[list[ToolExecutionRequest], dict[str, Any]]:
    """Parse each assistant tool_call into a ToolExecutionRequest,
    emit EVENT_TOOL_BEFORE hooks, and run before_tool callbacks.
    Returns (requests, tool_states_by_call_id)."""
```

7 inputs, 2 outputs. The `tool_states` dict carries
before_tool-callback return values keyed by `call_id`; the caller
later threads it into `after_tool` invocations (currently inside
`_record_tool_outcome`). Including this helper takes the main body
from ~150 → ~110 lines, which is the point where the for-loop
becomes a 5-verb narrative: prepare → call → persist → execute →
process.

### Migration phases

Five commits, each independently green on CI.

    ADR  docs(adr): record run_query_engine decomposition as ADR 0008

    L1   refactor(query_engine): extract _call_llm_with_reactive_compact_retry
    L2   refactor(query_engine): extract _resolve_tool_approval_flow
    L3   refactor(query_engine): extract _record_tool_outcome
    L4   refactor(query_engine): extract _build_execution_requests

Order is chosen by *density* (largest control-flow lump first): the
LLM-call retry block is the riskiest to extract correctly (nested
try/while/break with reactive compact recovery), so it lands first
while the surrounding context is still maximally familiar. The
build phase is the cleanest and lands last as a low-risk
victory-lap commit. Any commit can be reverted independently
without unwinding the others.

Each commit body:
- pulls a contiguous block of code out of `run_query_engine` into a
  new module-level `_`-prefixed async function
- replaces the original block with a single `await _helper(...)` call
- runs `pytest tests/test_query_engine*.py tests/test_query_engine_pathology.py tests/test_agent_dispatcher.py tests/test_query_engine_compaction_callback.py tests/test_query_engine_deepseek_passback.py tests/test_query_engine_reasoning_capture.py tests/test_query_engine_stream_usage.py` (65 tests) and confirms green

After L4, one final regression matches the post-ADR-0007 baseline
(25 pre-existing failures, 0 new).

## Considered Options

- **Option Class-Promotion — make `run_query_engine` a class with
  Template Method hooks.** Mirrors CellClaw's
  `DecisionExecutionEngine`. Rejected for the same reason ADR 0007
  §Q3 rejected it: composition via `QueryEngineCallbacks` already
  provides variation; class promotion would create two ways to do
  one thing. Reaffirmed here.

- **Option Per-Phase — extract one helper per natural phase
  (7–8 helpers).** Rejected because phases 3 and 4 (17 and 13
  lines respectively, both linear) gain nothing from extraction.
  YAGNI applied at the helper level: only extract where the cost
  of inline reading exceeds the cost of a function call.

- **Option Grouped-by-Verb — 4 helpers along setup / plan /
  execute / process verbs.** Cleanest narrative on paper but
  forces a `_RunContext` dataclass to keep signatures sane (the
  "plan turn" helper would have 10+ inputs). The carrier is a
  class in all but name; ADR 0007 §Q3 ruled against that line.

- **Option Carrier-Pattern — introduce `_RunContext` dataclass
  threaded through every helper.** Hides the wide signatures
  behind a single parameter. Rejected because the wide signatures
  are *honest*: they declare exactly what each helper depends on.
  A carrier would make every helper appear to depend on the entire
  context, defeating the readability win.

- **Option Split-File — move helpers to
  `query_engine_helpers.py`.** Rejected: 19 existing private
  helpers already live in `query_engine.py`; splitting four off
  would fragment a logical unit and add cross-file import
  overhead. The file size (currently ~1010 lines, ~960 after L4
  because some local variables in `run_query_engine` get
  reorganised) is a separate concern. Splitting for size alone
  is treating the symptom, not the cause.

- **Option Topical-Split — three new files
  `_llm_call.py` / `_approval.py` / `_tool_outcome.py`.**
  Rejected as scope creep beyond the smallest clear change. The
  test directory structure would also have to absorb the split,
  and the helpers' tight coupling to private types makes the
  module boundary nominal rather than meaningful.

- **Option Single-Commit — land all four extractions in one
  commit.** ~250 line diff. Rejected because the four extractions
  are independent and mechanical: per-commit landing buys real
  bisectability against subtle control-flow regressions
  (`has_attempted_reactive_compact` flag flow, `current_policy_state`
  mutation chain, `interruption_message` early-return semantics
  are exactly the kind of state that an inattentive extraction
  drops). Same reasoning ADR 0006 §Q8 used to choose L0-L4 phased
  landing over atomic landing.

- **Option Add-Helper-Unit-Tests — write a dedicated test file
  exercising each helper in isolation.** Out of scope for this
  ADR. Q1 picked "pure readability" as the driver; testability is
  a byproduct that arrives because helpers can now be mocked
  individually, but adding tests proactively pivots the goal to
  Q1=B (testability) and inflates the PR with code that the
  existing 65 integration tests already exercise. Reintroduce
  if and when a regression slips past the integration suite that
  a helper unit test would have caught.

## Consequences

**Wins**

- `run_query_engine` body shrinks from 491 → ~250 lines. The
  main for-loop becomes a 5-verb narrative (prepare → call →
  persist → execute → process) that a new reader can trace in
  one screen rather than scrolling.
- Future detectors and callbacks have a smaller insertion target.
  When the wall-clock timeout detector or per-tool budget detector
  from ADR 0007 §Open questions lands, the contributor reads
  `_record_tool_outcome` (74 lines) rather than archaeology
  through 491.
- Bisectability: a regression introduced by any single extraction
  is isolated to that L1-L4 commit. ADR 0006's "smallest clear
  change" principle applies at the commit level, not just at the
  ADR level.
- The cleanest helper (`_resolve_tool_approval_flow`, 4 inputs)
  validates the surgical strategy: density does correlate with
  natural seams, and we are extracting along those seams.
- The 19 existing module-level `_`-prefixed helpers in
  `query_engine.py` now have four more in the same style; the
  module's internal organisation becomes more consistent rather
  than less.

**Costs**

- `query_engine.py` total line count is roughly unchanged (~1010 →
  ~960). The win is in function-level cognitive load, not in file
  size. A reader who navigates by line count rather than function
  scope will not perceive a win.
- Four new module-level names (`_call_llm_with_reactive_compact_retry`,
  `_resolve_tool_approval_flow`, `_record_tool_outcome`,
  `_build_execution_requests`). Anyone reading the function inline
  must now context-switch to the helper definitions to read the
  details — the tradeoff against scrolling-through-491-lines.
- The largest helper signature (`_call_llm_with_reactive_compact_retry`,
  13 inputs) is wide enough to invite future "wrap this in a
  carrier" pressure. The ADR-level decision is explicitly that the
  width is honest declaration of dependencies, not a code smell to
  fix — but the line will need defending in code review.
- The 3-mechanism after-tool layer in `_record_tool_outcome`
  (hook_runtime events + pre-call rule preamble + legacy
  `after_tool` callback) survives intact. Untangling that knot is
  its own ADR.

**Alternatives are catalogued under §Considered Options above.**

## Verification

Per-phase gates, mirroring the per-L gate pattern of ADR 0006 and
ADR 0007.

- **L1:** `pytest tests/test_query_engine.py
  tests/test_query_engine_compaction_callback.py
  tests/test_query_engine_deepseek_passback.py
  tests/test_query_engine_reasoning_capture.py
  tests/test_query_engine_stream_usage.py
  tests/test_query_engine_pathology.py
  tests/test_agent_dispatcher.py` — 65 tests must remain green.
  The reactive-compact path is exercised by
  `test_query_engine_compaction_callback.py` (5 cases); the
  retry-on-prompt-too-long path is exercised by the
  `_FakePromptTooLongError` fixture in `test_query_engine.py`.

- **L2:** Same 65-test invocation green. The approval flow is
  exercised by `test_run_query_engine_records_policy_blocked_tool_results`
  and any test using `REQUIRE_APPROVAL` policy decisions in
  `test_query_engine.py`.

- **L3:** Same 65-test invocation green. Per-result handling is
  exercised by every test that runs at least one tool, including
  `test_run_query_engine_executes_tools_and_records_transcript`
  and the four pathology tests in
  `test_query_engine_pathology.py` (which depend critically on
  `ToolCallRecord` / `ToolErrorRecord` being appended into
  `LoopState`).

- **L4:** Same 65-test invocation green. The tool-request build
  path is exercised by every test that emits tool_calls.

- **Final:** Full regression
  `pytest tests/ --ignore=tests/integration --ignore=tests/eval
  --deselect tests/test_autoagent_runtime.py::test_call_llm_reuses_active_provider_runtime`
  — result must match the post-ADR-0007 baseline (25 pre-existing
  failures, 0 new; 2677 passed). Any deviation indicates a
  refactor regression and the offending L-commit can be reverted
  in isolation.

A `/graphify` re-index lands after L4 so that knowledge-graph
queries for "LLM call retry" / "tool approval flow" return the new
helper anchors rather than the buried original blocks.

## Open questions

Tracked but not resolved here:

- **`query_engine.py` total file size.** Stays at ~960 lines after
  L4. Splitting the module is a separate decision driven by
  module-boundary semantics (does anything in this file belong in
  a different conceptual unit?), not by line count. No current
  signal forces the split.

- **The three-mechanism after-tool layer in `_record_tool_outcome`.**
  `hook_runtime` events, pre-call rule preamble injection from
  `tool_runtime/execution_hooks.py`, and the legacy `after_tool`
  callback all fire on every tool result. Consolidating them is
  its own historical-debris cleanup; this ADR preserves the
  existing 3-way fan-out verbatim inside the helper.

- **Helper unit tests.** Each helper is now individually mockable.
  Whether to add per-helper unit tests is a Q1=B (testability)
  decision; Q1=A (readability) deferred it. Reintroduce when a
  regression slips past the 65 integration tests in a way that a
  unit test would have caught.

- **`_RunContext` carrier.** The widest helper signature
  (`_call_llm_with_reactive_compact_retry`, 13 inputs) is honest
  about dependencies but invites code-review pressure to "tidy it
  up." That pressure should be deflected: ADR 0007 §Q3 and this
  ADR §Q2 both ruled against state-carrier classes. Document the
  rationale once in code comments if the question recurs in review.

- **Cross-file split for further growth.** If any of the four
  helpers grows past ~100 lines on its own (e.g.
  `_resolve_tool_approval_flow` if the approval semantics expand),
  a `query_engine_approval.py` could become justified. Not
  motivated by current code state.
