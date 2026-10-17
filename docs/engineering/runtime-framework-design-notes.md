# Runtime framework design notes

These module introductions were archived on 2026-10-16. The quoted text records earlier implementation decisions, migration context and measurements. References to retired code or old deployments describe that earlier state. Consult the current implementation and deployment documentation for present behavior.

Each source module retains a short description and a link here. Function and class documentation remains beside the code.

## omicsclaw/entry/render.py

```text
``omicsclaw/entry`` — one exchange's frames as text, and as JSON.

Plan 0031 §3.2. Two projections of :class:`~omicsclaw.entry.events.
TurnEvent`, deliberately of different kinds:

:class:`TextRenderer` is **stateful**, because the Channel Surface cannot
be served by a pure function. An IM platform rate-limits message edits, so
a bot that forwarded every text delta would be throttled within a
sentence; deltas have to be accumulated *across* events and sent in
batches. The CLI wants the opposite — a token on screen as it arrives —
so the same class does both, and which one is a constructor argument
rather than two classes with one shared bug.

:func:`to_wire` is **pure**, and is what crosses a process boundary.

Every text field a control line is built from — a tool name, a progress
message, a request id, a refusal's reason, a compaction's failure, an
error's type name, an approval card's reason and arguments, a question
card's question and options — is made
inert by ``omicsclaw/entry/display.py`` before it is put in the line, so
no surface receives a control character from one. Text and reasoning
deltas are returned as they arrived; a surface that prints them makes
them inert itself.

Three things this module is careful about, each with a named trap in plan
0031 §6:

*The elapsed figure is not "how long the tool took"* (trap 6).
``EngineEvent.duration_s`` is wall-clock measured by the scheduler and
therefore **includes time a human spent deciding an approval**
(``engine/types.py:239-245``). The guard here is structural rather than
editorial: no field named for a tool's duration ever reaches a caller —
the text always carries :data:`ELAPSED_INCLUDES_APPROVAL_WAIT` and the
wire always uses ``elapsed_s`` plus an explicit
``elapsed_includes_approval_wait`` flag. Neither can be composed by
accident, and neither depends on anyone reading the prose.

*A missing token count and a zero token count are different facts*
(trap 7). ``usage=None`` only ever comes from a streamed run and means the
backend reported nothing; a zero :class:`~omicsclaw.schema.Usage` comes
from the blocking path and means "free **or** unreported", because
``Completion.usage`` cannot express the difference
(``engine/types.py:204-215``). They render as
:data:`USAGE_UNREPORTED` and :data:`USAGE_ZERO`, never as the same string.

*Tool arguments and tool output are not rendered as text* (Q22 rule 1),
approval lines excepted. Argument payloads and tool output can carry
subject identifiers, and this layer feeds a CLI, an IM transport and an
HTTP stream from one place, so :class:`TextRenderer` never puts a raw
payload or any output on a screen or in a chat; a Surface that has decided
it may show them reads them off
:attr:`~omicsclaw.entry.events.TurnEvent.engine` itself, having made that
decision explicitly. An approval line carries the request's reason and,
unless :attr:`~omicsclaw.tools.ApprovalRequest.reason_shows_call`, the
call's arguments with credential-named values hidden; both are made inert
by ``omicsclaw/entry/display.py``, shown to the person deciding, and never
logged.

:func:`to_wire` is the other case and the opposite rule: it **must**
carry both, because ``ToolCall.arguments`` crossing byte for byte is a
published contract (see below) and a ``tool_result`` without its content
is not a result. It follows that ``to_wire``'s **output** is itself
inside Q22 rule 1's protection: those dictionaries reach an SSE stream by
way of ``desktop/turn_observation.py``, whose ``_wire_json_value``
redacts by *key name* only and passes every string through untouched. A
frame is therefore not safe to log, and nothing here logs one.

**Wire compatibility** (Q24). ``POST /chat/stream`` is a published
contract with an external client, not this layer's to invent. Three
consequences are load-bearing and are pinned by tests:

- ``ToolCall.arguments`` crosses **byte for byte**. It is a raw JSON
  string precisely so that a decode/re-encode cannot reorder keys
  (``schema/message.py:81-89``); prompt-prefix caching and replay
  evidence both depend on those bytes.
- A ``tool_result`` frame uses the key names
  ``surfaces/desktop/_chat_sse.py`` reads — ``tool_use_id``,
  ``tool_name``, ``content``, ``is_error``. That renderer's 4 MiB
  oversize projection reaches into the payload by name, so a frame that
  spelled them differently would lose its correlation identity exactly
  when a result is too big to send.
- The terminal frame preserves the error's **type**
  (``wire_contract.py``: ``terminal_error_type_preserved``), and only its
  type: an exception's text can contain a URL with credentials in the
  query string.

The mapping from :class:`~omicsclaw.entry.events.TurnEventType` to that
client's frame names is :data:`DESKTOP_CHAT_FRAME_TYPE`; ``to_wire``
itself tags frames with the enum value, because the enum is this layer's
truth and the client's vocabulary is the Desktop Surface's to speak.

```

## omicsclaw/observability/scope.py

```text
The span tree of one run, built from the events the loop already emits.

This module is this layer's answer to ``engine/observer.go`` plus
``observability/observer.go``, and it is the one place where the design
genuinely departs from the reference rather than merely being spelled
differently. **No seam was added to the engine.**

The reference has to add one. Go's loop can only be observed by being
called back, so ``internal/engine`` declares a four-method
``EngineObserver`` interface, stores one, and invokes it at four points.
This rebuild's loop already publishes a richer record of the same four
moments — :class:`~omicsclaw.engine.EngineEvent`, yielded by
``AgentEngine.run_stream`` — so the observer is a *consumer* of something
that exists rather than a hook that had to be cut into a leaf layer whose
own docstring forbids I/O. ``omicsclaw/engine/`` is unmodified by this
step, and ``tests/observability/test_observability_boundaries.py``
asserts that nothing in ``engine`` imports this package.

Three consequences are worth knowing before reading the code.

**There is no ``TURN_START`` event, so a turn span is created lazily.**
The kernel begins turn *N+1*'s work inside the very ``__anext__`` that
follows turn *N*'s ``TURN_END``, so the only honest moment to open the
next turn span is the instant after the previous one closed — at which
point nobody knows yet whether the run continues. Opening it eagerly
would export one empty phantom span at the end of every run.
:class:`TurnScope` therefore holds the *intent* to open a span and
materialises it the first time somebody asks for a parent, which is the
model call. A run that stops instead discards an intent, which costs
nothing and exports nothing. The price, stated so it is not rediscovered
as a bug: a turn span starts at its **model call**, so the compaction and
plan-injection that precede it fall outside — they are inside the
interaction span, just not inside the turn.

**The blocking path gets two levels, not three, and has to say so.**
``AgentEngine.run`` drives the same kernel and the kernel produces the
same events — ``run`` then **drops them** ("a caller who wants to watch a
run asked for ``run_stream``"). So a scope driving it is never told where
a turn ended, and the honest answer to *why* is in two halves, because
only the first half is forced:

- Turn 1 genuinely cannot be inferred in time. Its model call asks for a
  parent *before* any event could have arrived, so no amount of waiting
  helps and eager staging is the only option.
- Turns 2..N are a different matter. ``TURN_END`` for turn *N* exists
  inside the kernel at the moment turn *N+1* begins; it is discarded by
  ``run``'s own pre-existing contract, not by anything this layer decided.

So **two levels here is the cost of ``run``'s contract, not a limit of
the event model**: an observer attached inside the kernel, the way the
reference's ``EngineObserver`` is, would get three levels on both paths.
That trade was taken deliberately — not cutting a seam into a leaf layer
beats three levels on a path no surface drives — and it is written down
here rather than left in a commit message, so that reopening it is a
decision rather than a discovery.

Hence :attr:`RunScope.turn_events`, which
:func:`~omicsclaw.entry.turn.run_turn` sets to ``False`` and every
streaming caller leaves alone.

Getting it wrong is what the flag is designed against rather than what it
invites. Told ``False`` and sent turn events anyway, a scope starts
grouping from the next boundary — self-correcting. Told ``True`` and sent
none, it warns at exit naming the run it could not segment, because the
alternative is the failure this flag exists to prevent: **one turn span
labelled ``agent.turn=1`` silently containing every model call of a
five-turn run**, which is not a missing level but a wrong number.
Every count, duration and token figure is unaffected either way.

**Parenting is carried by one :class:`~contextvars.ContextVar`, and it is
a stack of one slot.** :func:`current_parent` is what
:class:`~omicsclaw.observability.provider.TracedProvider` and
:class:`~omicsclaw.observability.hook.TracingHook` call; they never
receive a parent as an argument, because the engine sits between them and
this module and must not learn that either exists. A
:class:`~contextvars.ContextVar` is the right carrier for the same reason
``omicsclaw/tools/context.py`` already uses one: each tool call runs in
its own :class:`asyncio.Task`, a Task copies the context at creation, so
concurrent tool calls cannot see each other's bindings and two
simultaneous sessions in one process cannot see each other's traces.

Imports :mod:`omicsclaw.engine`, :mod:`omicsclaw.schema` and the standard
library.

```

## omicsclaw/hooks/__init__.py

```text
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

```

## omicsclaw/context/tokens.py

```text
``omicsclaw/context`` — what a conversation costs, in tokens.

Plan 0030 task A. Every budget decision in this package divides by a
number produced here, so the counting rule is written once, in this
module, and nowhere else: :mod:`~omicsclaw.context.budget`,
:mod:`~omicsclaw.context.prompt` and
:mod:`~omicsclaw.context.compaction` call these functions instead of
growing a second ``len(text) // 4``. The layer this one replaces had
three estimators living side by side, in two different units.

**The rule, and why it is not the reference harness's.** The harness
counts ``len(m.Content) / 4`` (``internal/memory/token.go:15,19-29``),
and Go's ``len`` on a string counts **bytes**. Python's ``len`` counts
code points, so the literal translation is not the same function::

    s = '这是一个中文句子，用于测试 token 估算。'
    len(s)          == 23      # code points — what a naive port counts
    len(s.encode()) == 55      # bytes — what the harness counts
    55 // 4 == 13   vs   23 // 4 == 5

A naive port under-counts Chinese text two to three fold.
Under-counting is the dangerous direction: it means
believing there is room, sending the request, and being truncated or
refused by the API. So the harness's *intent* — roughly four ASCII
characters to a token — is re-derived here rather than transliterated::

    tokens(text) = ceil(ascii_chars / 4) + non_ascii_chars

Pure ASCII agrees with the harness to within the rounding, and errs
*above* it where they differ: ``token.go:29`` divides a running total
once, which floors, while this rounds each field up. ``"hello"`` is one
token there and two here. Anything outside ASCII is billed a whole
token per character, which is deliberately pessimistic in the same
direction: a CJK character is usually somewhat less than one token, and
erring above the truth is the only safe error here.

**Accuracy is not claimed. This estimator is UNCALIBRATED.** No
tokenizer was available to measure against (``tiktoken`` is not
installed on the machine this was written on and cannot be installed —
there is no network), so no error figure appears anywhere in this
package. Plan 0030 §11.A-8 carries the debt. A deployment that has a
real tokenizer injects one through :class:`TokenCounter`; nothing here
reaches for ``tiktoken`` on its own, and nothing here changes behaviour
because ``tiktoken`` happens to be installed — a local budget that
silently moves when an optional package appears is not a budget.

**What is counted, per message.** Content, ``reasoning_content``,
``tool_call_id``, ``name``, and for every tool call its id, name and
raw ``arguments``. ``token.go:19-29`` counts content, ``tool_call_id``
and the three parts of a tool call — the two it leaves out are
``reasoning_content`` and ``name``, and it leaves them out because
``internal/schema/message.go:33-63`` has no field for either. This
repository's :class:`~omicsclaw.schema.Message` has both, and ADR 0077
added ``reasoning_content`` on purpose (thinking endpoints reject a
history that has lost it), so copying the harness's field list would
systematically under-count exactly the field this repository went out of
its way to keep.

Each field is estimated and rounded up **separately**, where the harness
sums raw lengths and divides once at the end. Many short fields
therefore cost slightly more here than there — again the safe direction,
and it is what makes "add a field, the estimate grows" true rather than
true-above-four-characters.

**No separate prompt estimator.** The layer this replaces had
``estimate_prompt_tokens(system, messages)``. It is not missing, it is
an addition::

    estimate_text_tokens(system_prompt) + estimate_messages_tokens(msgs)

**Known blind spot: images cost zero.** ``Message.content`` is a
``str`` — ADR 0077 deliberately left multimodal content parts out — so
an image reaching this layer has no representation to count and is
billed nothing. No image reaches this layer today: the channel adapters
refuse or drop inbound photos and attachments, and an MCP image result
arrives as a one-line text placeholder. Once the schema carries image
parts, the budget will be optimistic by roughly a thousand tokens per
picture. **Do not paper over it with a heuristic**: "looks
like base64, add 1300" misfires on ordinary text, which is a worse
failure than the one it treats. Plan 0030 §11.B-11.

```

## omicsclaw/observability/__init__.py

```text
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

```

## omicsclaw/tools/builtin/__init__.py

```text
The foundation tools: the hands the agent actually works with.

Plan 0029, then its §12 follow-ups. ``read_file``, ``write_file``,
``edit_file`` and ``bash`` are built on the sandbox boundary in
:mod:`omicsclaw.tools._workspace` and the path locks in
:mod:`omicsclaw.tools._pathlock`, and are modelled on the reference
harness's ``read_file.go`` / ``write_file.go`` / ``edit_file.go`` /
``bash.go``. Those four are the canonical set: it is what
``cmd/harness9/main.go:275-278`` mounts, and what the two other harnesses
plan 0029 appendix A compares against converge on independently.

``web_fetch`` and ``web_search`` join them (owner, 2026-09-18, reversing
plan 0029 §2's ruling against building them in this step). They stand on
a *second* boundary rather than the filesystem one —
:mod:`omicsclaw.tools._websafety`, the SSRF gate that is to the network
what ``_workspace.py`` is to the disk — and they share the HTML reduction
in :mod:`omicsclaw.tools._html`. Read ``_websafety.py``'s docstring
before mounting either: the gate stops this agent reaching *internal*
addresses, and it does **not** and cannot stop data leaving in a URL.
That second thing is what their ``ASK`` policy is for.

The four file tools each take a
:class:`~omicsclaw.tools._workspace.Workspace`, so there is no useful
zero-argument instance of one and no ``builtin_tools()`` helper here:
mounting a foundation tool is always a decision about *which* workspace,
and that decision belongs to whoever is assembling the registry. The two
web tools take no workspace — their boundary is a destination, not a
path — which is why they are constructed keyword-only.

``bash`` is the one with **no boundary argument at all**, and therefore
nothing for either helper to check — its only boundary is the approval
gate, until something implements its
:class:`~omicsclaw.tools.builtin.bash.BashEnvironment` seam.

**Two of these names collide with the legacy layer and two do not.**
``read_file`` / ``write_file`` / ``edit_file`` deliberately differ from
``file_read`` / ``file_write`` / ``file_edit`` so both layers can be
mounted while the migration runs; ``web_fetch`` and ``web_search`` are
spelled identically in ``runtime/tools/builders/engineering.py``, so
those two cannot be, and whoever migrates has to retire one side. Each
module's ``TOOL_NAME`` docstring says which case it is.

**Plan 0028's three reference tools used to live here** —
``inspect_analysis_environment``, ``analyze_sequence`` and
``save_gene_panel``. They existed to answer one question with code
rather than a claim: *does what the registry and the adapters provide
survive contact with a tool that really does something?* The foundation
tools answer it better, because they are the real thing rather than a
demonstration, so the three were removed (owner, 2026-09-18) once
``read_file`` / ``write_file`` / ``bash`` carried every property the
three were built to prove:

- a plain callable wrapped with no ceremony → ``read_tool()``;
- a correctable Observation for a truncated payload, a missing field or
  a wrong type, with the run surviving → ``read_file``'s decode and
  validation tests;
- the out-of-band convention of plan 0028 §4 Q4 under real engine
  concurrency → ``tests/tools/test_context.py``;
- an approval prompt faithful to the exact bytes the model sent, which
  :class:`~omicsclaw.tools.function_tool.FunctionTool` cannot give →
  ``write_file`` and ``bash``, both hand-written against
  :class:`~omicsclaw.tools.base.Tool`.

``ask_user`` (:class:`~omicsclaw.tools.builtin.ask_user.AskUserTool`) has
no boundary argument either. It reads and writes nothing: it puts one
question to the person through the tool context's question channel and
returns the answer.

**Standard library only, and the constraint shaped these tools too.**
``omicsclaw/tools/`` may import ``omicsclaw.schema`` and nothing else
inside this namespace, which ``tests/tools/test_tools_is_a_leaf_layer.py``
enforces by walking every module under the package — this subpackage
included, from the moment its files exist.

```

## omicsclaw/entry/desktop/turn_observation.py

```text
The ``/chat/stream`` SSE body: turn events projected onto Desktop frames.

Each frame is one ``data:`` line carrying a JSON object with exactly two
keys, ``type`` and ``data``, followed by a blank line. ``data`` is a
string: plain text for ``text``, ``thinking``, ``tool_output`` and the
terminal frames, and a JSON-encoded object for the rest.
:func:`~omicsclaw.entry.desktop._chat_sse.render_chat_sse_frame` keeps
every frame under 4 MiB.

A frame an event produced is preceded by an ``id:`` line holding that
event's sequence number, which is a resume cursor: a client that has
received ``id: N`` has every frame of the events numbered up to N, and a
``/chat/stream`` request with ``after_seq: N`` continues after them. The
frames that end the stream (a ``/compact`` exchange's reported
``status``, ``result``, ``error``, ``done``), the ``error`` and ``done``
a body adds when its observation ends early, and ``keep_alive`` carry no
``id:`` line. A client holds the frames without one until ``done``
arrives, and drops them if the connection breaks first, because a
resumed stream sends them again.

The frames of ``sse_schema_version`` 3:

* ``text`` / ``thinking`` — an answer delta / a reasoning delta.
* ``tool_use`` — ``tool_use_id``, ``tool_name``, ``arguments`` (the call's
  raw JSON string, byte for byte), ``turn``, and the identity keys.
* ``tool_result`` — ``tool_use_id``, ``tool_name``, ``content``,
  ``is_error``, ``elapsed_s`` (which includes any approval wait),
  ``elapsed_includes_approval_wait``, ``turn``, and the identity keys.
* ``tool_output`` — a tool's progress message.
* ``suggestions`` — additive in ``sse_schema_version`` 3 (P6 item 2):
  up to three follow-up questions the model offered with the finished
  answer, ``{"suggestions": [...]}`` plus the identity keys. Sent only
  when the answer ended with the prompt's suggestion block; a client
  that does not know the type drops it, which is the frame's absence
  case exactly.
* ``status`` — a compaction: ``kind`` is ``"compaction"``, with the
  before/after counts, ``degraded``, ``written_back`` and the identity
  keys. A ``/compact`` exchange always sends one, including when nothing
  was compacted (``written_back`` false).
* ``permission_request`` — ``request_id``, ``tool_name``, ``arguments``,
  ``reason``, ``reason_shows_call``, ``risk_level``, ``approval_mode``,
  ``ask_every_time``, ``can_remember`` (whether "always allow" can take
  effect for this call) and the identity keys. Sent only while the
  request is outstanding; a request a session grant or ``full_access``
  answers is allowed without a card.
 * ``shadow_review`` — additive in ``sse_schema_version`` 3 (P6 item
   4): an automatic reviewer's ``warn`` or ``fail`` verdict on this
   session's recent artifacts, ``{"verdict", "note"}`` plus the
   identity keys. Sent only while an exchange of the session is live; a
   client that does not know the type drops it, and ``pass`` is never
   sent — quiet is the frame's absence case exactly.
 * ``event_omitted`` — frames were lost to this cursor, or one frame was
  too large to send. Its ``id:`` is the sequence number just before the
  first event still available, or the oversized event's own.
* ``result`` — once, before ``done``, when the exchange converged:
  ``usage`` (the key-wise sum of every model call's ``input_tokens``,
  ``output_tokens``, ``cache_read_tokens`` and ``cache_write_tokens`` in
  the exchange, as read by this body and any earlier one over the same
  exchange, or ``null`` if none reported usage), ``usage_reported``
  (every call reported usage and no call can have gone unread),
  ``model_calls``, ``provider`` and ``model``.
* ``keep_alive`` — after :data:`KEEPALIVE_INTERVAL_S` of silence.
* ``error`` then ``done`` — the exchange was cancelled (``"cancelled"``) or
  failed (the exception's type name only); ``done`` alone — it converged.

The identity keys are ``sequence``, ``turn_id`` and ``session_id``.

```

## omicsclaw/tools/__init__.py

```text
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
:class:`~omicsclaw.tools.builtin.ask_user.AskUserTool` is there too: it
asks the person one question through the context's question channel.

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

```

## omicsclaw/context/budget.py

```text
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

```

## omicsclaw/entry/config.py

```text
``omicsclaw.entry`` — the one place a deployment is read.

Plan 0031 task A, decision Q8. Every layer below this one was built to
know nothing about its environment: ``EngineConfig`` reads no variables
on purpose (plan 0027: "a turn ceiling that moves with the shell makes
two runs of one benchmark incomparable"), ``omicsclaw.context`` takes
the contract file and the model table as callables and integers, and
``omicsclaw.tools`` takes a :class:`~omicsclaw.tools._workspace.Workspace`
rather than finding one. All of that knowledge arrives here.

**One parse point, and it fetches one thing.**
:func:`resolve_app_config` is the only place a deployment is
interpreted, but as of plan 0037 §5.4 it no longer *fetches* anything:
the command line and the process environment are handed to it by
:func:`omicsclaw.launch.main`, which is the only reader of those two
globals in the program. This module therefore takes no interest in the
process it runs in, and the rule over this package is now flat — no
module under ``omicsclaw/entry/`` names either global, with no
exemption list. The one declared exception to the whole arrangement
survives from plan 0031 Q8 and lives a layer down:
:func:`omicsclaw.provider.provider_from_env` keeps reading its own API
key, because moving a secret through this dataclass would only add a
place it can be printed from. Everything else — which workspace, which
model, how long a tool may run — is a field below.

**The timeout is one number, written once.** ``tool_timeout_s`` is the
single source for both halves of the ceiling a tool runs under:
:meth:`AppConfig.engine_config` derives the scheduler's
``EngineConfig.tool_timeout`` and :meth:`AppConfig.bash_timeout` derives
what ``bash`` is constructed with, one
:data:`~omicsclaw.tools.builtin.bash.ENGINE_TIMEOUT_MARGIN` below it.
Plan 0031 §12-2 is blunt about why two independent literals are not
acceptable: the day somebody raises the ceiling they will raise one of
them, and half of this mechanism does not help. The margin is imported
rather than re-typed for the same reason.

**Bad input is refused, not defaulted.** ``omicsclaw/provider/config.py``
replaces an unparseable environment value with its default; this module
raises :exc:`AppConfigError` instead. The divergence is deliberate and
narrow: a provider setting that silently falls back costs one slow
request, while ``OMICSCLAW_TOOL_TIMEOUT_S=6OO`` silently falling back to
60 s is exactly the failure §12-2 exists to prevent, and a single parse
point is the only place that can say so out loud.

**What this module does not read.** No ``.env`` file, and that is no
longer a gap. The reference harness loads ``cwd/.env``
(``main.go:117``) and this repository's ``.env.example`` documents ``.env``
as the way channel credentials are supplied, but a resolver that returns
a value cannot make ``provider_from_env`` see a variable without
mutating the live environment behind its caller's back. Loading it
belongs to whatever owns the process, and since plan 0037's review
:func:`omicsclaw.launch._adopt_dotenv` does it — before this function
is called, without overriding anything already exported. Between the
delivery and the review the promise was recorded in three places and
implemented in none, which is the failure mode "recorded as open" has
when nobody is counting.

``os`` is still imported, for :data:`os.pathsep` in :func:`_as_paths`
and nothing else. Plan 0037 §5.4 predicted the import would go with the
default; separator-joined path lists are the reason it does not.

```

## omicsclaw/tools/builtin/edit.py

```text
``edit_file`` — replace one passage of a file, not the whole file.

Modelled on harness9's ``internal/tools/edit_file.go``. Where
``write_file`` replaces a file entirely, this takes an anchor
(``source_text``) and what to put in its place, and refuses unless the
anchor names exactly one place.

**Four match levels, each looser than the last, each keeping the
uniqueness guard:** L1 exact, L2 ignoring line endings, L3 ignoring
whitespace around the anchor, L4 ignoring indentation. A model writing an
anchor from memory lands *near* the bytes on disk rather than on them;
refusing a near miss costs a turn, and matching loosely writes bytes
nobody approved, so the cascade trades one for the other a step at a time.

**L1 is reported as exact and L2–L4 as fuzzy.** After an exact match the
diff in the result is authoritative. After a fuzzy one the written bytes
may differ from what the model pictured — in Python, where indentation is
syntax, that is the difference between a fix and an ``IndentationError``
— so the result asks for a re-read instead.

**Order of operations: read, match, ask, re-read, write.** The match runs
before the approval so a human is shown the diff rather than the
arguments. The file is then read again inside the write lock; if it
changed while the prompt was open the edit is refused, because the thing
approved was a diff against particular bytes. The lock is not held across
the approval — a human's thinking time is unbounded, and holding it would
stall every other tool touching the path.

**Two guards the reference lacks.** An empty or all-whitespace anchor is
refused by name (see :func:`_reject_empty`), and a no-op edit succeeds
rather than erroring, so re-sending an edit already applied is harmless.

**Failures are the model's to correct.** A missing anchor, an ambiguous
one, a path outside the workspace or a file that is not UTF-8 all raise
and become ``is_error=True``. A no-op edit does not: the world is already
in the requested state.

``environment=None`` means the local filesystem; anything else has both
its ``read_file`` and ``write_file`` awaited. See
:class:`FileEditEnvironment`.

**What the legacy ``file_edit`` could do and this cannot.** Its schema
carries ``replace_all``, and on an ambiguous anchor it tells the model to
set it. There is no equivalent here, so N identical lines that must all
change have no anchor that disambiguates them and the edit is simply
refused — the model has to fall back to ``write_file`` with the whole
file. The reference has no such flag either, which is why porting it was
never considered, but it is a real capability loss and belongs on the
migration's list rather than in nobody's.

**A known imprecision in the error classification.** ``except OSError``
in :meth:`EditTool._get` and :meth:`EditTool._put` also catches
:exc:`TimeoutError`, which is an ``OSError`` subclass and is what
:func:`asyncio.wait_for` raises — so an ``Environment`` that bounds its
own container round-trip reports a transient infrastructure failure as
"send a different path". :exc:`asyncio.CancelledError` is unaffected.
The same pattern is in ``read.py``, ``write.py`` and ``bash.py``, so it
is a layer-wide convention to fix in one place rather than here.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools._pathlock``,
``omicsclaw.tools._workspace``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``,
``omicsclaw.tools.builtin.read``, ``omicsclaw.tools.builtin.write``, and
the standard library.

```

## omicsclaw/tools/builtin/web_fetch.py

```text
``web_fetch`` — read one URL, after asking, and never an internal one.

Modelled on harness9's ``internal/tools/web_fetch.go``. An HTTP GET whose
destination has been proved not to be internal infrastructure, whose
response is reduced to Markdown, and which asks a human first because the
request itself is the risk.

**The approval is about the request, not the response**, which is what
makes ``ASK`` right for a tool that only reads.
:data:`~omicsclaw.entry.assembly.SAFETY_RULES` rule 1 is that genetic data
never leaves this machine, and a URL is an outbound
channel: everything in the path and query string is transmitted, and a
model asked to "look this identifier up" will put the identifier in the
URL. The gate in ``omicsclaw/tools/_websafety.py`` cannot see that — to
it, ``https://example.com/?q=<patient-id>`` is a public address like any
other. So what stands between a payload and the wire is a person reading
:meth:`WebFetchTool._reason`, which shows the whole URL and says plainly
that it will be sent. By the "can this cause something irreversible"
criterion the answer is yes: bytes that have left cannot be recalled.

**World failure is not tool failure.** A 404, a 500, an unsupported
content type and a transport error are facts about the world, are returned
as ordinary text, and leave ``is_error`` false — a model told its *tool*
failed will try to fix the tool. A refused URL is the other case: the
model fixes that by sending a different one, so
:exc:`~omicsclaw.tools._websafety.UrlRefused` propagates.

**Nothing here has spoken to a real HTTP endpoint.** This environment has
no network, so every test drives an injected
:class:`~omicsclaw.tools._websafety.Transport` or a fake ``opener`` under
the real gate. It is the same debt ``docs/FRAMEWORK-REBUILD.md`` records
against the provider adapters, and the reason every borrowed literal here
carries a re-verification note.

The existing ``runtime/tools/builders/engineering.py`` ``web_fetch`` is
untouched and still checks only whether the URL starts with ``http://``.
Three things it does better, recorded rather than discovered later:

* its ``max_chars`` ceiling is **100,000**, against
  :data:`~omicsclaw.tools._html.HARD_MAX_CHARS`' 32,000. ``read_file``
  kept the same legacy ceiling for its own byte limit; this tool did not,
  and the reference's 32,000 won by default rather than by argument.
* it converts HTML with ``markdownify``, a real converter with tables,
  emphasis and nesting, where :mod:`omicsclaw.tools._html` is a fixed tag
  list. When this was written the library was a dependency of this
  repository (through extras deleted since), so the honest statement of
  the gap was not only "the standard library has no readability" but
  "this layer may not import what the old one uses".
* it sends a browser ``User-Agent``. ``OmicsClaw/1.0`` is the honest one
  and is what ships, but documentation sites behind a WAF answer 403 to
  non-browser agents, so some pages the old tool reads this one will not.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools._html``,
``omicsclaw.tools._websafety``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``, and the
standard library. Notably **not** ``builtin/bash.py``: the engine-margin
arithmetic discussed under :data:`FETCH_TIMEOUT` is asserted in
``tests/tools/test_web_fetch.py``, and importing a constant only to
mention it in prose would leave an unused name here.

```

## omicsclaw/tools/_websafety.py

```text
The network boundary, and the only way through it.

What ``_workspace.py`` is to the filesystem, this is to the network: a
gate every tool that opens a socket passes through, plus the transport
that is the only thing allowed to open one. Modelled on harness9's
``internal/tools/web_safety.go``.

**The class of bug it prevents is SSRF.** On any cloud instance
``http://169.254.169.254/latest/meta-data/iam/`` returns credentials to
whoever asks, and a model told to "check what this endpoint returns" will
ask. A URL prefix check does not prevent it; this repository's existing
``runtime/tools/builders/engineering.py`` ``web_fetch`` has only that, so
the metadata endpoint is reachable from it today.

**What this does NOT protect.** :data:`~omicsclaw.entry.assembly.SAFETY_RULES` rule 1
is that genetic data never leaves this machine, and nothing here enforces it. A
blocklist of destinations cannot: ``https://example.com/?q=`` plus a
patient identifier is a perfectly public address. Data egress is governed
by the ``ASK`` policy on the tools themselves.

**Four checks, in order:** the scheme must be ``http`` or ``https``;
userinfo (``user:pass@host``) is refused because parsers disagree about
which host ``https://expected.com@evil.com/`` names; the host is
resolved, fail-closed on failure; and **every** resolved address is
checked against :data:`BLOCKED_NETWORKS`, not just the first — a name
with one public ``A`` record and one ``127.0.0.1`` is a rebinding attack
with no timing requirement.

**Then it pins, which the reference does not.** harness9 validates the
resolved addresses and hands the *hostname* to ``http.Client``, which
resolves it again, so one record with a short TTL makes the check
describe a different connection from the one that happens.
:class:`PinnedTransport` connects to the address it validated, carrying
the hostname only in the ``Host`` header and the TLS SNI, so certificate
verification is unaffected. Redirects are re-checked on every hop.

**The seams, and which of them is load-bearing.**
:class:`PinnedTransport` takes a ``resolver`` and an ``opener``.
``resolver`` **cannot weaken a check**: whatever address it returns is
itself judged, so a test can point a name at ``127.0.0.1`` and watch it
refused but cannot make ``127.0.0.1`` acceptable. ``opener`` is a
different matter and should not be described as if it were the same —
it is the thing that opens the socket, so an implementation that ignores
the :class:`SafeTarget` handed to it reaches wherever it likes. Like
``bash``'s ``BashEnvironment`` and like a tool's own ``transport``
argument, it sits **inside** the trust boundary: whoever assembles a
registry chooses where the syscalls land and owns that choice.

**A budget for the whole call, not per hop.** ``timeout`` on
:meth:`PinnedTransport.request` covers every redirect and every byte,
because a per-hop budget multiplies by six behind a number the caller
derived from the engine's per-tool deadline — and because a socket
timeout on its own bounds one read rather than their sum, so a server
dripping bytes just inside it holds the request open indefinitely.

**Network failure is not refusal.** A reset, a TLS error or the deadline
raises :exc:`TransportFailed`, which tools report as ordinary output;
only :exc:`UrlRefused` means "send different arguments".

**Leaf-adjacent.** The standard library only — no ``omicsclaw`` import at
all, not even ``schema``.

```

## omicsclaw/tools/builtin/remote.py

```text
``remote_*`` and ``ask_about_host`` — the remote execution tool family.

C1 of the remote runtime plan: a Claude-Science-shaped execution plane,
where the remote host is asked for nothing but ``sshd`` and a POSIX
shell. The agent gets five tools — ``remote_exec``, ``remote_submit``,
``remote_status``, ``remote_cancel``, ``remote_fetch`` — plus
``ask_about_host``, which puts a question about a host to the person and
writes the answer into the host knowledge base.

**The plane is injected** (:class:`RemotePlane`), for the reason every
environment seam in this package exists: ``omicsclaw/tools/`` is a leaf
layer and may import ``omicsclaw.schema`` and nothing else inside this
namespace, while the SSH spawner, the probe parser and the SQLite stores
live in ``omicsclaw.remote``. The composition root
(:func:`~omicsclaw.entry.assembly.build_app`) binds
``omicsclaw.remote.plane.RemotePlaneBinding`` when
:attr:`~omicsclaw.entry.config.AppConfig.remote_execution` is on; a tool
constructed without a plane refuses at execution time, the same "the
surface binds it" refusal ``save_artifact``'s sink makes, because no
argument the model can send could supply a database or an SSH client.

**Hand-written like ``bash``, and for the same reason**: every tool here
is ``ASK``, and an approval prompt must quote the bytes the model
actually sent — an adapter that decodes and re-encodes cannot. The
approval *reason* names the host and the ``intent`` argument, because
"run a shell command on ``gpu.lab``" and "run a shell command" are
different decisions for the person clicking, and ``intent`` is the
model's own one-line human-language title for the card.

**Host names are validated, commands are not.** A ``host`` that matches
:data:`HOST_NAME` is an ``ssh`` config alias or a DNS name — a closed
alphabet with no space, metacharacter or leading dash, so it cannot
become an option or a second word on any ``ssh`` command line this plane
builds. A ``command`` is deliberately *not* filtered: it is a whole
programming language, the boundary claimed is the approval gate (the
same ruling ``builtin/bash.py`` states for itself), and the command
reaches the process layer as one ``argv`` element through
``exec``-style spawning, so no local shell ever re-parses it. What the
remote shell does with it is exactly what the human approved.

**A non-zero exit is a result, not a failure** — ``remote_exec`` follows
``bash``'s Q6 ruling, and for the same reason: the model's next move
after ``squeue`` answering "invalid job id specified" is to fix the call,
not to look for a broken tool. Timeouts, unreachable hosts and refused
arguments are the tool's own failures and are raised.

**Timeouts and output caps follow ``bash``'s numbers.** Output is cut in
the middle keeping two thirds of the tail (:data:`MAX_OUTPUT_CHARS`
imported rather than re-declared — one ceiling, one place), and the
``remote_exec`` budget is capped at 600 s rather than ``bash``'s 45
because the point of the tool is remote round-trips that don't fit a
local attention span; anything longer than that belongs in
``remote_submit``, which is the whole distinction between the two.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``,
``omicsclaw.tools.builtin.bash`` (for the truncation constants), and the
standard library.

```

## omicsclaw/context/__init__.py

```text
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

```

## omicsclaw/context/prompt.py

```text
``omicsclaw/context`` — sections in, one system prompt out.

Plan 0030 task B, decisions Q5 and Q5b.

**One system message per prompt, and it is message zero.** The two
adapters disagree about several system messages:
``anthropic_provider.py:262-291`` pulls them all out and joins them with
``"\n\n"``, while ``openai_provider.py:123-169`` passes each through as
its own ``{"role": "system"}`` entry. A prompt built as one message per
section would therefore *be a different prompt* depending on which
backend read it — one block on Anthropic, N entries on OpenAI. So
:func:`assemble` emits exactly one, sections joined with ``"\n\n"``:
the separator the harness uses (``builder.go:189``) and the one the
Anthropic adapter would have used anyway.

It also holds the prompt cache breakpoint still.
``apply_cache_breakpoints`` marks the **last** system message
(``openai_provider.py:212-252``); with only ever one, the mark cannot
drift from message zero because somebody added a section.

**Everything goes into the system prompt — including the volatile
parts.** The layer being replaced split sections across two placements:
stable text into the system prompt where a prefix cache could hold it,
query-dependent text riding on the user turn where churn was free. The
owner's ruling of 2026-09-18 replaced that with the harness's shape
(``builder.go:78-189``, one ``parts`` slice and one join), and the cost
is recorded rather than hidden:

* Volatile blocks — skill context, scoped memory, capability assessment,
  knowledge guidance, plans, knowhow constraints — now sit inside the
  cached prefix, so each change invalidates it. The mark
  ``apply_cache_breakpoints`` places is paid for in full every time.
* This repository has more volatile blocks than the harness, which has
  exactly one (long-term memory) and can afford it.
* The tool half of the prefix is untouched: ``registry.available_tools()``
  is still byte-stable on purpose. Only the prompt half lost its return.

Somebody looking at a low cache hit rate should read this paragraph
before changing anything — it is a decision, not an oversight.

**Order is arrival order.** The replaced layer sorted by ``(order,
key)``. The harness appends, and so does
:meth:`PromptAssembler.with_section`. Byte stability does not suffer —
arrival order is just as deterministic as a sort key — but the
responsibility moves: **a composition root must add sections in the
order it wants them**, because there is no ``order`` field to correct
them with afterwards.

**No clock lives here.** Not a ``clock`` parameter either: today's date
is external knowledge like every other, so a composition root passes
``Section(key="date", heading="", source=lambda: date.today().isoformat())``
and the injectability is the closure's, not this layer's. What is left
is a negative property — :meth:`PromptAssembler.render` never calls
``date.today()``, ``datetime.now()`` or ``time.time()`` — which is both
stronger and easier to test than a parameter would have been.

```

## omicsclaw/entry/__init__.py

```text
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

```

## omicsclaw/entry/desktop/artifacts_manager.py

```text
Capture channel ② of the artifacts plane: the output-contract scanner.

The curated skills already declare what they write in
``references/output_contract.md`` — free-form prose a person reads, built
from backticked relative paths (``​`tables/program_usage.csv`​``) and,
in the auto-generated contracts, a fenced ``output_directory/`` tree.
That file is the raw material the plan named: after a ``skill_run`` job
reaches a good end, this module parses the contract of the skill that
ran, walks the workspace for files it declares, and registers what it
finds as artifacts — **zero LLM cost**, which is why it is the first
capture channel.

The parser is deliberately a tokenizer, not a markdown parser. Three
shapes carry the declarations this repository's 88 contracts use:

* backtick spans anywhere in the text — ``​`figures/mean_program_usage.png`​``;
* fenced code blocks holding a directory tree, whose ``├──`` /
  ``└──`` lines name one file each;
* bare path-shaped words — list items (``- tables/Summary.csv: counts``)
  and prose mentions (``writes processed.h5ad, report.md, result.json``).
  The curated contracts are not uniform: a bare ``-`` list with no
  backticks is a real style, and the auto-generated ones name their root
  outputs in plain sentences.

A token survives only when it *looks like a file path*: no spaces or
brackets, a short alphanumeric suffix, relative, not a directory, no
``..``. Everything else — ``run_info``, ``obsm["X_gene_programs"]``,
``--r-enhanced``, ``figures/r_enhanced/`` — is prose about the outputs,
not a declaration, and is dropped. A declared path still registers only
when the file exists in the workspace and passes the attribution rules
below, so a prose mention of a file the run never wrote is inert.

**The fallback.** A contract that is missing, unparseable (no token
survived) or that matched nothing in the workspace turns the scan over
to the safety net: the conventional output directories the skills'
``write_output`` SDK actually creates — ``figures/``, ``tables/``,
``figure_data/``, ``results/`` — anywhere in the workspace, filtered by
extension. Artifacts registered that way carry
``meta["capture"] = "fallback"`` so a review can tell a declared output
from an inferred one. (A contract that parsed but matched nothing is
treated the same way: from the scanner's seat, "declared and absent" and
"undeclared" are both "the declaration told me nothing", and the
convention scan is what answers.)

**Idempotent by construction.** Registration goes through
:meth:`~omicsclaw.memory.artifacts.ArtifactStore.insert_artifact`, whose
``(job_id, path)`` uniqueness check makes a second scan of the same job
register nothing — the scanner itself also keeps per-scan bookkeeping so
one declaration matching two files (``figures/a.png`` declared, run wrote
``run1/figures/a.png`` and ``run2/figures/a.png``) registers each once,
in a stable order.

```

## omicsclaw/entry/session.py

```text
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

With durable events enabled, the shared memory database records admission
before lane execution. Restarted callers inspect that receipt and the existing
event log; admission never grants an automatic retry of uncertain work.

**What is not here.** No authentication or rate limiting. A sender allowlist is
:class:`~omicsclaw.entry.ingress.SenderPolicy`'s, one layer out, and it
is out there because a registry that decided who may talk to it would be
a registry every surface had to configure identically.

```

## omicsclaw/tools/builtin/save_artifact.py

```text
``save_artifact`` — capture channel ① of the P2 artifacts plane.

The explicit half: the model (or a skill calling through the tool
surface) names a file it just produced, and the tool registers that file
as an artifact so it appears in the desktop tray with a kind, a checksum
and a lineage. Claude-Science's ``save_artifacts`` is the reference; the
difference worth stating is that this tool **creates nothing** — the
file is already on disk, the tool's whole effect is one registry row.

**Hand-written like ``write_file``, and for the same reason**: the
approval prompt must quote the bytes the model actually sent, and a
decoded-and-reconstructed payload would not.

**Path fencing follows the house style**: the path goes through
:class:`~omicsclaw.tools._workspace.Workspace.resolve`, so a symlink
pointing out of the workspace, a ``..`` climb and a sensitive name
(``.env`` and friends) are all refused before anything is registered —
the same boundary ``read``/``write``/``edit`` stand on. One more rule is
enforced here to match ``/files/serve`` exactly: any path segment that
starts with a dot (``.omicsclaw/jobs.db``, ``results/.hidden/x.png``)
is refused, because the serve route refuses every hidden segment and a
registered artifact must always be a servable one.

**The registrar is injected** (:class:`ArtifactSink`), because this
layer is a leaf: it may import ``omicsclaw.schema`` and nothing else,
while the artifact registry lives in the memory layer. The composition
root (:func:`~omicsclaw.entry.assembly.build_app`) binds the sink over
the workspace's shared artifact store; a tool constructed without one
refuses at execution time with a :exc:`RuntimeError`, the same "the
surface binds it" refusal ``resolve_workspace`` makes, because no
argument the model can send could supply a database. The sink call runs
in ``asyncio.to_thread``: it hashes the file (up to the store's
half-gigabyte ceiling) before writing its row, and that read must not
pause the event loop that is serving every other HTTP and SSE client.

**Approval follows the write category**, per the P2 plan: ``HIGH`` risk,
``ASK`` mode. The blast radius is not the filesystem (nothing is
written) but the *surface*: a registered artifact is pushed at the human
through the tray, and a path the person did not mean to surface is a
leak the prompt has to catch.

**Session, not job**: the row is registered with the ``session_id`` of
the running context (:func:`~omicsclaw.tools.context.context_value`),
an empty ``job_id`` — a tool call belongs to a session, and it is the
job-plane scanner that owns job-bound rows. The ``artifact.created``
job-stream event is therefore emitted by the scanner and not by this
tool: a tool call with no job has no stream to speak on.

```

## omicsclaw/entry/approval.py

```text
``omicsclaw/entry`` — turning "ask a human" into a frame and a future.

Plan 0031 trap 1. A tool that needs consent calls
:func:`omicsclaw.tools.require_approval`, which awaits the
:data:`~omicsclaw.tools.ApprovalChannel` bound for its Task. That
``await`` happens **inside** the tool, inside
``AgentEngine.run_stream``'s ``__anext__``. The generator that would have
to announce the question is the same generator that is suspended waiting
for its answer, so no arrangement of a single consumer loop can both show
the prompt and produce the decision:

- a consumer that *polls* between engine events never sees the request,
  because the request is issued after it went back to waiting;
- a consumer that stops iterating to show a dialog stops the engine, and
  with two concurrent tools the second request never even happens.

The way out this layer takes is the first of the two
``omicsclaw/tools/context.py:136-141`` names: the engine runs in **its
own Task**, and this broker is the seam between it and whoever answers.
The request becomes an ``APPROVAL_REQUIRED`` frame on the exchange's
:class:`~omicsclaw.entry.stream.TurnStream` — which any number of
observers can be reading — plus an :class:`asyncio.Future` that a
*different* Task resolves through :meth:`ApprovalBroker.settle`.

**This channel is asynchronous**, and that is a decision rather than an
accommodation. ``ApprovalChannel`` is typed ``Callable[[ApprovalRequest],
Any]`` and ``require_approval`` awaits the result only when it is
awaitable, so a synchronous channel is legal; a synchronous broker is
not, because the whole of its job is to suspend one Task until another
one answers, and there is no synchronous spelling of that which does not
block the event loop the answer has to arrive on. :meth:`settle` is the
synchronous half, and it is synchronous for the mirror-image reason: it
is called from a click handler, an HTTP route or an IM callback, and
none of those should have to await anything to deliver one boolean.

**The deadline denies** (plan 0031 Q12). ``timeout_s=None`` waits for as
long as the exchange lives, which is what a CLI with a person in front of
it wants. A channel surface must set a number, and when that number
expires the answer is *no*: an approval that expired into consent would
be a security control that a silent user disables.

**Never logs an argument.** ``omicsclaw.entry``'s logging rule (Q22) is
at its sharpest here — an :class:`~omicsclaw.tools.ApprovalRequest`
carries the raw argument payload of the call about to run, which is
exactly the ``bash`` command line or ``write_file`` body that may name a
subject. Only the tool name, the request id and the outcome are logged.

```

## omicsclaw/entry/events.py

```text
``omicsclaw/entry`` — what one user exchange emits, with an identity.

Plan 0031, step 6 of the staged framework rebuild. Step 3
(:mod:`omicsclaw.engine`) already emits :class:`~omicsclaw.engine.types.
EngineEvent`; this module is its **superset**, and the superset exists for
three reasons that are all structural rather than stylistic:

``APPROVAL_REQUIRED`` / ``APPROVAL_SETTLED``
    The engine has no representation for "a human is being asked". It
    cannot grow one honestly either: the ``await`` that waits for a person
    happens *inside* a tool, inside the engine generator's ``__anext__``,
    so the generator that would have to report the question is the same
    generator that is blocked on its answer. The layer that binds
    ``ApprovalChannel`` is this one, so the layer that can say so is this
    one (plan 0031 Q5).

``QUESTION_ASKED`` / ``QUESTION_SETTLED``
    The same arrangement for a question a tool puts to the person through
    the question channel this layer binds.

``CONTEXT`` / ``COMPACTION``
    Both come from the compactor the engine consults before each model
    call, not from the engine: the engine does not know what a budget or
    a compaction record is. They are published by the exchange that
    supplied the compactor, so they reach the stream ahead of that model
    call's own events.

``QUEUED`` / ``GAP`` / ``EXCHANGE_*``
    Facts about *delivery*, not about the loop: a second message arriving
    while the first is still running, an observer whose cursor fell off
    the ring, and the boundary of one user round-trip.

**Two scopes, deliberately not the same word** (Q21)
------------------------------------------------------

``TURN_END`` is the engine's, and it fires **once per model call** — the
emit site is ``engine/loop.py:287``, inside the ``while`` that starts at
``:234``, so one user round-trip produces *N* of them, one per
Thought→Action→Observation cycle.

``EXCHANGE_START`` / ``EXCHANGE_END`` are this layer's, and they fire
**once per user round-trip** — exactly one each, whatever happened in
between. Pairing a new ``*_START`` with the engine's ``TURN_END`` would
have mismatched their cardinality, which is the drafting error Q21
records.

**Pass-through events carry the original** :class:`EngineEvent`, on
:attr:`TurnEvent.engine`, never a re-packed copy. Re-packing would create
a second place that has to change every time the engine's event grows a
field, and the first place would keep compiling while the second silently
dropped it.

This module is types only: no I/O, no clock, no logging. Transport lives
in :mod:`omicsclaw.entry.stream` and projection in
:mod:`omicsclaw.entry.render`.

```

## omicsclaw/engine/__init__.py

```text
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

```

## omicsclaw/entry/desktop/wire_contract.py

```text
The versioned Desktop wire contract, as ``GET /health`` publishes it.

The backend serves two kinds of route.

**Chat contract routes**, versioned here: ``POST /chat/stream`` (request
body and SSE frames), ``POST /chat/permission``, ``POST /chat/abort``,
``POST /chat/session-permission-profile``, ``GET``/``PUT /workspace``,
``GET /env/doctor`` and ``GET /health``. The backend defines this
contract and the desktop client implements the major version it names.
Optional fields and capability declarations are additive within v3. Removing
a field, changing its meaning or type, or requiring new client behavior
bumps the matching major version. Clients ignore fields they do not use
and refuse major versions they do not implement.

**Management routes**, released with the package version and not covered
by these numbers: ``GET /skills``, ``GET /skills/{domain}/{name}``,
``GET /mcp/servers``, ``GET``/``PUT /providers``, ``POST /providers/test``,
``POST /chat/title`` (whose request body carries its own
``schema_version``), the read-only file routes ``GET /files/tree`` and
``GET /files/serve``, and the remote-execution host routes
``GET``/``POST /remote/hosts`` and ``DELETE /remote/hosts/{alias}``, plus
Backend-owned alias suggestions and read-only target file routes.
(C1's REST face over the host knowledge base — additive, and answered
``503 remote_execution_disabled`` where no remote plane is bound, so a
client gates on ``capabilities.remote_execution``).

* ``request_schema_version`` — the ``/chat/stream`` request body. The
  body must carry ``ingress_schema_version`` equal to it; a missing or
  different value is refused with 422. Version 3 adds ``after_seq`` (the
  sequence number to resume after) and ``resume`` (reattach to the
  exchange a ``source_request_id`` started, and never start one).
* ``sse_schema_version`` — the frames of ``/chat/stream``, the request
  and response of ``/chat/permission`` and of
  ``/chat/session-permission-profile``, and the ``/env/doctor`` payload.
  Version 3 puts an ``id:`` line on every frame an event produced, except
  the frames that end the stream, and sums ``result.usage`` over the
  whole exchange rather than over one connection.
* ``interrupt_schema_version`` — the ``/chat/abort`` request.

``abandon_grace_s`` is how long an exchange keeps running once nobody
observes it, ``None`` when this backend never cancels one for that. It
is a fact about the running process rather than about the wire, so it is
not versioned.

:data:`SERVED_PATHS` names the routes
:func:`~omicsclaw.entry.desktop.server.create_desktop_app` mounts.

```

## omicsclaw/entry/stream.py

```text
``omicsclaw/entry`` — one exchange's event truth, observable more than once.

Plan 0031 Q14. The drafted design had a single ``TurnHandle`` that was the
exchange's identity *and* its only consumer *and* the approval entry point.
Two of the three Surfaces break that shape immediately:

- **Desktop.** An SSE reconnect is a *new HTTP request*. It cannot be
  handed the iterator the previous request was holding, and the frames
  that request already consumed are gone.
- **Channel.** A failed IM delivery is retried, which means replaying from
  a cursor.

So identity and observation are separated here: one :class:`TurnStream`
per exchange, any number of :class:`TurnObservation` over it, each with
its own cursor. The vocabulary is deliberately the deleted control
plane's — ``sequence``, a bounded ring, ``cursor_evicted``,
``EventObserverDetached`` — because that code had already solved
cross-process streaming delivery, and a rebuild that renamed the concepts
would have had to re-derive them (``git show
HEAD:omicsclaw/control/event_hub.py``).

**What is new, and why** — three departures from that implementation, each
because the old behaviour has a failure mode this layer must not inherit:

1. *A slow observer is no longer detached.* The old hub dropped an
   observer whose queue filled up (``_DETACHED``). Here the **droppable**
   types are discarded instead and the observer is told, because a Desktop
   client on a slow link should lose text tokens, not the approval prompt
   that its user is expected to answer.
2. *A gap does not throw the retained suffix away.* The old hub skipped
   replay entirely on a gap, on the grounds that the retained suffix was
   incomplete. Once a ``GAP`` frame names the hole the suffix is no longer
   ambiguous, and discarding frames the process is still holding buys
   nothing.
3. *A cursor past the end is not an error.* ``cursor_ahead`` made sense
   against a durable log. Against a live in-process stream it just means
   "caught up", so the cursor is clamped and no gap is reported.

**Concurrency: Tasks, not threads.**
:meth:`TurnStream.publish` is safe between Tasks of **one** event loop and
is *not* thread-safe — neither ``asyncio.Queue`` nor a ``deque`` plus an
``asyncio.Event`` is, whatever the reference harness's goroutine-safe
``p.Send`` may suggest (plan 0031 trap 10). A callback running on a vendor
SDK's own thread — the normal case on the Channel Surface — must go
through :meth:`TurnStream.publish_threadsafe`, which is the
``loop.call_soon_threadsafe`` hop spelled out.

```

## omicsclaw/remote/ssh.py

```text
Spawning the system ``ssh`` and ``sftp`` — and nothing else in between.

The transport half of C1. The remote runtime plan's ruling is Claude
Science's: the remote host is asked for **sshd and a POSIX shell**, so
the client side should be the system's own OpenSSH — not a pip SSH
library. Three reasons, all bought by somebody else's failures:

* **Zero new dependencies.** A pip SSH stack (paramiko and friends) is a
  wheel matrix, a cryptography build and a CVE feed this project would
  own forever, for capability ``/usr/bin/ssh`` already has.
* **The user's config is the contract.** ``~/.ssh/config`` aliases,
  ``ProxyJump`` chains, agent forwarding policy and known hosts all
  resolve exactly as they do in the user's terminal. An in-process
  client re-implements a subset of that, differently.
* **Host keys stay the system's business.** Nothing here relaxes
  ``StrictHostKeyChecking`` or writes a fresh ``known_hosts``; the
  security posture of a remote run is the posture of the user's own
  ``ssh`` invocation, no better and no worse.

**Authentication is key- or agent-based only.** :data:`SSH_OPTIONS`
pins ``BatchMode=yes``, so ``ssh`` fails rather than prompting for a
password, and no password parameter exists anywhere in this package.
That is the plan's explicit "not doing" and this is where it is enforced.

**Construction and execution are separate layers**, so the construction
half can be tested without a network: :func:`build_ssh_argv` and
:func:`build_sftp_argv` return ``argv`` lists — pure functions — and
:class:`SystemSshSpawner` is the only place a process starts. A test
double for the spawner is a dict of canned outcomes; the real one writes
output to a temporary file rather than a pipe, for the reason
``builtin/bash.py`` measures at length: a remote command that
backgrounds a grandchild holding the write end of a pipe makes ``wait``
block on something that is not the command.

**Injection is bounded twice over.** A ``host`` is validated against a
closed alphabet (:func:`validate_host`) *and* the ``argv`` puts ``--``
before the destination, so a value that slipped the alphabet could not
become an option. A ``command`` is never filtered — it is a whole
programming language and the boundary is the approval gate, the ruling
``bash.py`` states for its own single argument — but it travels as one
``argv`` element to an ``exec``-style spawn, so no *local* shell ever
re-parses it; what the remote shell does with it is what the human
approved.

```

## omicsclaw/permission/rules.py

```text
The permission rule file: reading it, matching against it, writing it back.

Rules say what this deployment has decided about individual tool calls, at a
finer grain than a tool name. A rule file looks like this::

    {
      "permissions": {
        "deny":  ["bash(rm -rf *)", "write_file(/etc/*)"],
        "allow": ["read_file", "bash(git *)", "bash(python -m pytest*)"],
        "ask":   ["bash(pip install*)"]
      }
    }

**Pattern syntax.** Two forms, and no others:

``toolName``
    Matches that tool whatever its arguments.

``toolName(pattern)``
    Matches that tool when *pattern* matches its principal argument. An empty
    ``toolName()`` matches any arguments, like the bare form.

**What a pattern is matched against** is one string, not the whole argument
payload: the value of the tool's first *required* ``string`` property, taken
from the tool's own JSON Schema — ``command`` for ``bash``, ``path`` for the
file tools, ``url`` for ``web_fetch``, ``query`` for ``web_search``. See
:func:`principal_argument`, including what happens when a tool has no such
property. Writing a pattern against the wrong thing is the easy mistake here:
``write_file(*/tmp/*)`` is a statement about the destination path and says
nothing about the content being written.

**Matching.** A pattern containing none of ``*?[`` must equal the principal
argument **exactly**; a pattern containing one of them is a :mod:`fnmatch`
glob over the whole string. There is no substring fallback, so ``bash(ls)``
does not match ``ls -la`` — write ``bash(ls*)`` for that. ``*`` crosses any
character including ``/``, so ``bash(git *)`` matches
``git log -- a/b/c.py``.

**Matching is case-sensitive**, unlike :mod:`omicsclaw.permission.danger`.
A rule naming a tool in the wrong case names a tool that does not exist,
because the registry keys tools exactly; and a case-insensitive ``allow``
would grant more than its author wrote.

**Evaluation.** First match wins, and ``deny`` rules are loaded ahead of
``allow`` ahead of ``ask``, so the key order inside the file does not matter
and a ``deny`` always outranks an ``allow`` that would also have matched. A
call no rule mentions returns ``None`` — not ``ask`` — which is what lets a
caller fall through to further checks of its own.

**A file that cannot be read as written raises** rather than being partly
applied: an unknown action key, a malformed pattern, a scalar where a list
belongs. A *missing* file is an empty rule set and not an error.

```

## omicsclaw/skills/discovery.py

```text
Skill discovery: which skills this turn's request is probably about.

The catalogue in the system prompt is the *map* — every name, under
``compact`` nothing else — and this module is the crossing service: a
lexical retrieval over the same
:class:`~omicsclaw.skills.index.SkillIndex`, run once per model call
against the request that call is about, whose top few hits are appended
to what the model reads. It exists because a flat list the model must
scan itself taxes every turn in proportion to the catalogue's size,
while a query costs the same at 88 skills as at 200.

**Why lexical, and why that is admitted rather than hidden.** BM25 over
(name ×3, domain ×2, tags ×2, triggers ×2, description ×1) with
reciprocal-rank fusion against a substring ranking is blind to
synonyms — "cell typing" will not find a skill that says "annotation"
and nothing else. A reranking model would close that gap and is
deliberately not here: it would add a second model call to every turn
of a product whose cost this module exists to lower. The blind spot is
handled the honest way instead — a low-confidence block that says so
and points at ``search_skills``, where the model can spend its own
understanding of the domain — by tags and triggers, which are the
cheap human patch for exactly this failure, and by the weak-term gate
(``_WEAK_TERMS``), which keeps session-and-deliverable vocabulary from
dressing a non-domain request up as one.

**Why the engine is never imported.** :class:`SkillDiscovery` satisfies
the engine's ``TurnAugmentor`` protocol structurally, the way
:class:`~omicsclaw.context.MemoryNudge` does, and imports nothing from
``omicsclaw.engine`` — or from ``omicsclaw.observability``, which is
why the tracer also arrives structurally: the recorder's
``start_span(name, *, parent, attributes)`` shape is taken as a local
protocol and the default is a no-op defined here, so the leaf-layer
import rule keeps holding and a deployment that never configured
telemetry runs the whole chain without a branch.

**State is re-derived, never held.** The set of skills already loaded
is re-read from the conversation's ``use_skill`` calls on every call,
so a compaction that summarizes those calls away shrinks the set and
the skill becomes discoverable again — which is the correct behaviour,
not a leak: the instructions are no longer in the context either.

```

## omicsclaw/planning/__init__.py

```text
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

```

## omicsclaw/permission/gate.py

```text
Where a tool call is permitted, refused, or put to a person.

:class:`PermissionGate` decides; :class:`GatedTool` acts on the decision.

**The gate decorates a tool, not the registry.** :func:`gate_tools` wraps each
:class:`~omicsclaw.tools.Tool` that a registry is built over, so the gate runs
*inside* :meth:`~omicsclaw.tools.ToolRegistry.execute` and reads the policy
that registry already resolved. Two consequences for a caller:

- Nothing wraps the registry, so the optional Protocols the engine probes for
  (``DeadlineAwareExecutor``, ``ConcurrencyAwareExecutor``) keep working — in
  particular a human's approval time still stays out of the per-tool timeout.
- **A tool registered without passing through :func:`gate_tools` is not
  gated.** Composing the registry is where that has to be got right.

**Who asks a human.** A verdict of ``ask`` does not mean *the gate* asks. A
tool that declares :attr:`~omicsclaw.tools.ToolPolicy.prompts_for_itself` puts
the question itself, with a prompt that can describe the actual effect — the
diff ``edit_file`` is about to write, the whole URL ``web_fetch`` is about to
request. The gate asks in every other case, and in two cases even when the
tool claims otherwise:

- the tool would self-approve this time, because its resolved
  ``approval_mode`` is ``AUTO`` and a rule escalated the call;
- a dangerous-command pattern matched, where the pattern's reason is
  something the tool does not know and a person needs to read.

Either way a call is put to a human **at most once**: after the gate asks, it
runs the tool with consent already recorded, so the tool's own
``require_approval`` returns immediately.

**Which questions a standing grant may answer.** A surface can offer "stop
asking me about this tool" (the CLI's ``s``). That is only safe for questions
the tool's *own default* raised. On both paths above —— the gate asking, and
the gate handing the question down —— the gate binds
:func:`~omicsclaw.tools.ask_every_time` to say whether anything more specific
than that raised this one: a rule, a dangerous-command pattern, a protected
path. It has to be both paths, because a rule outranks a danger pattern: an
``ask: ["bash"]`` rule turns ``rm -rf /`` into a *rule* question, which is
handed down to ``bash`` like any other (plan 0049 §3).

```
