# Wire `cancel_event` from `MessageEnvelope` through `dispatch()` down to the skill subprocess driver

## Status

Accepted (2026-05-18).

**Identity and carrier refinement (2026-07-14):**
[ADR 0047](0047-separate-inbound-envelope-from-dispatch-context.md) moves the
live cancellation token from serialized request data into Dispatch Context.
[ADR 0051](0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md)
makes opaque Turn ID the cancellation identity; Conversation or legacy
`session_id` may only be a Surface convenience used to resolve the active Turn.
This ADR's end-to-end subprocess propagation remains current.

**Cancellation-trigger refinement (2026-07-14):**
[ADR 0052](0052-bind-retried-ingress-to-one-turn-and-resume-observation.md)
requires explicit `cancel(Turn ID)`. Losing a Desktop SSE connection or another
Response Sink detaches the observer and is not cancellation.

## Context

ADR 0006 §"Open questions" left one item explicitly deferred:

> *Cancellation contract.* Desktop has `pending_preflight_requests` and
> a chat-stream cancel endpoint; CLI has Ctrl-C; Channel has nothing.
> A unified cancel token would naturally live on the `MessageEnvelope`,
> but no Surface today consumes it consistently enough to motivate
> the contract. Revisit when a real cross-Surface cancel concern
> emerges.

Two days later (2026-05-18) a grilling session against the reference
implementation's `agent/tools/base.py:execute_with_cancel_check`
re-opened the question.
The session produced two findings that together cross the "real concern
emerged" bar:

**Finding 1 — long bioinformatics jobs are uncancellable end-to-end.**
The skills under `skills/spatial-velocity/`, `skills/sc-pseudotime/`,
`skills/bulkrna-survival/`, etc. spawn Python subprocesses that run
5–15 minutes (scVelo CNV inference, scanpy harmony integration,
pyDESeq2 with 50k+ genes). A user who realises mid-run that they
passed the wrong `--method` argument has no way to stop the burn:
the LLM stream continues, the subprocess continues, and on shared
hardware the GPU memory stays pinned.

**Finding 2 — the Desktop `/chat/abort` endpoint is wired but断链 ("broken
chain").** `omicsclaw/surfaces/desktop/server.py:1946-1954` defines
`POST /chat/abort` which looks up the session's `asyncio.Task` from
`_active_sessions` and calls `task.cancel()`. That raises
`asyncio.CancelledError` at the next await point in `dispatch()` —
which `runtime/agent/dispatcher.py:126` catches at the outermost
level — but the `skill.runner.run_skill()` call that is currently
streaming subprocess stdout into the tool result never sees the
cancellation. The subprocess keeps running in its detached process
group even though the user pressed the abort button.

The grilling session then surfaced a third finding that reshaped the
ADR from "introduce the reference implementation's mechanism" to "wire
OmicsClaw's existing chain":

**Finding 3 — OmicsClaw already has a more thorough subprocess-level
cancel than the reference implementation.** A trace of `cancel_event` references through
the tree shows two disconnected halves:

| Side | Files | What it does |
|---|---|---|
| **Producer** | `autoagent/api.py:75`, `autoagent/runner.py:47`, `autoagent/optimization_loop.py`, `autoagent/harness_loop.py` | Holds a `threading.Event`, signals `cancel_event.set()` on user / API request, plumbs it through every layer |
| **Consumer** | `skill/runner.py:289-361`, `skill/execution/subprocess_driver.py:70`, `skill/execution/async_subprocess_driver.py:59-83` | `start_new_session=True` + `os.killpg(pgid, SIGTERM)` → 2s grace → `os.killpg(pgid, SIGKILL)`, reaches every grandchild |

Both halves are reachable from the **`autoagent` automated-optimization
path** (harness experiments, optimization loops). Neither half is
reachable from the **`runtime/agent/dispatcher → query_engine →
execute_tool_requests → tool executor`** path that the three live
Surfaces (Channel, Desktop, CLI) actually exercise. A `grep -rn
"cancel_event" omicsclaw/runtime/` returns only two hits, both inside
`dispatcher.py`'s outermost `try/except asyncio.CancelledError`
clean-up — zero in `query_engine.py`, zero in `runtime/tools/`.

In other words: the cancellation infrastructure is **stronger than the
reference implementation's** (the reference implementation's `execute_with_cancel_check` only `task.cancel()`s
the coroutine; OmicsClaw's `subprocess_driver` actively `killpg`s the
process group). The gap is structural, not capability — the chat
loop and the skill runner have never been wired together for cancel
even though both individually support it.

### Why not import the reference implementation's mechanism

The reference implementation's `execute_with_cancel_check` (`agent/tools/base.py:100-156`)
is a 56-line `asyncio.wait_for + asyncio.shield` polling wrapper that
checks `cancel_event` every 50ms. It is a perfect fit for cooperative
cancellation of pure-coroutine tools that do not spawn subprocesses.
OmicsClaw's three live Surfaces drive subprocess-heavy tools through
`skill.runner.run_skill()`, which already does the harder work: killpg
on a process group spawned with `start_new_session=True`, with a
SIGTERM→grace→SIGKILL escalation ladder. Importing `execute_with_cancel_check`
would solve the smaller problem (pure-coroutine tools) at the cost of
introducing a parallel cancel mechanism alongside the killpg one.
After this ADR the pure-coroutine case is left as a follow-up: a
later detector pass (`grep` for tools that do not bottom out at
`skill.runner.run_skill`) can decide whether enough of them exist to
motivate the polling wrapper.

## Decision

Add a single `cancel_event: threading.Event | None = None` field to
`MessageEnvelope`, thread it through `dispatch()` →
`QueryEngineCallbacks` → `execute_tool_requests` →
`tool_runtime.executors[*]` → `skill.runner.run_skill(cancel_event=…)`,
and wire `Desktop /chat/abort` and `CLI SIGINT` to call
`envelope.cancel_event.set()`.

`Channel Surface` is **out of scope** for this ADR — the 10 IM adapters
have no native cancel UI primitive (a `/stop` slash command is the
closest, but routing it through the dispatcher loop is its own
design tree). Adding cancel to Channel will be a follow-up ADR if
and when a real adapter user requests it; the closed-bracket precedent
is ADR 0007 §"Open questions" where `PathologyDetected` rendering on
Channel is similarly deferred.

The five design decisions resolved in the grilling session, in
dependency order:

**Q1 — Forcing function: two concrete pains, not one architectural
itch.** The session considered four candidate forcing functions —
(1) long bioinformatics jobs, (2) zombie subprocesses, (3) Telegram
session timeouts, (4) Desktop断链. Picked (1) + (4) because each
on its own is dismissible ("add a timeout!" / "fix the bug!"); only
the two together explain why this is a *contract* issue and not a
patch. (2) and (3) are downstream beneficiaries called out in
§Consequences but not used as drivers.

**Q2 — Scope: wire the existing chain, do not introduce the reference
implementation's `execute_with_cancel_check`.** The grilling session found OmicsClaw's
`skill/execution/async_subprocess_driver.py:59-83` already implements
SIGTERM→grace→SIGKILL via `os.killpg`, reaching grandchild processes
via `start_new_session=True`. The reference implementation's wrapper is a coarser-grained
`task.cancel()` + 2s wait. Importing the latter on top of the former
would create two cancel mechanisms for the same concern — the same
"parallel variation mechanism" anti-pattern ADR 0007 §Q3 and ADR
0008 §Q2 both rejected. The ADR therefore becomes a wiring change,
not a mechanism import.

**Q3 — Location: `MessageEnvelope.cancel_event`, per-request lifetime.**
ADR 0006 §"Open questions" already telegraphed this: *"A unified cancel
token would naturally live on the `MessageEnvelope`."* The field
joins the existing 18 fields on the `frozen=True` dataclass
(`runtime/agent/envelope.py:18-42`). `threading.Event` is a mutable
object whose **reference** does not change after construction — the
frozen contract is preserved (no field reassignment), only the
event's internal flag mutates via `.set()`. A short docstring note
on the field makes this explicit.

Two alternatives were rejected:

- **Option `RuntimeContext` carrier** — a new dataclass holding
  `cancel_event` plus future `started_at`, `request_id`, etc. Rejected
  because ADR 0007 §Q4 already designated `LoopState` as the
  per-iteration carrier for `started_at`/timeout-detector state; a
  second carrier would split the per-request state surface across two
  classes with no clear rule for which goes where. Reaffirming Q4 here:
  one field is enough; future fields go on the existing carrier whose
  role matches their lifetime.
- **Option Session-level registry** — store `cancel_event` in a
  `chat_id → Event` dict on the session store. Rejected because ADR
  0006 §Q3 fixed the dispatcher's lifecycle as *"construct-run-discard
  per inbound message,"* and a session-spanning registry violates that
  invariant. The `_active_sessions: dict[session_id, asyncio.Task]`
  that already lives at `surfaces/desktop/server.py:1949` is the
  closest equivalent today; in Q4 below we reuse it to look up the
  envelope alongside the task rather than creating a parallel dict.

**Q4 — Surface scope: Desktop + CLI; Channel deferred.** Desktop has
the `/chat/abort` endpoint (`server.py:1946`) and the
`_active_sessions` registry — the wiring is a 3-line change inside
the existing endpoint. CLI has six existing `KeyboardInterrupt`
handlers in `surfaces/cli/interactive.py` (lines 844, 1332, 1577,
1589, 2194, 2282); the relevant one for in-flight dispatch is the
ESC/Ctrl+C branch at line 1577-1589 — also a 3-line change.

Channel Surface has no parallel:
- 10 adapters under `surfaces/channels/{telegram, feishu, slack, …}.py`
- No native "stop generating" affordance in any IM platform's chat UI
- A `/stop` slash command would need to be parsed inside `loop.py:62-63`
  (the slash-command dispatcher) and routed back through the
  `_active_sessions`-style registry per chat_id — at least 50-100 LOC
  per adapter once edge cases (mid-tool-execution `/stop`, dedup,
  rate-limit interaction) are handled
- No user has asked for it; the forcing functions in Q1 are both
  Desktop+CLI scenarios

The same precedent ADR 0007 set for `PathologyDetected` rendering
applies: "Channel may choose to ignore … initially if the Telegram
render budget is tight." Cancel for Channel becomes a follow-up ADR
when a real user complaint surfaces.

**Q5 — Migration: five-step phased, no atomic landing.** Mirrors the
L0-L4 pattern of ADRs 0006, 0007, and 0008. Each step independently
green on CI:

```
ADR   docs(adr): record cancel_event wiring as ADR 0009 (this file)
L1    runtime/agent/envelope.py: add MessageEnvelope.cancel_event field
      + frozen-dataclass-with-mutable-Event docstring note
      + unit test that field defaults to None and is keyword-constructible
      + unit test that frozen=True still rejects field reassignment
L2    runtime/agent/dispatcher.py + query_engine.py + tools/orchestration.py:
      thread envelope.cancel_event through:
        - dispatch() → llm_tool_loop() (loop.py gains a kwarg)
        - llm_tool_loop() → QueryEngineCallbacks new field cancel_event
        - run_query_engine() → execute_tool_requests(requests, cancel_event=…)
        - execute_tool_requests → tool_runtime.executors[*] via
          tool_runtime_context["cancel_event"] (already a dict, no
          signature change for executors)
      + integration test: fake 5-second sleep skill subprocess,
        envelope.cancel_event.set() after 1 second, assert
        Final event NOT emitted, Error(asyncio.CancelledError) IS
        emitted, ps -o pid,sid shows no orphaned subprocess
L3    surfaces/desktop/server.py: /chat/abort sets envelope.cancel_event
      before task.cancel(); store envelope alongside task in
      _active_sessions so the endpoint can look it up
      + smoke test: POST /chat/stream with a long-running skill, POST
        /chat/abort after 1 second, verify SSE stream ends with
        Error frame and the subprocess is killpg'd
L4    surfaces/cli/interactive.py: ESC/Ctrl+C handler in the streaming
      branch sets envelope.cancel_event in addition to its existing
      task cancel; preserve KeyboardInterrupt semantics at the REPL
      level (those are out-of-dispatch and unchanged)
      + manual smoke: `oc interactive` with a long-running skill,
        press Ctrl+C, verify prompt returns and the subprocess is gone
```

L2 is the riskiest commit (touches three modules across two packages);
L1 and L4 are surgical; L3 is mechanical. The ordering is dependency-
forced (L1 must precede L2 must precede L3/L4); within L3/L4 the
order is arbitrary and both can land in parallel PRs.

### The signature changes

#### `MessageEnvelope` (`runtime/agent/envelope.py`)

```python
@dataclass(frozen=True)
class MessageEnvelope:
    # … 18 existing fields …
    cancel_event: threading.Event | None = None
    """Set by the Surface to request mid-flight cancellation.

    The dataclass is frozen, but ``threading.Event`` is a mutable
    object — only its internal flag changes via ``.set()``, never
    the field's reference. This preserves the frozen contract
    (no reassignment) while allowing the live cancel signal.

    None means cancellation is not supported by the calling Surface
    (Channel Surface today). The dispatch chain treats None as
    "never cancelled" and skips all cancel-check logic.
    """
```

#### `QueryEngineCallbacks` (`runtime/agent/query_engine.py`)

```python
@dataclass
class QueryEngineCallbacks:
    # … 8 existing fields …
    cancel_event: threading.Event | None = None
    """Forwarded from MessageEnvelope. Threaded into
    execute_tool_requests via tool_runtime_context."""
```

#### `execute_tool_requests` (`runtime/tools/orchestration.py:650`)

```python
async def execute_tool_requests(
    requests: list[ToolExecutionRequest],
    *,
    cancel_event: threading.Event | None = None,
) -> list[ToolExecutionResult]:
    """… existing docstring … plus:

    When ``cancel_event`` is set mid-execution, in-flight tools that
    bottom out at ``skill.runner.run_skill`` get killpg'd; the function
    returns whatever results completed before the signal, plus a
    cancellation-marker result for the in-flight one.
    """
```

The cancel_event reaches the skill driver via the existing
`tool_runtime_context: dict[str, Any]` channel that already plumbs
`workspace`, `pipeline_workspace`, etc. through to each executor.
Adding one key (`"cancel_event"`) avoids changing 60+ executor
signatures.

## Considered Options

- **Option Reference-Mechanism — import `execute_with_cancel_check`.**
  56-line `asyncio.wait_for + shield` polling wrapper. Rejected because
  `skill/execution/async_subprocess_driver.py:59-83` already implements
  stronger subprocess-level cancel via killpg; importing the wrapper
  would create two cancel mechanisms for the same concern (the
  parallel-variation anti-pattern ADRs 0007 §Q3 and 0008 §Q2 both
  rejected). Reintroduce only if a meaningful population of pure-
  coroutine tools (no subprocess) emerges that the killpg path
  cannot cover.

- **Option RuntimeContext-Carrier — new dataclass holding cancel_event +
  future per-request fields.** Rejected because ADR 0007 §Q4 already
  designated `LoopState` as the per-request/iteration carrier for
  state that detectors consume; a second carrier with no clear rule
  for which fields go where would re-create the split-state problem
  ADR 0007 §Q4 solved. A single field on `MessageEnvelope` is the
  honest declaration of where this dependency comes from.

- **Option Session-Registry — `chat_id → cancel_event` dict on the
  session store.** Rejected because ADR 0006 §Q3 fixed the dispatcher's
  lifecycle as construct-run-discard per inbound message; a session-
  spanning registry would silently extend cancel state across turns
  in a way that breaks the per-turn isolation. The existing
  `_active_sessions: dict[session_id, asyncio.Task]` on Desktop is
  retained but kept scoped to its current purpose (look up the task
  to cancel); cancel state stays on the envelope it travels with.

- **Option All-Surfaces — wire Channel adapters too.** Rejected per
  Q4: no Channel adapter has a native cancel UI primitive; a `/stop`
  slash command would need slash-dispatcher rework plus per-adapter
  edge-case handling (mid-tool `/stop`, dedup, rate-limit interaction),
  estimated 500-1000 LOC across 10 adapters. No user has asked. ADR
  0007 set the precedent for deferring Channel rendering of cross-
  cutting concerns until a real user request lands.

- **Option Atomic — land L1-L4 in one commit.** ~150-200 LOC across
  five files. Rejected because the four phases are independently
  shippable (each one valid and CI-green on its own), and the L2
  signature change is the largest risk surface — bisectability across
  L1/L2/L3/L4 buys real value when a regression surfaces. Same
  reasoning ADRs 0006, 0007, 0008 used.

- **Option asyncio.Event instead of threading.Event.** Cleaner asyncio
  semantics on the dispatch side. Rejected because `skill.runner.run_skill`,
  `subprocess_driver.drive_subprocess`, and the entire `autoagent/`
  cancel chain already consume `threading.Event` (~12 call sites).
  Switching to `asyncio.Event` would require either dual-typing every
  consumer or converting at the boundary; the conversion would
  introduce a bridging actor that this ADR exists to avoid. Stay
  consistent with the existing chain; the dispatcher already runs
  inside an event loop and `Event.set()` is thread-safe-callable from
  either side.

- **Option Add `cancel_event` to every executor signature.** More
  explicit than threading through `tool_runtime_context`. Rejected
  because 60+ executors in `runtime/tools/builders/agent_executors.py`
  would all need their signatures updated, and most never spawn
  subprocesses — they would ignore the field. Threading through the
  existing dict gives the same capability where it matters
  (executors that call `skill.runner.run_skill`) without the
  60-file edit.

- **Option Skip the ADR, just fix /chat/abort as a bug.** Rejected
  because the fix is necessarily structural: `task.cancel()` does not
  propagate to a subprocess in another process group, so the
  "single-line bug fix" is impossible. The minimal fix is the field
  + the threading + the wire-up, which is exactly what this ADR
  proposes. Recording it as an ADR ensures a future reader who finds
  `threading.Event` on a frozen dataclass understands why.

## Consequences

**Wins**

- Closes ADR 0006 §"Open questions" entry on the Cancellation
  contract. The 4-day-old deferral resolves with a concrete contract.
- Long bioinformatics jobs (`skills/spatial-velocity`, `skills/sc-pseudotime`,
  `skills/bulkrna-survival`, `skills/sc-de`, all of which spawn
  Python subprocesses for 5–15 minutes) become user-cancellable
  end-to-end. The GPU-memory-pinning scenario in Finding 1 closes:
  user presses abort → subprocess group receives SIGTERM →
  scanpy/pyDESeq2 release GPU memory within the 2-second grace
  before SIGKILL.
- Desktop `/chat/abort` (`server.py:1946`) goes from "broken chain"
  to working end-to-end. The fix is a 3-line change in the
  endpoint plus the L2 plumbing.
- Zero new cancel mechanism. The wiring composes two existing
  mechanisms (the `autoagent` cancel_event producer side and the
  `subprocess_driver` killpg consumer side) that already work
  individually.
- The `cancel_event` field on `MessageEnvelope` becomes the natural
  home for any future Surface to opt into cancellation (e.g.
  Channel's hypothetical `/stop` command in a follow-up ADR). The
  contract is fixed; only the Surface-side wire-up is the new work.
- The `tool_runtime_context: dict` injection point at
  `runtime/agent/query_engine.py:104` already exists and already
  passes `workspace`/`pipeline_workspace`/etc.; adding one key
  is structurally trivial and a precedent for future per-request
  data needs.

**Costs**

- `MessageEnvelope` grows from 18 fields to 19. The new field
  carries the only mutable-internal-state field on the otherwise-
  immutable dataclass; the docstring note is load-bearing
  documentation that must survive future field reorderings or
  documentation rewrites.
- `QueryEngineCallbacks` grows by one optional field (`cancel_event`),
  mirroring the additive growth pattern ADR 0007 §Costs flagged
  for `on_pathology_signal`. External code that constructs the
  dataclass positionally would break; a grep over the tree shows
  all constructions are keyword-style (same as ADR 0007's grep),
  so the additive field is safe.
- `execute_tool_requests` gains a keyword-only kwarg. All current
  call sites (`query_engine.py:1100`, two test sites) need updating
  in L2.
- `tool_runtime_context: dict[str, Any]` gains one reserved key
  (`"cancel_event"`). Any downstream consumer of that dict that
  iterates keys will see the extra entry — none currently iterate
  (grep), but the convention now is "the dict carries cancel signal
  too" which a future contributor needs to know.
- Channel Surface remains cancel-less. Telegram / Feishu / etc.
  users who run a 10-minute spatial-velocity command and want to
  stop it have no recourse this PR — same as today. Documented as
  intentional in Q4 with a forward pointer to the follow-up ADR.
- Pure-coroutine tools (if any are added later) are not covered.
  The killpg path is subprocess-shaped; a future tool that holds
  the asyncio loop busy for 5 minutes without spawning a subprocess
  would only respond to the outer `task.cancel()`, not to
  `cancel_event.set()`. If such a tool category appears, the
  follow-up is to import the reference implementation's `execute_with_cancel_check`-style
  wrapper at that point (the rejected Option above), narrowly
  applied to that tool class.
- One frozen-dataclass-with-mutable-Event idiom now exists in the
  codebase. Code reviewers unfamiliar with the pattern will need
  the docstring's explanation. Reviewers can also be pointed at
  this ADR's Q3 for the rationale.

**Alternatives are catalogued under §Considered Options above.**

## Verification

Per-phase gates, mirroring the L0-L4 patterns of ADRs 0006, 0007, 0008.

- **L1:** `pytest tests/test_envelope.py` (new file or extending
  whichever existing test module first imports `MessageEnvelope`).
  Cases:
  - `MessageEnvelope(chat_id=1, content="hi")` constructs with
    `cancel_event = None`.
  - `MessageEnvelope(chat_id=1, content="hi", cancel_event=threading.Event())`
    constructs; `env.cancel_event.set()` flips the flag; the field
    reference itself is the same object before and after.
  - Attempting `env.cancel_event = threading.Event()` after
    construction raises `FrozenInstanceError` (proves frozen still
    bites field reassignment).

- **L2:** integration test against a stub skill subprocess.
  `tests/test_dispatch_cancel.py` (new):
  - Spawn a fake skill (`sh -c 'sleep 5'`) through the executor.
  - 1 second in, call `envelope.cancel_event.set()`.
  - Assert: `Final` event NOT emitted, `Error(asyncio.CancelledError)`
    IS emitted, `os.waitpid` confirms no orphaned subprocess in the
    skill's `start_new_session=True` process group.
  - Run the full 65-test `pytest tests/test_query_engine*.py
    tests/test_query_engine_pathology.py tests/test_agent_dispatcher.py
    tests/test_query_engine_compaction_callback.py
    tests/test_query_engine_deepseek_passback.py
    tests/test_query_engine_reasoning_capture.py
    tests/test_query_engine_stream_usage.py` invocation
    inherited from ADR 0008 §Verification — must remain green.

- **L3:** `oc desktop-server` smoke.
  - Start server, POST `/chat/stream` with a request that invokes
    a long-running skill (sc-pseudotime on the demo dataset).
  - 1 second in, POST `/chat/abort` with the session id.
  - Verify: SSE stream ends with `Error` frame within 2.5 seconds
    (1s + 2s grace), `ps -ef | grep skill_runner` shows no
    leftover subprocess, server.log shows the cancel event was
    set before the task was cancelled.

- **L4:** `oc interactive` smoke.
  - Start REPL, send a message that invokes the same long-running
    skill.
  - During streaming output, press Ctrl+C (or ESC, both should
    fire the same handler at `interactive.py:1577-1589`).
  - Verify: prompt returns within 2.5 seconds, `pgrep -P $$`
    shows no leftover Python subprocess from the skill.

- **Final:** `pytest tests/ --ignore=tests/integration --ignore=tests/eval`
  — full regression. Result must match the post-ADR-0008 baseline
  (25 pre-existing failures, 0 new; 2677 passed plus any new test
  cases from L1/L2).

A `/graphify` re-index lands after L4 so that knowledge-graph queries
for "cancellation" / "abort" / "long-running skill" return the new
wiring anchors rather than the dead `/chat/abort` endpoint.

## Open questions

Tracked but not resolved here:

- **Channel Surface cancellation.** Telegram / Feishu / Slack /
  Discord / WeChat / WeCom / DingTalk / iMessage / Email / QQ
  (10 adapters) remain cancel-less. The natural primitive is a
  `/stop` slash command routed through `surfaces/channels/commands`,
  but the slash dispatcher also has the reverse-import problem from
  ADR 0006 §"Open questions" still unresolved. Follow-up ADR if and
  when a real user requests cancel on an adapter.

- **Pure-coroutine cancellation.** Tools that do not spawn
  subprocesses (none today, but `runtime/tools/builders/agent_executors.py`
  could grow some) would respond to the outer `task.cancel()` but
  not to `cancel_event.set()`. If such a tool category emerges,
  apply the reference implementation's `execute_with_cancel_check`-style wrapper
  narrowly to it, rather than promoting the wrapper to the whole
  executor path.

- **Cancellation telemetry.** Today the cancel reason is implicit
  (whoever called `.set()` knows). Surfacing "who cancelled, when,
  why" through a new `Cancelled(source: str, reason: str)` event
  on the `dispatch` stream is forward-compatible but premature
  until a Surface needs to render the reason differently from a
  generic `Error`.

- **Auto-cancel on session timeout.** A Surface could
  `cancel_event.set()` after a wall-clock timeout (e.g. 10 minutes
  of no progress events). This would compose naturally with ADR
  0007's deferred wall-clock timeout detector — both would set the
  same event. Out of scope here; revisit when the detector lands.

- **MCP server interaction.** MCP tools spawned via the MCP protocol
  bypass `skill.runner.run_skill` and therefore the killpg path. If
  an MCP tool hangs, `cancel_event` will not reach it. The MCP
  client wrapper would need its own cancel forwarding; out of scope
  for this ADR.
