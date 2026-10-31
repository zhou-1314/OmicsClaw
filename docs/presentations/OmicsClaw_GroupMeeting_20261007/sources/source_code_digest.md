# OmicsClaw 源码摘录

快照提交：`43e9c034b1f04773aa2121d9aab0944beec3ec57`。分支：`feat/spatial-skill-migration`。
以下摘录用于核对当前组件的职责和接口。历史计划中的状态不能替代当前实现。

## `skills/_sdk/notebook/contract.py`

Pure literals shared by the step runner, the framework and the tests.

Every name here is a literal that code outside this package reads with
``ast.literal_eval`` instead of importing it, the way ``DEPENDENCIES`` in
``skills/_sdk/deps.py`` is read.

``LAYOUT`` fixes the project layout: the module, step, validate-step and
report names, the output folders a step may write, and where the runner
keeps its state inside a module's results, the review brief included.

``ENVIRONMENT`` names the variables the runner sets for a step's kernel.

``LEDGER_EVENTS`` maps every ledger event to the fields it always carries.
Every ledger line is one JSON object that also carries ``v`` (the ledger
version, 1), ``event`` and ``at`` (UTC, ISO 8601).

``MANIFEST_SCHEMA`` describes ``provenance/manifest.json``: its required
keys and their types, the keys that may be ``null``, the module status
values, the keys and states of each step entry, and the keys of the replay
record and of each ``review_history`` entry.

公开定义位置：
```text

```

## `skills/_sdk/notebook/run.py`

The step runner: run a project's analysis modules and record what they did.

Call it from the project root (the folder holding ``analysis/`` and
``results/``)::

    python <skills>/_sdk/notebook/run.py new clustering
    python <skills>/_sdk/notebook/run.py run analysis/03_clustering
    python <skills>/_sdk/notebook/run.py status
    python <skills>/_sdk/notebook/run.py replay analysis/03_clustering
    python <skills>/_sdk/notebook/run.py accept analysis/03_clustering --review results/03_clustering/reviews/2026-10-01_review.md
    python <skills>/_sdk/notebook/run.py reference write_output

Locks are ``fcntl.flock`` files inside ``results/``; they do not exclude
reliably across network filesystems or Docker Desktop's macOS file sharing.
``run`` and ``replay`` run in the foreground only: they stop, killing the
running kernel and releasing the lock, once the process that started them
exits, including a shell that put them in the background.

公开定义位置：
```text
43: FunctionDef build_parser
92: FunctionDef _reference
103: FunctionDef _api
129: FunctionDef _root
138: FunctionDef main
```

## `skills/_sdk/notebook/_acceptance.py`

Accepting a module, and opening an accepted module for revision.

``accept`` records the user's acceptance and freezes the module once four
conditions hold: the module README exists; the module is replayed (latest
replay ok, covering the current step files, validate step included, no
stale step); the report exists and carries the disclaimer; and either an
approving review newer than the replay, or the user's own words asking to
skip the review. ``revise`` snapshots the accepted results into
``baseline/<date>_pre_revision/`` (files up to 16 MiB copied, larger ones
by sha256) and unfreezes the module.

公开定义位置：
```text
38: FunctionDef _module
53: FunctionDef _squash
57: FunctionDef _replay_problem
74: FunctionDef _review_problem
125: FunctionDef accept
181: FunctionDef _baseline_dir
192: FunctionDef snapshot
222: FunctionDef revise
```

## `skills/_sdk/notebook/_executor.py`

The step runner's ``new``, ``run`` and ``status``.

``run`` takes the module lock, checks the interpreter against the
manifest, and runs each stale step in a fresh kernel (validate step last),
re-checking later steps after each one because an earlier step may have
rewritten what they read. Every run gets a ledger file; afterwards the
manifest is rebuilt from the ledgers and the module notebook re-stitched.

Output goes to stdout. When its reader goes away (``run ... | head``), the
rest of the output is dropped and the run carries on to the end.

公开定义位置：
```text
41: FunctionDef _print
49: FunctionDef _drop_stdout
60: ClassDef StepResult
67: FunctionDef _short_python
71: FunctionDef _call_text
77: FunctionDef _join
82: FunctionDef format_step
140: FunctionDef _step_env
151: FunctionDef _record_r_io
169: FunctionDef _write_notebook
178: FunctionDef execute_step
256: FunctionDef stitch
319: FunctionDef _check_runnable
330: FunctionDef _frozen
338: FunctionDef _kernel_check
348: FunctionDef _locked
355: FunctionDef _rscript_check
364: FunctionDef _rscript_warning
372: FunctionDef run_targets
401: FunctionDef run_module
421: FunctionDef _interpreter_warning
425: FunctionDef _run_locked
477: FunctionDef status
522: FunctionDef new_module
558: FunctionDef _output_files
572: FunctionDef replay
612: FunctionDef _replay_locked
```

## `omicsclaw/entry/assembly.py`

The composition root: five layers, wired once, by one function.

Plan 0031 task A. Every layer below this one names a hole in its own
docstring — ``engine/loop.py:34-38`` "the conversation arrives
assembled", ``context/__init__.py`` "``OMICSCLAW.md``, the model table and the
summarizing model are all the composition root's",
``tools/builtin/__init__.py`` "mounting a foundation tool is always a
decision about *which* workspace, and that decision belongs to whoever is
assembling the registry". They are five descriptions of this module.

:func:`build_app` is the only place that knows how the pieces fit. That
is the property worth defending: a second site that builds its own
registry or its own budget is a second deployment nobody configured.

**Three seams the assembly owns, each of which is silent when broken.**

*The two reserves have no defaults.*
:class:`~omicsclaw.context.ContextBudget` requires
``reserve_output_tokens`` and ``reserve_tool_tokens`` as positional
fields, and it requires them because nothing below can compute either:
the first comes from the model table in ``omicsclaw.provider``, the
second from the registry that was just built. See :func:`build_budget`.

*One workspace, six tools.* :func:`foundation_tools` constructs the
:class:`~omicsclaw.tools._workspace.Workspace` once and hands the same
object to every tool that takes one. Two workspaces would not raise —
they would give ``write_file`` and ``read_file`` two different sandbox
roots, and the agent would write a file it then cannot find.

*The registry goes to the engine unwrapped.*
:class:`~omicsclaw.tools.ToolRegistry` satisfies
:class:`~omicsclaw.engine.DeadlineAwareExecutor` and
:class:`~omicsclaw.engine.ConcurrencyAwareExecutor`, both
``runtime_checkable``, and the engine decides by :func:`isinstance`
whether to hand down a
:class:`~omicsclaw.tools.TimeoutPause`. Wrapping the registry for logging
or filtering without forwarding ``use_timeout_pause`` puts defect R3 back
in: a human's thinking time starts counting against the tool timeout
again, on the first layer that ever binds an approval channel. See
:func:`build_registry`.

**This layer is allowed to log, and is the first one that is.** The
logger is ``omicsclaw.entry``; nothing here configures it, so an
unconfigured process gets the root logger's default and warnings and
above are what reach a terminal. Two rules that are not style:
**no tool argument and no tool output is ever logged** — a ``bash``
command line, a ``write_file`` body and a ``web_fetch`` query string can
each carry a subject identifier, and :data:`SAFETY_RULES` rule 1 is
a rule a log statement can break — and no third-party logging library is
introduced.

**How the session registry gets here.** :func:`build_app` still returns
an app whose :attr:`AgentApp.sessions` is ``None``, and it always will:
the registry needs a finished app and the app names the registry, so
:func:`~omicsclaw.entry.session.attach_sessions` ties that knot in one
line with :func:`dataclasses.replace` rather than either object being
built twice. A deployment is therefore
``attach_sessions(await open_app(...))``, and an app that skipped that
call has no queues — :meth:`AgentApp.aclose` has nothing to drain and
``/chat/stream`` raises.

**Skills.** This module imports :mod:`omicsclaw.skills`:
:func:`foundation_tools` appends :func:`~omicsclaw.skills.use_skill_tool`
and :func:`default_sections` adds a skill catalogue section.
``AppConfig.skills_index`` is the switch, and ``off`` drops both. The
prompt is the contract, safety rules and tool guidance, then the
optional planning, execution sandbox and skills sections, the
environment, and the optional long-term memory block;
:func:`default_sections` documents when each optional one appears.

公开定义位置：
```text
262: FunctionDef build_skill_index
287: FunctionDef foundation_tools
366: FunctionDef build_permission_gate
388: FunctionDef build_hooks
427: FunctionDef build_registry
455: FunctionDef _environment_source
489: FunctionDef _front_matter
504: FunctionDef _skills_section
521: FunctionDef default_sections
617: FunctionDef build_prompt
636: FunctionDef build_budget
669: ClassDef _ProviderSummarizer
721: FunctionDef build_summarizer
741: ClassDef AgentApp
1040: FunctionDef build_app
1287: FunctionDef _apply_bash_policy
1314: FunctionDef _is_bash
1334: FunctionDef _sandbox_state
1340: AsyncFunctionDef open_app
1431: AsyncFunctionDef _swept
1450: FunctionDef _log_mcp_outcome
```

## `omicsclaw/entry/session.py`

``omicsclaw/entry`` — conversations, their queues, and one Task each.

Plan 0031 Q4 and Q6. The engine below deliberately has no ``Session``
("a conversation goes in, a conversation comes out"; plan 0027 declined
to build half of one), so a conversation's history has to live
somewhere, and this is the somewhere. The reference harness keeps it in
SQLite from line one (``main.go:232-245``); this module keeps it behind
a :class:`SessionStore` Protocol with one in-memory implementation,
because **building half a persistence layer is a mistake this rebuild
has already paid for once**.

**Two things live on a session, not one.** ``history`` is the obvious
one. :attr:`Session.compaction` is the one that goes missing:
``context/compaction.py:214-220`` says outright that
:class:`~omicsclaw.context.CompactionState` is "carried between turns by
the caller; step 6 decides where it lives", and a deployment that drops
it re-summarizes from ``FIRST_TEMPLATE`` every single time — paying for
a summary it already has and losing the detail the last one chose to
keep.

**The registry is three guarantees and nothing else** (Q6):

1. *One Task per exchange.* ``contextvars`` isolate per Task, so two
   concurrent sessions are isolated only if each runs in its own —
   otherwise "the second ``set`` wins and one user's tool call is offered
   to the other user's approval prompt" (``tools/context.py`` Q4.5).
2. *Serial within a session, concurrent across sessions.* A second
   message to one conversation waits; a message to another does not.
3. *Queueing is a semantic, not a lock* (Q7). :meth:`SessionRegistry.
   submit` returns a handle **immediately**, publishes how many
   exchanges are ahead of it, refuses outright past
   ``max_queued_per_session``, and a queued exchange can be cancelled
   before it ever starts. A ``submit`` that awaited a lock would give a
   user who typed twice no handle, no feedback and no way out.

**Persistence happens here, not in the exchange's Task** (trap 3b).
Cancelling a Task injects :exc:`asyncio.CancelledError` at its next
``await``, so ``finally: await store.save(...)`` does not finish — and
an in-memory store whose ``save`` never actually suspends hides that
until the day it becomes SQLite. So the registry, which is the side that
*asks* for cancellation, reaps the Task first and saves afterwards. For
the same reason :meth:`InMemorySessionStore.save` suspends once on
purpose.

**What is not here.** No authentication, no rate limiting, no durable
log, no cross-process anything. A sender allowlist is
:class:`~omicsclaw.entry.ingress.SenderPolicy`'s, one layer out, and it
is out there because a registry that decided who may talk to it would be
a registry every surface had to configure identically.

公开定义位置：
```text
110: FunctionDef new_turn_id
123: ClassDef Session
168: ClassDef SessionStore
194: ClassDef InMemorySessionStore
242: ClassDef SubmissionRefused
252: ClassDef QueueFull
261: ClassDef RegistryClosed
266: ClassDef _Lane
300: ClassDef SessionRegistry
883: FunctionDef _reap
895: FunctionDef attach_sessions
```

## `omicsclaw/entry/review.py`

Archive a reviewer reply against the project files it was asked to read.

Receipts detect stale inputs and changed text. They are workspace files,
not signatures or proof against a writer who can replace both files.

公开定义位置：
```text
18: ClassDef ReviewArchive
```

## `omicsclaw/entry/desktop/server.py`

The Desktop HTTP routes, and the plain functions behind them.

Routes (see :data:`~omicsclaw.entry.desktop.wire_contract.SERVED_PATHS`):

* ``POST /chat/stream`` — admit one chat message and stream its frames;
* ``POST /chat/permission`` — answer one ``permission_request``;
* ``POST /chat/abort`` — cancel the exchange a stream request started;
* ``POST /chat/session-permission-profile`` — put a session in the
  ``default`` or ``full_access`` permission profile;
* ``GET /workspace`` — the workspace this process serves;
* ``PUT /workspace`` — accepted only for that same workspace, since a
  process serves one workspace and changing it means a restart;
* ``GET /env/doctor`` — a checklist of what this backend was assembled
  with (:mod:`~omicsclaw.entry.desktop.doctor`);
* ``GET``/``HEAD /health`` — process facts and the wire contract;
* ``GET /skills`` and ``GET /skills/{domain}/{name}`` — the skill index
  (:mod:`~omicsclaw.entry.desktop.catalog`);
* ``GET /mcp/servers`` — the MCP servers and their connection state
  (:mod:`~omicsclaw.entry.desktop.catalog`);
* ``GET``/``PUT /providers`` and ``POST /providers/test`` — the model
  backend the deployment's ``.env`` configures
  (:mod:`~omicsclaw.entry.desktop.providers`);
* ``POST /chat/title`` — a title for a session's first message
  (:mod:`~omicsclaw.entry.desktop.title`);
* ``GET /files/tree`` and ``GET /files/serve`` — read-only views of the
  workspace's files (:mod:`~omicsclaw.entry.desktop.files`).

A ``/chat/stream`` message whose content is exactly ``/compact`` compacts
the session's history instead of being sent to the model. A
``/chat/stream`` body with ``resume: true`` reattaches to the exchange
its ``source_request_id`` started and never starts one.

Every write route requires ``Content-Type: application/json`` and answers
415 ``unsupported_media_type`` otherwise, including when the header is
missing. A cross-origin page can send a ``text/plain`` request to a
loopback port without a CORS preflight, but not a JSON one, and this
server grants no CORS. A refused request is answered as
``{"detail": <code>}`` with the code's status.

Everything above the HTTP boundary — :func:`open_chat_stream`,
:func:`health_payload`, :func:`workspace_payload`,
:func:`change_workspace`, the functions in
:mod:`~omicsclaw.entry.desktop.interactions` and
:func:`~omicsclaw.entry.desktop.doctor.doctor_report` — is plain ``async def`` or
``def`` over plain dictionaries. :func:`create_desktop_app` is the adapter
that puts them behind FastAPI, and it imports FastAPI **inside the
function body**, so ``import omicsclaw.entry.desktop`` succeeds where no
web framework is installed.

This module does **not** enable ``from __future__ import annotations``.
The routes are defined inside :func:`create_desktop_app`, where
``Request`` is a local name; with postponed evaluation FastAPI would see
the string ``"Request"``, fail to resolve it in this module's globals and
treat the parameter as a required query parameter, so every route would
answer 422.

公开定义位置：
```text
153: ClassDef ChatStream
165: AsyncFunctionDef open_chat_stream
276: FunctionDef _retained_exchange
289: FunctionDef _observed_body
323: FunctionDef _observe
330: AsyncFunctionDef _compaction
345: FunctionDef health_payload
388: FunctionDef unauthenticated_health_payload
406: FunctionDef workspace_payload
414: FunctionDef change_workspace
436: FunctionDef _same_workspace
451: FunctionDef is_json_media_type
463: FunctionDef create_desktop_app
777: FunctionDef _after_seq
```

## `omicsclaw/entry/desktop/interactions.py`

Process-local state behind the Desktop return channels, and their logic.

:class:`DesktopInteractions` is created once per
:func:`~omicsclaw.entry.desktop.server.create_desktop_app` and holds three
bounded maps, all in memory and none persisted:

* the approval requests a ``/chat/stream`` body has shown as a
  ``permission_request`` card, keyed by ``request_id``, so that
  ``/chat/permission`` knows the session and the tool a decision is about;
* the tools a session has allowed for the rest of its life in this
  process ("allow for this session"), keyed by ``session_id``;
* the sessions in the ``full_access`` permission profile;
* ``(session_id, source_request_id) → turn_id``, so that ``/chat/abort``
  can find the exchange a client is streaming, and a resuming
  ``/chat/stream`` the exchange to reattach to;
* ``turn_id → TurnUsage``, what the model calls of each exchange cost,
  gathered across every stream that observed it.

:func:`answer_permission`, :func:`abort_chat` and
:func:`change_permission_profile` are the logic of ``POST
/chat/permission``, ``POST /chat/abort`` and ``POST
/chat/session-permission-profile`` over an already parsed JSON document,
with no web framework in their signatures.

``full_access`` allows, without a card, every request that is not marked
``ask_every_time`` — the same line ``auto-approve`` draws — so a
dangerous command, an explicit ``ask`` rule and a change to a protected
file are still asked about. It lives here, per session and in memory,
rather than in the permission gate's mode, which is one per process and
shared by every session.

A session grant and ``full_access`` are applied by an observer of the
stream: an approval request raised while no ``/chat/stream`` body is
reading the exchange is answered only once somebody observes it again.

公开定义位置：
```text
97: ClassDef TurnUsage
177: ClassDef PendingApproval
185: ClassDef DesktopInteractions
452: AsyncFunctionDef answer_permission
529: FunctionDef _remember
542: FunctionDef _permission_decision
572: AsyncFunctionDef change_permission_profile
614: AsyncFunctionDef abort_chat
659: FunctionDef _retained
```

## `omicsclaw/engine/loop.py`

``omicsclaw/engine`` — the ReAct loop itself.

Plan 0027 §2 and §6, the third step of the staged rebuild. Step 1 gave
the system a vocabulary and step 2 gave it a way to speak; this is the
loop that keeps

    Message → ToolCall → ToolResult → Message

moving until the model stops asking to act::

    engine = AgentEngine(provider, tools, EngineConfig(max_turns=50))

    result = await engine.run(messages)               # blocking
    async for event in engine.run_stream(messages):   # streaming
        ...

Plan 0027 §12.4.4 adds a second pair of entry points over that same
kernel, for the caller who has a system prompt and a history rather than
a finished list of messages::

    engine = AgentEngine(provider, tools, prompt=assembler)

    result = await engine.exchange("分析这份 Visium 数据", conversation=chat)
    async for event in engine.exchange_stream("…", conversation=chat):
        ...

``exchange`` and ``exchange_stream`` are **shells**, not a second loop:
they render, splice, delegate to ``run`` / ``run_stream``, and hand the
finished trajectory back to the conversation. Everything about turns,
tools and stopping stays in the one kernel below.

**One kernel, two mouths.** :meth:`AgentEngine.run` and
:meth:`AgentEngine.run_stream` share a single copy of the turn / tool /
Observation logic, because a second copy is how a blocking path and a
streaming path start disagreeing about when a run is finished — and the
disagreement surfaces as a bug report about one of them. What differs is
one injected turn strategy: ``run`` calls ``generate`` and
``run_stream`` calls ``generate_stream``, so a blocking caller never pays
for streaming and never reassembles a message out of deltas it did not
want.

Python cannot spell that seam the way the reference harness does. PEP 525
forbids a non-empty ``return`` in an async generator, so a strategy
cannot both yield events and hand back a :class:`Completion`; it fills a
mutable outcome holder instead — the shape plan 0027 §6 blesses, and the
one :func:`~omicsclaw.engine.executor.execute_tool_calls` already uses
for its results.

**What this layer is not.** No prompt assembly, no session persistence,
no tool implementations. Nothing here renders a prompt or writes a
conversation down: a :class:`~omicsclaw.engine.prompt.PromptSource` is
*asked* for a render it made elsewhere, and a
:class:`~omicsclaw.engine.conversation.Conversation` is *handed* the
trajectory to do as it likes with. Compaction is not
decided here either: a :class:`~omicsclaw.engine.compactor.
HistoryCompactor` handed to :meth:`AgentEngine.run` is consulted before
every model call and may rewrite what is sent, or the history itself.
Nor is what a call is *reminded* of: a
:class:`~omicsclaw.engine.augmentor.TurnAugmentor` is consulted after the
compactor and may append to that call — and only to that call. The loop
knows neither what a plan is nor that one exists.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.provider`` and the
standard library. No logging, no I/O, no vendor SDK.

公开定义位置：
```text
104: ClassDef _TurnOutcome
155: ClassDef AgentEngine
645: FunctionDef _opening
671: AsyncFunctionDef _settle
700: FunctionDef _stop_reason_for
724: FunctionDef _answer_every_call
```

## `omicsclaw/context/budget.py`

``omicsclaw/context`` — how much room is left, and how worried to be.

Plan 0030 task A, decisions Q9, Q9-a and Q9-b. One question this module
answers before the model is called rather than after: *does this fit, and
if not, how badly?*

**The denominator is a subtraction, not a percentage.** The reference
harness compares against a flat fraction of the context window — 80% in
``internal/memory/compaction.go:87-92``, 60/70/80/95% of the window in
``progressive_compactor.go:124-134``. It uses a fraction because it has
no better number to hand. We do::

    usable = context_tokens
           - reserve_output_tokens      # ModelLimits.output_tokens
           - reserve_tool_tokens        # estimate_tool_tokens(tools)
           - context_tokens * safety_ratio

That this is the harness's own intent rather than a local invention is
something the harness says out loud. Its real benchmark does not use the
80% default: ``cmd/swebench/runner.go:293-299`` sets the budget to
**55%** of the window, and the comment on ``:294`` spends the missing
45% on "tool definitions (~25K) + output reserve + chars/4 estimation
error" — the three subtrahends above, folded into one fraction because
Go had nowhere to put them separately.

**Neither reserve has a default, and that is the point.** A default of
zero is the convenient value, not the guarded one: a caller who forgets
``reserve_tool_tokens`` gets a budget 20-30K tokens larger than the
space that actually exists (``token.go:32``), and nothing in this
package can notice, because :func:`~omicsclaw.context.compaction.
plan_compaction` is never shown the tools. Both numbers are *known* to
the caller — one from ``get_model_limits(model)``, one from
:func:`~omicsclaw.context.tokens.estimate_tool_tokens` — so demanding
them costs a line and buys the whole argument above. Forgetting one is a
``TypeError`` at construction.

A warning that travels with ``reserve_output_tokens``: an output ceiling
of 8192 out of ``omicsclaw/provider/_model_limits.py`` means *not
known*, not *known to be 8192*. Where the real ceiling is higher, this
budget is optimistic by the difference.

**"No budget configured" is not a budget of zero.** ``context_tokens
<= 0`` raises rather than modelling anything; a caller who does not know
the window holds ``ContextBudget | None`` and does not construct one.
The opposite case — a window so small, or reserves so large, that
``usable_tokens`` is zero or negative — is real and is
:attr:`Pressure.EMERGENCY`, because not one message of history fits and
"do nothing" means posting the whole conversation into a refusal. The
harness's ``ContextWindow <= 0 → TierNone`` (``progressive_compactor.go:
211-213``) is deliberately **not** ported: after the denominator
changed, that line answers a different question than it used to.

**The four ratios are the harness's, uncalibrated here.** 0.60 / 0.70 /
0.80 / 0.95 come from ``progressive_compactor.go:124-134``. They have
never been measured against this repository's traffic, and the number
they are compared to now has a different denominator than the one they
were chosen for (plan 0030 §11.A-7). Saying so is worth more than
inventing four fresh numbers with the same amount of evidence behind
them.

**Where ``local_budget_status`` went.** The replaced layer had a second
grading function, because grading against a 1M-token window says "fine"
forever and only a local budget says anything useful. ``usable_tokens``
*is* that local budget, so the capability is not missing — it is the
only mode this class has.

公开定义位置：
```text
88: ClassDef Pressure
136: FunctionDef at_least
142: ClassDef ContextBudget
279: ClassDef BudgetReport
303: FunctionDef measure
```

## `omicsclaw/context/progressive.py`

Progressive compaction across the model calls of one run.

:class:`ProgressiveCompactor` is consulted before every model call. It
measures the conversation against the budget, and from the configured
tier upwards compacts it with :func:`~omicsclaw.context.compaction.
compact` — offloading, then summarizing half or all of the head, then
emergency truncation — and decides whether the result replaces the
run's history (:func:`should_write_back`).

It keeps the incremental summary state between calls, so one instance
belongs to one run of one conversation. Its :meth:`~ProgressiveCompactor.
compact` method satisfies the engine's ``HistoryCompactor`` protocol
structurally.

公开定义位置：
```text
41: FunctionDef should_write_back
59: FunctionDef _did_something
68: ClassDef ProgressiveCompactor
```

## `omicsclaw/tools/registry.py`

The hub: registration, exposure, dispatch — and nothing else.

Plan 0028 §2. Three jobs in one place, so the loop can use tools while
knowing none of them:

    registry.register(tool)                 # mount
    defs = registry.available_tools()       # expose  → provider
    result = await registry.execute(call)   # dispatch → Observation

:class:`ToolRegistry` satisfies
:class:`omicsclaw.engine.executor.ToolExecutor` **structurally** — same
two methods, same signatures — without importing :mod:`omicsclaw.engine`.
That is the whole point of the seam being a Protocol: the dependency runs
one way, from the engine's test suite to here, and never back.

Two further methods satisfy that layer's two **optional** seams the same
way. :meth:`ToolRegistry.is_concurrency_safe` answers
``ConcurrencyAwareExecutor``, which is what finally gives
:attr:`~omicsclaw.tools.base.ToolPolicy.concurrency_safe` a consumer: a
tool that declares itself unsafe is scheduled alone instead of beside a
sibling writing the same file. :meth:`ToolRegistry.use_timeout_pause`
answers ``DeadlineAwareExecutor`` and carries the engine's per-call
timeout pause down to :func:`~omicsclaw.tools.context.require_approval`,
so a human's thinking time stops being reported as a tool running long.
Both report or relay; neither schedules and neither decides.

**What is absent is the design.** There is no ``request``, no
``surface``, no ``stage`` anywhere on this class. Deciding *which* tools
a particular caller may see is the assembly layer's business (plan 0028
§5); a registry that knew about requests would be the thing this rebuild
is walking away from. What is offered instead is the full set, in a
stable order, plus :meth:`ToolRegistry.policy_for` so a filter can be
written elsewhere.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, and the standard library.

公开定义位置：
```text
55: ClassDef ToolRegistrationError
66: ClassDef ToolAlreadyRegistered
78: ClassDef ToolNameMismatch
90: ClassDef ToolRegistry
416: FunctionDef _timing
425: FunctionDef _failed
```

## `tests/sdk/test_boundary.py`

Guards for the code boundary between ``skills/**`` and ``omicsclaw/**``.

Plan 0062 §3.8. Skills and the framework meet only through file contracts
(``SKILL.md``, the result.json schema in ``skills/_sdk``);
the mechanical helpers every skill needs live in ``skills/_sdk/``. These
tests pin that down statically, with known-item tables that must match the
violations *exactly* — a violation that appears or disappears both turn the
test red, so the tables cannot rot silently.

Rules and the plan's names for them:

* B1 — nothing imports ``omicsclaw.core``; ``omicsclaw/core/`` and
  ``omicsclaw/r_scripts/`` hold no code.
* B2 — every ``omicsclaw.*`` / ``skills.*`` module a skill imports exists.
* B3 — skill code does not import ``omicsclaw`` at all.
* B4 — the framework does not import ``skills.*``.
* B5 — ``skills/_sdk`` imports nothing above itself.
* B6 — a domain ``_lib`` imports only its own ``_lib`` and ``skills._sdk``.
* B7 — no ``SKILL.md`` under ``skills/_sdk`` (the loader would index it).
* B8 — skill code carries no framework control-plane credential names.
* B9 — no string constant in skill code spells an ``omicsclaw.*`` module.
* B10 — every domain ``_lib`` module imports with ``omicsclaw`` made unimportable.

``skills/**/tests/`` are outside B3 (plan 0062 Q5); B1 still covers them.

公开定义位置：
```text
56: FunctionDef _skill_code
60: FunctionDef test_core_is_gone
75: FunctionDef test_nothing_imports_omicsclaw_core
86: FunctionDef test_imported_modules_exist_on_disk
97: FunctionDef test_sdk_imports_nothing_above_it
109: FunctionDef test_no_skill_md_under_sdk
114: FunctionDef test_skill_code_carries_no_control_credentials
128: FunctionDef test_no_omicsclaw_module_strings
141: FunctionDef test_framework_imports_no_skills
152: FunctionDef test_skill_code_imports_no_omicsclaw
164: FunctionDef _lib_files
171: FunctionDef test_domain_lib_imports_only_itself_and_sdk
186: FunctionDef test_lib_modules_import_without_omicsclaw
```

## `tests/sdk/test_public_surface.py`

The frozen public surface of ``skills/_sdk`` (plan 0062 §3.10, case 18).

``_sdk`` is the read-only API that skill code — hand-written or produced by
a future self-improvement loop — may use, so what it exposes is a review
gate: adding or removing a public name means editing ``PUBLIC_SURFACE``.
Only names that skill or template code actually imports are frozen, plus
two machine-read contracts (``RESULT_SCHEMA``, ``DEPENDENCIES``). Checked
over non-test code in ``skills/**`` and ``templates/**``: every name taken
from an ``_sdk`` module is in that module's ``__all__`` and not private,
and every frozen name (contracts aside) has at least one user.

公开定义位置：
```text
71: FunctionDef _sdk_modules
78: FunctionDef _uses
105: FunctionDef test_every_module_is_frozen
109: FunctionDef test_all_matches_the_frozen_surface
114: FunctionDef test_skill_code_takes_only_public_names
123: FunctionDef test_every_frozen_name_has_a_user
134: FunctionDef test_the_shared_r_scripts_are_frozen
```

## `omicsclaw/schema/__init__.py`

``omicsclaw.schema`` — the system's unified blood.

Per ADR 0077. The single vocabulary in which the Main Loop, the model
adapter, the tool layer, and the memory layer talk to each other.

    from omicsclaw.schema import Message, Role, ToolCall, ToolResult

Defined before the Main Loop because the loop body *is* the movement of
these types — ``Message → ToolCall → ToolResult → Message``. There is no
loop to write until they exist.

**Leaf package.** Standard library only — no ``omicsclaw`` imports, no
third-party dependency, no I/O, no logging. Every component depends on it
and it depends on none, which is what keeps the dependency graph acyclic.

**Vendor-neutral.** Nothing here mirrors any vendor's payload shape.
Translating OpenAI / Anthropic / DeepSeek / Ollama formats into and out
of these types belongs to the model adapter layer. A vendor field name
appearing in this package is a bug.

公开定义位置：
```text

```

## `omicsclaw/provider/__init__.py`

``omicsclaw.provider`` — the simultaneous interpreter.

Plan 0026, step 2 of the staged rebuild. One interface the Main Loop
programs against, so switching or adding an LLM backend never touches
loop logic::

    from omicsclaw.provider import LLMProvider, provider_from_env

    llm = provider_from_env()          # the backend the environment implies
    completion = await llm.generate(messages, tools)

:mod:`omicsclaw.schema` types go in, :mod:`omicsclaw.schema` types come
out, and every vendor-shaped field name lives and dies inside one adapter
module. See :mod:`omicsclaw.provider.base` for why the interface takes
only the conversation and the tools.

Singular ``provider``, alongside the existing plural
``omicsclaw.providers``, which stays live and untouched until the closing
migration task folds it in (plan 0026 §10). This package imports
``omicsclaw.schema`` and the standard library and nothing else, so the
two can coexist without a cycle.

The adapter classes are exported alongside the contract, and importing
them still requires neither vendor SDK: each loads its package inside its
own client factory, on the first call that needs a socket. A caller that
only wants "the provider my environment implies" should reach for
:func:`~omicsclaw.provider.factory.provider_from_env` and never name an
adapter class at all — that is the coupling this package exists to absorb.

公开定义位置：
```text

```

## `omicsclaw/engine/__init__.py`

``omicsclaw.engine`` — the ReAct main loop.

Plan 0027, step 3 of the staged rebuild. Steps 1 and 2 delivered the
vocabulary (:mod:`omicsclaw.schema`) and the interpreter that speaks it
(:mod:`omicsclaw.provider`); this package is the loop that moves those
types until the model has nothing left to ask for::

    from omicsclaw.engine import AgentEngine, EngineConfig

    engine = AgentEngine(provider, tools, EngineConfig(max_turns=50))
    result = await engine.run(messages)

The loop owns turn orchestration, tool scheduling, Observation injection
and the decision to stop — and owns nothing else. Prompt assembly,
session persistence and the tools themselves live elsewhere; compaction
reaches the loop through :class:`~omicsclaw.engine.compactor.
HistoryCompactor`, an optional per-run argument, and
:class:`~omicsclaw.engine.augmentor.TurnAugmentor` is its sibling — also
optional, also per run, consulted after it, and able only to *append* to
the call being made. :class:`~omicsclaw.engine.executor.ToolExecutor`
is exported as the seam those tools will satisfy **structurally**,
without importing this package, exactly as an adapter satisfies
``LLMProvider``.

:class:`~omicsclaw.engine.prompt.PromptSource` and
:class:`~omicsclaw.engine.conversation.Conversation` are the same idea
applied to the two ends of one exchange: the loop asks a renderer for a
system prompt and hands a trajectory to whoever keeps the conversation,
and neither of them has to know this package exists.
:class:`~omicsclaw.engine.prompt.RenderedPrompt` joins them because a
render is passed through — to
:attr:`~omicsclaw.engine.types.RunResult.prompt` — rather than reduced to
a string on the way in.

``execute_tool_calls`` and ``observations`` are exported beside it
because scheduling policy — concurrency, per-tool timeouts, the ordering
of Observations — is the loop's business rather than any tool's, and a
caller assembling its own turn should reach for the same scheduler the
engine uses instead of writing a second one.

:class:`~omicsclaw.engine.executor.ConcurrencyAwareExecutor` and
:class:`~omicsclaw.engine.executor.DeadlineAwareExecutor` are the two
**optional** halves of that seam: the first lets an executor mark a tool
as a barrier so it runs alone, the second lets it carry the per-call
timeout pause down to a tool waiting on a human. An executor that
implements neither is driven exactly as the two-method seam always was.

This package imports ``omicsclaw.schema``, ``omicsclaw.provider`` and the
standard library, and nothing else. No vendor SDK, no logging, no I/O:
importing the loop never requires an optional extra to be installed.

公开定义位置：
```text

```

## `omicsclaw/tools/__init__.py`

``omicsclaw.tools`` — the hands the loop acts through.

Plan 0028, step 4 of the staged rebuild. Steps 1–3 delivered the
vocabulary (:mod:`omicsclaw.schema`), the interpreter that speaks it
(:mod:`omicsclaw.provider`) and the loop that moves it
(:mod:`omicsclaw.engine`). This package is what the loop dispatches to::

    from omicsclaw.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(InspectDataTool(workspace))
    defs = registry.available_tools()          # → provider
    result = await registry.execute(call)      # → Observation

:class:`~omicsclaw.tools.registry.ToolRegistry` satisfies
:class:`omicsclaw.engine.executor.ToolExecutor` **structurally**, without
importing :mod:`omicsclaw.engine`: the engine declared the smallest
Protocol that lets it act, and this package matches its shape. Nothing
here is wired into production — the existing tool layer under
``omicsclaw.runtime.tools`` still serves every surface, untouched, and
migrating its tools onto this one is a later step.

Two adapters mean most tools need no class of their own:
:class:`~omicsclaw.tools.function_tool.FunctionTool` wraps a plain
callable and turns a model's bad arguments into something it can correct,
and :class:`~omicsclaw.tools.mcp_tool.MCPTool` makes a tool that lives in
another process indistinguishable from one that lives here.

:mod:`omicsclaw.tools.context` is the third piece and the least obvious:
``execute(arguments)`` takes one argument on purpose, so a tool that has
to ask a human for permission, or say how far along it is, reaches its
caller through :mod:`contextvars` rather than through a widened seam. The
conventions — who binds, who reads, and what an unbound channel means —
are written down there because the mechanism works by a property of the
engine that nobody would otherwise know to preserve.

:mod:`omicsclaw.tools.builtin` holds the **foundation** tools:
``read_tool``, :class:`~omicsclaw.tools.builtin.write.WriteTool`,
:class:`~omicsclaw.tools.builtin.edit.EditTool` and
:class:`~omicsclaw.tools.builtin.bash.BashTool` (plan 0029 and its §12
follow-ups), plus
:class:`~omicsclaw.tools.builtin.web_fetch.WebFetchTool` and
:class:`~omicsclaw.tools.builtin.web_search.WebSearchTool` — the hands
the agent works with, not a starter set. The fifty tools under
``omicsclaw.runtime.tools`` are migrated later, one at a time.

The three file tools stand on this package's private helpers —
``_workspace.py``, the sandbox boundary every filesystem tool shares, and
``_pathlock.py``, the per-path lock that stops two writes from losing
each other wherever the engine's own barrier does not reach — overlapping
turns above all. The two web tools stand on ``_websafety.py``,
which is the same idea applied to a different boundary: an SSRF gate
every request passes, plus the pinned transport that is the only thing
allowed to open a socket. ``bash`` stands on none of them, because it has
no path to check and no destination to resolve; what bounds it is the
approval gate its policy declares. None of the six is wired into
production.

Plan 0028's three reference tools used to sit beside them, as evidence
that the pieces above hold up for a tool that really does something.
They were removed once the foundation tools carried every property they
were built to prove — see :mod:`omicsclaw.tools.builtin` for which
property moved where.

**A leaf layer.** Inside the ``omicsclaw`` namespace this package imports
``omicsclaw.schema`` and nothing else — not ``provider``, not ``engine``,
and not ``omicsclaw.runtime.tools``, which is the tempting one precisely
because it is also called ``tools``. Nothing about the package layout
prevents that import; ``tests/tools/test_tools_is_a_leaf_layer.py``
does, by walking the AST of every module here.

公开定义位置：
```text

```

## `omicsclaw/context/__init__.py`

``omicsclaw.context`` — what the model is shown, and what it costs.

Plan 0030, step 5 of the staged rebuild. The three layers before this
one can run a ReAct turn but cannot say where the conversation came
from: ``AgentEngine.run(messages)`` takes an assembled conversation and
``omicsclaw/engine/loop.py:34-38`` says so outright — "the conversation
arrives assembled". **This is the layer that assembles it**, decides
whether it still fits, and shrinks it when it does not::

    from omicsclaw.context import (
        ContextBudget, PromptAssembler, Section, assemble, compact, static,
    )

    prompt = (
        PromptAssembler()
        .with_section(Section("contract", "", text_from_file("OMICSCLAW.md")))
        .with_section(Section("project", "## Project", text_from_file("AGENTS.md")))
        .with_section(Section("skills", "## Skills", render_skill_index))
        .render()
    )
    conversation = assemble(prompt, history, "分析这份 Visium 数据")

    limits = get_model_limits(model)
    budget = ContextBudget(
        context_tokens=limits.context_tokens,
        reserve_output_tokens=limits.output_tokens,
        reserve_tool_tokens=estimate_tool_tokens(tools),
    )
    smaller, record, state = await compact(
        conversation, budget, summarizer=summarize_with_haiku, pinned=1
    )
    # Next turn: ``smaller[1:]`` — ``pinned=1`` kept the system message
    # inside the compacted history, and ``assemble`` always adds a fresh
    # one. See :func:`~omicsclaw.context.prompt.assemble`.
    conversation = assemble(prompt, smaller[1:], "下一步")

Note where the knowledge lives in that sketch: ``OMICSCLAW.md``, the skill
index, the model table and the summarizing model are all the composition
root's, reaching this package through a callable or a plain ``int``.

**Leaf package.** Inside ``omicsclaw`` it imports ``omicsclaw.schema``
and nothing else; outside it, only the standard library. Not
``provider`` (``get_model_limits`` would be one table read and one
import too many), not ``engine``, not ``tools``, not ``runtime``. Every
piece of outside knowledge arrives through a Protocol or a callable:
:class:`~omicsclaw.context.tokens.TokenCounter` for an exact tokenizer,
:data:`~omicsclaw.context.sections.SectionSource` for anything textual,
:class:`~omicsclaw.context.summary.Summarizer` for the model that
writes summaries. ``tests/context/test_context_is_a_leaf_layer.py``
enforces it, by running the layer in a subprocess and looking at what
actually got imported rather than at what was spelled.

**Compaction inside a run.** :class:`ProgressiveCompactor` grades the
conversation before every model call and compacts it by tier; it
satisfies the engine's ``HistoryCompactor`` protocol structurally, so an
engine run can be handed one without either package importing the
other::

    compactor = ProgressiveCompactor(
        budget, summarizer=summarizer, offloader=Offloader(store), pinned=1
    )
    result = await engine.run(conversation, compactor=compactor)
    carried = compactor.state

**Not here.** No session, no persistence and no filesystem: offloaded
text goes to an :class:`OffloadStore` and records leave through a
callback, both supplied by the caller.

公开定义位置：
```text

```

## `omicsclaw/skills/__init__.py`

``omicsclaw.skills`` — the Skill loader.

Skills are the third source the prompt composer draws on, after the
engine's own minimal core and the workspace's ``AGENTS.md``. They are
disclosed progressively: the system prompt carries one line per skill,
and the ``use_skill`` tool loads a body when the model needs one. Over
this repository's 96 skills that is roughly 8.5k tokens of index against
125k tokens of instructions.

Wiring one up takes three lines at a composition root::

    from omicsclaw.context import Section
    from omicsclaw.skills import load_skills, use_skill_tool

    index = load_skills(workdir / "skills")   # absent directory → empty
    assembler = assembler.with_section(
        Section("skills", "## Available skills", index.prompt_body)
    )
    registry.register(use_skill_tool(index))

An empty index renders an empty body, and a section with an empty body
is dropped, so the block disappears rather than announcing that there
are no skills.

Binding ``index.prompt_body`` binds a **snapshot**: a skill written while
the agent runs is not in it. A deployment whose agent creates skills at
runtime passes a closure that rescans instead, which costs about 20 ms
per render over 96 skills::

    Section("skills", "## Available skills",
            lambda: load_skills(root).prompt_body())

Inside the ``omicsclaw`` namespace this package imports
``omicsclaw.schema`` and ``omicsclaw.tools`` and nothing else — in
particular not ``omicsclaw.skill`` (singular), the legacy skill system,
whose name differs by one letter. ``tests/skills/
test_skills_is_a_leaf_layer.py`` enforces that by running the package in
a subprocess and inspecting what was actually imported.

公开定义位置：
```text

```

## `omicsclaw/memory/__init__.py`

Session persistence and long-term recall.

Two things live here. :class:`SqliteSessionStore` keeps conversations
between exchanges and satisfies the entry layer's ``SessionStore``
protocol structurally, so it is passed in rather than imported::

    from omicsclaw.entry import attach_sessions, build_app, resolve_app_config
    from omicsclaw.memory import Database, SqliteSessionStore

    db = Database("~/.omicsclaw/memory.db")
    app = attach_sessions(build_app(resolve_app_config(argv, env)),
                          store=SqliteSessionStore(db))

:class:`LongTermStore` keeps what is worth remembering across sessions,
deduplicated by content fingerprint and searchable through SQLite's FTS5;
:class:`Precis` renders its most valuable entries into a bounded Markdown
file for a prompt section to pick up.

:class:`MemoryExtractor` is what fills that store without anyone typing
into it. Run just before compaction, it reads the messages about to be
summarized away and writes down what stays true afterwards::

    extractor = MemoryExtractor(summarizer, LongTermStore(db))
    result = await extractor.extract(history_about_to_be_compacted)

It takes the same :class:`~omicsclaw.context.Summarizer` the compaction
layer summarizes through, so no model client is imported here, and it
never raises: a failed extraction costs a fact, while a raised one would
cost the turn being compacted.

Compaction leaves two kinds of file behind, and both are kept here.
:class:`FileOffloadStore` holds the tool results a compaction moved out of
the conversation — it satisfies :class:`omicsclaw.context.OffloadStore` —
and :class:`JsonlCompactionLog` appends one record per compaction.

Nothing here imports :mod:`omicsclaw.entry` — the dependency arrow points
one way, and the protocol is what keeps it pointing that way.

公开定义位置：
```text

```

## `omicsclaw/mcp/__init__.py`

``omicsclaw.mcp`` — tools that live in Model Context Protocol servers.

Reads a ``.mcp.json``, connects every server it names over stdio or
Streamable HTTP, and hands their tools over as ordinary
:class:`~omicsclaw.tools.MCPTool` objects::

    from omicsclaw.mcp import MCPManager, load_mcp_config

    manager = MCPManager(load_mcp_config(workspace / ".mcp.json"))
    await manager.start()                    # every server, concurrently
    registry = ToolRegistry([*foundation, *manager.tools()])
    ...
    await manager.aclose()

Once registered, an MCP tool is indistinguishable from a local one to the
engine. Inside the ``omicsclaw`` namespace this package imports only
``omicsclaw.tools`` (and through it ``omicsclaw.schema``) and
``omicsclaw.version``; everything else is the standard library.

公开定义位置：
```text

```

## `omicsclaw/planning/__init__.py`

``omicsclaw/planning`` — the plan the agent keeps, and keeps seeing.

Plan 0039, the layer that makes a long task survive its own context. The
main loop can already act, use tools and compact its history; what it
could not do is *remember what it set out to do* once the turns that said
so had been summarized away. A plan is that memory, held outside the
conversation and put back in front of the model before every call::

    from omicsclaw.planning import PlanBook, PlanInjector, plan_write_tool

    book = PlanBook(FilePlanArchive(workspace / ".omicsclaw" / "plans"))
    registry.register(plan_write_tool(book))                 # once, per app
    injector = PlanInjector(book.for_session(session_id))    # per exchange
    result = await engine.run(messages, augmentor=injector)

Four pieces, and each is a different kind of thing, which is why they are
four modules rather than one:

``plan`` / ``rules``
    The state, and what may be said about it. Standard library only.
``tool``
    The model's one way in. ``plan_write``, read and write modes,
    refusing a write that would record progress that did not happen.
``injector``
    Re-injection before every model call, plus a nudge for a run that has
    explored for a long time without writing anything down.
``archive`` / ``book``
    One plan per session, written through to disk as it changes, restored
    when a session resumes.

**Planning is native, not a mode.** There is no switch a person flips to
"enter planning" — the reference harness removed exactly that and said so
(``plan.go:3-5``). What decides whether a task gets a plan is the model,
reading :data:`~omicsclaw.planning.guidance.PLANNING_GUIDANCE` in its
system prompt.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools`` and the
standard library — the same whitelist as :mod:`omicsclaw.skills`, and
for the same reason: ``plan_write`` declares its own
:class:`~omicsclaw.tools.ToolPolicy`, and the guarded defaults would put
a human approval prompt in front of every plan update. Notably **not**
``omicsclaw.engine``: :class:`PlanInjector` satisfies that layer's
``TurnAugmentor`` structurally, and ``tests/planning/`` runs the real
code paths in a subprocess and then asserts no ``omicsclaw.engine``,
``omicsclaw.context`` or ``omicsclaw.entry`` is in ``sys.modules``.

公开定义位置：
```text

```

## `omicsclaw/permission/__init__.py`

Human-in-the-Loop permission control: whether a tool call happens at all.

Decides between the engine dispatching a call and the tool doing its work.
*How* a person is asked belongs elsewhere —
:func:`omicsclaw.tools.require_approval` puts the question and fails closed,
:class:`omicsclaw.entry.ApprovalBroker` carries it to a surface — and this
package does not touch either.

Three inputs, resolved in one pass by :class:`PermissionGate`:

- :class:`PermissionMode`, the session's posture;
- :class:`Rules`, a rule file matching on tool name *and* arguments;
- :class:`DangerPatterns`, built-in shell patterns worth a second look.

Usage: build a gate, wrap the tools, hand the registry to the engine::

    from omicsclaw.permission import PermissionGate, RuleStore, gate_tools

    gate = PermissionGate(
        mode=config.permission_mode,
        rules=RuleStore(workspace / ".omicsclaw" / "settings.json"),
    )
    registry = ToolRegistry(gate_tools(foundation_tools(config), gate))

Imports :mod:`omicsclaw.schema`, :mod:`omicsclaw.tools` and the standard
library, and nothing else in the ``omicsclaw`` namespace. In particular not
:mod:`omicsclaw.engine`, which is handed a registry and never learns anything
was gated, and not :mod:`omicsclaw.entry` — a gate is built from three
arguments, so a sub-agent, a benchmark runner or a test needs no ``AppConfig``
to make one.

公开定义位置：
```text

```

## `omicsclaw/sandbox/__init__.py`

``omicsclaw.sandbox`` — run the agent's shell commands in a container.

Starts one Docker (or Docker-compatible) container per agent, with no
network, no capabilities and the workspace bind-mounted at its own path,
and hands back an environment whose ``run_bash`` the ``bash`` tool can be
constructed with::

    from omicsclaw.sandbox import SandboxConfig, SandboxManager

    manager = SandboxManager(SandboxConfig(image="omicsclaw/runtime:1"))
    await manager.reap_orphans()
    environment = await manager.create_with_retry(workspace)
    bash = BashTool(workspace_boundary, environment=environment)
    ...
    await manager.aclose()

Only ``bash`` runs inside the container. Standard library only; this
package imports no other ``omicsclaw`` package.

公开定义位置：
```text

```

## `omicsclaw/hooks/__init__.py`

Interposition around a tool call: the seam, and the one hook nobody owned.

The gap this closes is an *extension point*. Everything this tree does
around a tool call today was written by the rebuild step that needed it —
the permission gate wraps a tool, the offloader edits the history during
compaction — and a deployment, a sub-agent or a future step wanting to
interpose anything has nowhere to put it. ``docs/plans/0031-entry-layer.md``
deferred the whole idea by name ("hooks：permission / danger / offload /
observability | 不做（Q13）") while ``entry/assembly.py`` reserved the
doorway: *"``tools`` is how hooks, permission wrappers, MCP tools and a
sub-agent's narrowed set all arrive later without this function
changing."* This package is what goes through it.

Usage — a chain, then the tools, then the gate::

    from omicsclaw.hooks import AuditHook, JsonlAuditSink, hook_tools

    hooks = (AuditHook(JsonlAuditSink(workspace / ".omicsclaw" / "audit.jsonl")),)
    registry = ToolRegistry(gate_tools(hook_tools(mounted, hooks), gate))

**Three of the reference harness's four hooks are not here, and that is
the design.** ``internal/hooks`` bundles the mechanism with four
policies; this tree already has homes for three of them, so only the
mechanism was missing:

=========================  ============================================
``hooks/hook.go``          this package — the interception seam
``permission/hook.go``     :mod:`omicsclaw.permission` (plan 0038)
``hooks/danger_hook.go``   :mod:`omicsclaw.permission` — ``DangerPatterns``
``hooks/offload.go``       :mod:`omicsclaw.context` — ``Offloader``
``hooks/plan_writer.go``   :mod:`omicsclaw.planning` — not a hook at all
``observability/hook.go``  :mod:`omicsclaw.hooks.audit` — the one left
=========================  ============================================

**One of those three is a narrowing rather than a full cover, and saying
so is the point of writing the table down.** ``context``'s ``Offloader``
moves the same bytes and writes the same kind of placeholder, but it is
driven by *budget pressure* during compaction, where ``hooks/offload.go``
runs unconditionally on every result over 10,000 characters; and it has
no equivalent of that hook's ``read_file``/``write_file``/``edit_file``
exclusion list. So a result that is large but not large enough to push
the conversation past ``Pressure.WARN`` is re-sent whole on every turn,
and a placeholder read back with ``read_file`` can be offloaded again.
Both are :mod:`omicsclaw.context`'s to decide, not this package's —
recorded here because the table would otherwise read as a claim that
nothing is missing.

Three decisions a reader should not have to re-derive:

**A hook decorates a tool, not the registry.** The reference wraps
``tools.Registry``; plan 0038 §2 rejected that for this tree and nothing
has changed. The engine probes the registry with :func:`isinstance` for
two optional Protocols, and a wrapper that forgets to forward one does
not fail — it silently charges a human's approval time to the tool's
timeout. See :class:`~omicsclaw.hooks.chain.HookedTool`.

**Hooks run inside the permission gate.** ``gate_tools(hook_tools(...))``,
in that order, matching the reference's chain order, where the permission
hook is first and the observability hook last
(``cmd/harness9/main.go:411-416`` — 413 alone builds the list, and the
appended ``obsHook`` is on 414-416). Permission therefore
decides before a hook is consulted, and a call a rule denied never
reaches one — the same blind spot the reference's observability hook has,
for the same reason.

**A hook cannot ask a human.** :mod:`omicsclaw.hooks.base` explains what
was dropped; this is why it can be. The gate publishes an ``AUTO``
policy before running the tool it settled, and
:func:`~omicsclaw.tools.require_approval` reads that first
(``tools/context.py:593-594``), so a question put from inside the chain
would answer itself. The behaviour is the reference's
``explicitlyAllowedContextKey`` reached by a mechanism that already
existed rather than by two more context keys — and it means "why was I
asked?" keeps exactly one answer, in the rule file.

Imports :mod:`omicsclaw.schema`, :mod:`omicsclaw.tools` and the standard
library. Not :mod:`omicsclaw.engine`, which is handed a registry and must
not learn that anything wraps a tool; not :mod:`omicsclaw.permission`,
which would make two leaves depend on each other for no gain — the gate
does not know about hooks either, and the composition root joins them in
one line. ``tests/hooks/test_hooks_is_a_leaf_layer.py`` is what enforces
it.

公开定义位置：
```text

```

## `omicsclaw/observability/__init__.py`

Seeing what the loop did: spans, measurements, and no engine changes.

Step 6.11 of the staged rebuild, plan 0043. The layer that answers *is it
working, what does it cost, what is slow* about a running agent, modelled
on ``internal/observability`` in the Go reference harness at
``/workspace/dataset/private/zhouwg_data/harness9``.

Usage — three lines at the composition root, two at the exchange::

    telemetry = build_telemetry()                       # reads OTEL_* env
    provider  = telemetry.trace_provider(provider, model=config.model)
    hooks     = (*build_hooks(config), *telemetry.tool_hooks())

    async with telemetry.run(session_id=sid, prompt=text) as scope:
        async for event in engine.run_stream(messages, ...):
            scope.observe(event)

The trace tree that produces::

    omicsclaw.interaction               session.id, agent.turns, agent.stop_reason
    ├── omicsclaw.turn        agent.turn=1
    │   ├── omicsclaw.llm_request       gen_ai.*, llm.tokens.*
    │   ├── omicsclaw.tool              tool.name, tool.status
    │   └── omicsclaw.tool              (siblings overlap: a parallel turn)
    └── omicsclaw.turn        agent.turn=2
        └── omicsclaw.llm_request

Six instruments, all declared in
:data:`~omicsclaw.observability.attributes.INSTRUMENTS`: LLM duration,
input and output tokens, tool calls, tool duration, turns.

Four decisions worth not re-deriving.

**No seam was cut into the engine.** The reference adds an
``EngineObserver`` interface to ``internal/engine`` and calls it at four
points, because a Go loop can only be watched by being called back. This
rebuild's loop already publishes those moments as
:class:`~omicsclaw.engine.EngineEvent`, so
:class:`~omicsclaw.observability.scope.RunScope` *consumes* an existing
stream instead. ``omicsclaw/engine/`` is unchanged by this step — the
layer whose own docstring says "no I/O, no logging" keeps that property,
and ``tests/observability/test_observability_boundaries.py`` asserts the
arrow never points back.

**This is an adapter layer, not a leaf.** It imports
:mod:`omicsclaw.schema`, :mod:`omicsclaw.provider`,
:mod:`omicsclaw.engine` and :mod:`omicsclaw.hooks`, and none of the four
imports it — the same one-way shape ``internal/observability`` has, and
the reason it can be deleted from a deployment without anything else
noticing. It does **not** import :mod:`omicsclaw.entry`: the composition
root joins them, in the three lines above.

**OpenTelemetry is optional, and the contract is ours.**
:mod:`omicsclaw.observability.contract` declares ``Span`` / ``Tracer`` /
``Meter`` in the standard library, :mod:`omicsclaw.observability.otel` is
the only module that knows the SDK exists, and
:mod:`omicsclaw.observability.console` is a working backend that needs
nothing installed. So ``import omicsclaw.entry`` keeps working on a
machine that never wanted telemetry — the same property
``provider/base.py`` claims for vendor SDKs — and the test suite can
exercise a real backend rather than only the no-op. What is given up is
named in :mod:`omicsclaw.observability.contract`: a third-party OTEL
instrumentation will not nest inside these spans.

**Payloads do not leave the machine unless somebody says so.** The
reference always serializes the conversation, the tool arguments and the
tool output into span attributes, which is reasonable for a coding
harness and is not reasonable here:
:data:`~omicsclaw.entry.assembly.SAFETY_RULES` rule 1 states *"Genetic data
never leaves this machine"*, and an OmicsClaw prompt routinely names a
cohort, a sequencing run or a patient sample. So
:attr:`~omicsclaw.observability.config.ObservabilityConfig.capture_content`
is off by default and every ``langfuse.*`` attribute is gated on it.
Counts, durations, token usage, model, outcome and turn structure are
recorded either way — everything a dashboard needs, and nothing that
names a person or a gene.

Configuration is the reference's five environment variables plus that
one::

    OTEL_ENABLED=true
    OTEL_SERVICE_NAME=omicsclaw
    OTEL_EXPORTER_TYPE=noop|stdout|otlp
    OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20...
    OMICSCLAW_OTEL_CAPTURE_CONTENT=false

公开定义位置：
```text

```

## `omicsclaw/subagent/__init__.py`

``omicsclaw/subagent`` — handing a bounded sub-task to a second agent.

A sub-agent is not a new kind of thing: it is one more run of the same
engine, opened with its own system prompt, given a narrowed set of the
parent's tools, and handed no conversation at all. What comes back is one
piece of text::

    from omicsclaw.subagent import SubAgentRegistry, TaskTool

    agents = SubAgentRegistry([definition])
    registry.register(TaskTool(agents, runner))     # once, per app

Seven modules, each a different kind of thing:

``definition``
    What a sub-agent is, and which parent tools it resolves to.
``registry``
    The sub-agents a deployment offers, in a stable order.
``frontmatter`` / ``loader``
    Reading definitions out of a directory of Markdown files.
``prompt``
    The system prompt a delegated run opens with.
``delegate``
    The seam that actually runs one, declared here and satisfied above.
``task_tool``
    The model's one way in.

**This package does not build engines.** Constructing a child engine
needs a provider, a tool registry and a deployment's sandbox state, which
are the composition root's to hold; :class:`~omicsclaw.subagent.Delegate`
is how the work leaves this layer without it learning any of that.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools`` and the
standard library — the same whitelist as :mod:`omicsclaw.planning`, and
for the same reason: ``task`` declares its own
:class:`~omicsclaw.tools.ToolPolicy` and holds the engine's timeout pause
for the length of a delegation. Nothing here imports
``omicsclaw.engine``, ``omicsclaw.entry`` or ``omicsclaw.skills``, and
importing this package pulls none of them in: :class:`ChildPrompt`
satisfies the engine's prompt seam structurally rather than by
inheriting from it, and :class:`Delegate` is a Protocol the composition
root supplies an implementation of.

公开定义位置：
```text

```

## `omicsclaw/entry/__init__.py`

``omicsclaw.entry`` — where a user reaches the agent.

Plan 0031, step 6 of the staged rebuild. The five layers before it are
each complete and **none of them can be reached**: ``schema`` knows what
a message looks like but not where one comes from, ``engine`` knows how
to run a conversation to convergence but not where the conversation was
stored, ``context`` knows how to assemble a prompt but not where
``OMICSCLAW.md`` lives. This package is the answer to all five at once::

    from omicsclaw.entry import attach_sessions, open_app, resolve_app_config

    config = resolve_app_config(argv, env)     # both are required, plan 0037
    app = attach_sessions(await open_app(config))
    handle = await app.sessions.submit("session-1", "分析这份 Visium 数据")
    async for event in handle.observe():
        ...
    await app.aclose()

**The top layer, not a leaf.** Every package below declares what it may
import; this one is the single place allowed to import all of them. Its
layering test is therefore inverted — ``tests/entry/
test_entry_is_the_top_layer.py`` asserts that nothing in the five layers
imports ``omicsclaw.entry``, and that driving this package loads none of
the packages this rebuild replaces (``omicsclaw.runtime``,
``omicsclaw.control``, ``omicsclaw.providers``, ``omicsclaw.skill``,
``omicsclaw.surfaces``) and no optional dependency
(``fastapi``, ``textual``, either vendor SDK). Behaviourally, in a
subprocess, because a static check catches spellings and only a probe
catches facts — plan 0028 lost 286 green tests to one lazy
``importlib``.

**The three surfaces import this package; it does not import them.**
``omicsclaw/surfaces/`` is this step's *input*, not its output: the
owner's ruling of 2026-09-19 is that the channel, CLI and desktop
implementations are reused rather than rebuilt. They depend on
:class:`~omicsclaw.entry.ingress.InboundMessage`, the turn event stream
and the renderer, and on nothing deeper.

**What a surface may depend on.** Plan 0031 §7 holds the three surfaces
to three things — :class:`~omicsclaw.entry.events.TurnEvent`, the
renderer, and the ingress types — so all three are exported here
alongside the deployment and the registry. Nothing below
:mod:`omicsclaw.entry` should appear in a surface's imports; if it does,
the seam moved.

**The surface subpackages are ports, not rewrites.** All three have
landed — :mod:`omicsclaw.entry.channel`, :mod:`omicsclaw.entry.cli` and
:mod:`omicsclaw.entry.desktop` — and between them they are what has
exercised the contracts above; a contract only a reference consumer has
exercised has not been exercised. None of them is imported here: a
surface costs an optional dependency and a deployment runs one.

:func:`~omicsclaw.entry.build_app` still leaves
:attr:`~omicsclaw.entry.assembly.AgentApp.sessions` as ``None`` — the
registry and the app name each other, and
:func:`~omicsclaw.entry.session.attach_sessions` is the one line that
ties that knot.

公开定义位置：
```text

```

## `omicsclaw/launch/__init__.py`

``omicsclaw.launch`` —— the process shell, and nothing else.

Plan 0037 §5, scheme C. Everything about *being a process* lives here:
the command line, the environment, the exit code. Everything about
*being an agent* lives one layer down in :mod:`omicsclaw.entry`, which
after this package exists is three libraries and no processes.

**Why a layer rather than three ``__main__`` files.** A process entry
point is naturally a member of the layer it starts, and that membership
is what forced ``omicsclaw/entry/`` to read ``argv`` for itself and
forced its one-parse-point rule to carry an exemption (plan 0031 Q8,
recorded as open in ``docs/FRAMEWORK-REBUILD.md`` step 6). Three
surfaces would have meant three exemptions, and a growing exemption list
is the only way that rule degrades. Lifting the process out removes both
of the exemptions it had instead of adding two more.

**This file is the only reader of two globals in this package.**
``sys.argv`` and ``os.environ`` are named here —— at the two defaults of
:func:`main`, and in :func:`_adopt_dotenv`, which is the one place that
*writes* the process environment —— and nowhere else under
``omicsclaw/launch/``: every function below takes them as arguments.

**The rest of the rebuilt stack is not env-free, and saying so was
wrong.** Plan 0037 §3-1 claimed ``os.environ`` was read by
``resolve_app_config`` alone "plus the one declared exception
``provider_from_env``". It is not, and the claim passed review only
because the other readers live in packages the scan did not cover. The
real list is four entries long, each with a reason, and it is enumerated
and pinned by
``tests/launch/test_the_environment_is_read_in_known_places.py`` —— an
inaccurate rule that nothing enforces is worse than a wider rule that
something does.

**The grammar is three commands, one per surface**::

    oc cli                                 REPL
    oc cli --prompt-file brief.md          one exchange, then exit
    oc desktop --port 8765                 HTTP backend
    oc channel --channels telegram,feishu  IM adapters

``oc run <skill>`` is deliberately absent (owner ruling, 2026-09-20):
deterministic skill execution is what the agent does in a session and
what an in-surface command asks for, not a fourth way into the program.

**Exit codes.** ``0`` converged, ``1`` the surface ran and did not
converge —— or failed in a way nothing here anticipated —— ``2`` the
command line or the deployment was refused, which includes an optional
dependency that is not installed because nothing the user retypes fixes
that either, ``129`` hung up (``SIGHUP``, the terminal closed),
``130`` interrupted (``SIGINT``) and ``143`` terminated (``SIGTERM``).
The last three are ``128 + signum``, the convention a supervisor
already reads.

公开定义位置：
```text
91: FunctionDef main
168: FunctionDef _adopt_dotenv
```

## `omicsclaw/skillenv/__init__.py`

``omicsclaw.skillenv`` — which Python packages a skill needs, whether ``bash`` can import them, and adding them.

``registry`` reads a skill's ``## Dependencies`` line and the dependency
registry file ``<skills root>/_sdk/deps.py`` (as a file, never imported);
``probe`` runs fixed Python programs where ``bash`` runs; ``report``
renders what it found as a note for ``use_skill``. ``sources`` checks what
reaches pip, ``overlay`` builds overlay environments over the base
interpreter, and ``tool`` is ``install_skill_deps``; they are not imported
here. Inside ``omicsclaw`` this package imports only ``schema``, ``tools``
and ``skills``.

公开定义位置：
```text

```

## `omicsclaw/evals/__init__.py`

Scripted, hermetic, deterministic agent evals.

A :class:`Case` scripts the model's replies with a
:class:`ScriptedProvider`, and :func:`run_case` runs it through the
production assembly (:func:`~omicsclaw.entry.build_app` with
``provider=``) and a real session, then checks its assertions. Skill
scripts can be answered from recorded :class:`StubResult` fixtures, and
:func:`build_report` turns a suite's results into JSON, Markdown and a CI
step summary. :mod:`omicsclaw.evals.live` runs the routing seeds against
a real model and scores which skill it chose.

This package imports from :mod:`omicsclaw.entry` and the layers below
it. Nothing else in ``omicsclaw`` imports it.

公开定义位置：
```text
63: FunctionDef __getattr__
```

## `omicsclaw/attachments/__init__.py`

Public Interface for immutable accepted Attachment storage.

公开定义位置：
```text

```

## `omicsclaw/common/__init__.py`

Common utilities shared across all OmicsClaw skills.

公开定义位置：
```text

```
