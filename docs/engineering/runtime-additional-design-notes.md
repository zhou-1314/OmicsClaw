# Additional runtime design notes

These module introductions are preserved from the S4d source snapshot. They describe historical design decisions; current behavior is defined by the code and current specifications. The archived literals below are unchanged. Module intros now contain a brief responsibility statement and this document path.

## omicsclaw/context/compaction.py

Source SHA-256: `143f1f27ac12fe09c3cb21d8b8eaa301ff15d3fa9fd6ab33e5e02faefa6633ff`.

``````````python
"""``omicsclaw/context`` — deciding what to drop, and writing it down.

One compaction, as a function: :func:`compact` grades the conversation,
offloads, cuts head from tail, summarizes and reports, taking the
previous :class:`CompactionState` as a parameter and returning the next
one. It keeps no state and decides nothing about what happens to its
result.

Carrying that state from one model call to the next, and deciding
whether a result replaces the history, is
:class:`~omicsclaw.context.progressive.ProgressiveCompactor`'s — one
instance per run, so concurrent conversations never share it. Keeping
the state between exchanges, persisting records and writing offloaded
text are the caller's.

**Three tiers do three different things**, which is the seam worth
watching: an implementation that routed every pressure level to the same
treatment would pass any test that only checked which
:class:`~omicsclaw.context.budget.Pressure` came back.

=============  =============================================================
``NONE``       nothing, byte for byte
``WARN``       offload oversized tool results; without an offloader, nothing
``SOFT``       summarize the older half of the head, keep the rest verbatim
``FULL``       summarize the whole head, keep ``min_tail`` behind it
``EMERGENCY``  truncate greedily, **never** call the summarizer
=============  =============================================================

When an :class:`~omicsclaw.context.offload.Offloader` is supplied,
:func:`compact` first moves oversized tool results out of the compactible
head at every tier above ``NONE``, then grades the conversation again and
applies the tier it is now in — offloading alone may be enough.

**Only one pass.** The replaced layer compacted to a target ratio and
iterated up to three times to converge, so that one compaction cost one
cache re-warm. Its premise does not hold here — that design assumed the
system prompt and the summary moved together, and in this package
assembly and compaction are two separate functions. One pass, as the
harness does; the total may still sit above target afterwards and the
next turn compacts again.
"""
``````````

## omicsclaw/context/sections.py

Source SHA-256: `9f3e743ac6bca3a3e037c6ec9342ac4ff3438bdefacf0c2ca07a6f8ff5a2c657`.

``````````python
"""``omicsclaw/context`` — the vocabulary a system prompt is built from.

Plan 0030 task B. A :class:`Section` is one titled block of the system
prompt and, more importantly, one place where knowledge this package is
not allowed to have gets in.

**Every source is a callable, and it is called on every render.** The
reference harness takes a function for exactly one of its blocks —
long-term memory, ``builder.go:56-59`` — with the reason written beside
it: ``memory_write`` rewrites ``MEMORY.md`` *while the agent runs*, so a
snapshot taken at construction would have the agent reasoning from the
version before the one it just wrote. That argument is not special to
memory here. ``create_omics_skill`` adds skills mid-run, ``edit_file``
rewrites ``AGENTS.md`` mid-run. So it is generalised: no section holds a
snapshot, all of them are closures, and "what was written is visible on
the next turn" is a property of the shape rather than a mechanism
someone has to remember to trigger.

**That generalisation has a price, and it is charged in cache.** See
:meth:`~omicsclaw.context.prompt.PromptAssembler.render`.

**Exceptions from a source are not caught, and nothing here logs.** The
layer being replaced wrapped every block in fail-closed error handling
with a warning. Three reasons it is gone: this is a leaf package and the
engine beside it already settled "no I/O, no logging"; a
:data:`SectionSource` is an *arbitrary callable* — not the harness's one
fixed ``os.ReadFile`` — and swallowing arbitrary exceptions is a Python
anti-pattern; and the failure mode it produced was the wrong one. A
prompt file that fails to read turning quietly into "the prompt lacks
that section" is more expensive than a crash, because nobody finds out. The
consequence is real and belongs to the caller: wrap a source that may
fail if a degraded prompt is genuinely better than no prompt.
"""
``````````

## omicsclaw/context/summary.py

Source SHA-256: `cb7a4d2526bb392531cfa099f108deb751123a414a65ef659f795a4af521ad74`.

``````````python
"""``omicsclaw/context`` — anchors, and the text that carries them.

Plan 0030 task C. Pure text: the five anchors a compaction preserves,
the parser that recovers them from whatever a model actually wrote, the
templates that ask for them, and the marker that says "this message is a
compaction". All of it ported from ``internal/memory/anchor.go`` and
``progressive_compactor.go:26-65``.

It is its own module for cohesion rather than for size. Anchors and
templates are a self-contained text transform that can be tested without
a budget, a plan or a model anywhere in sight; folded into
:mod:`~omicsclaw.context.compaction` they would turn that file into the
1,200-line compactor this rebuild exists to leave behind.

**There is no gate on what a summarizer returns.** The replaced layer
had six — empty, over-length by tokens, by characters, by bytes,
looks-like-a-tool-call, and a content-fidelity check that re-read the
source material for file paths and ``Error``/``Traceback``/``TODO``
markers and rejected a summary that had dropped one. The harness has
none: ``progressive_compactor.go:423-431`` checks that the call did not
error and did not return nil, and then parses whatever came back. The
owner's 2026-09-18 ruling takes the harness's shape, and **the cost is
that a compaction can lose the path to an ``h5ad`` file and nothing
will notice**. What is left in its place is the degradation path in
:func:`~omicsclaw.context.compaction.compact`, which must therefore
actually hold — see plan 0030 §11.A-9, the debt this makes most worth
repaying. It holds in two places, and both are budget arithmetic rather
than judgements about prose: the fallback trims to a *stricter* target
than the tier that triggered it, and ``compact`` re-measures its own
output against ``usable_tokens`` before handing it back.

**Do not invent a replacement gate.** "Reject a summary longer than the
template" needs a template to measure against, and the
deterministic-template path is exactly what was removed; reintroducing
a yardstick is reintroducing the path. The budget re-measurement is not
that: it compares the result to the number this package already divides
by, asks nothing at all about what the summary says, and is the literal
text of task C acceptance 3.
"""
``````````

## omicsclaw/context/transcript.py

Source SHA-256: `75c101752379a3cadf71700336040496f98d823764cc00c74ba5488a9b95e5e2`.

``````````python
"""``omicsclaw/context`` — message lists in, message lists out.

Plan 0030 task C. Pure transforms over a conversation: no clock, no
I/O, no model. Everything here is ``(messages, budget) -> messages``,
which is what makes the whole compaction story testable without
inventing half a session to test it in.

**Every return value is a tuple, and none of them is the caller's
list.** The reference harness has a scar here: issue #117 was an
aliasing bug, and ``progressive_compactor.go:354-360`` still carries the
defensive copy and the explanation. Python looks safer than it is —
:class:`~omicsclaw.schema.Message` is frozen, so no single entry can be
edited behind a caller's back — but a returned *slice* of the caller's
list is the same bug in a different spelling: one ``append`` and two
conversations are one conversation.

**Nothing here truncates a message.** Capacity is solved at the
granularity of whole messages — :func:`fit_to_budget` drops the oldest
ones first, :func:`emergency_fit` skips the ones that do not fit — never
by shortening one. In particular
:attr:`~omicsclaw.schema.ToolCall.arguments` is never rewritten: ADR
0077 requires it to stay the unparsed JSON text the model emitted, byte
for byte, because prompt-prefix caching and replay evidence both read
those bytes. A truncated argument payload is also an *invalid* one,
which the model will be handed and will retry against forever.

:func:`drop_unanswered_calls` is the one function here that edits a
message. It takes whole tool calls off a turn when no tool result
answers them, and it leaves the text and the arguments of what it keeps
as they were. A tool result with no call, and an answered call whose
arguments do not parse, are still there afterwards.
"""
``````````

## omicsclaw/engine/config.py

Source SHA-256: `db6447928503119edc44e4b7aa4ee1c7a4be8a75c34a05c63b2cd27940c3818a`.

``````````python
"""``omicsclaw/engine`` — how one run is budgeted.

Plan 0027 §3. The sibling of
:class:`~omicsclaw.provider.config.ProviderConfig`, and deliberately
disjoint from it: that record holds what a *model call* needs — model,
temperature, output ceiling, timeouts — while this one holds what a
*loop* needs, which is entirely a matter of budgets. Nothing here reaches
a prompt and nothing here names a vendor.

One difference from its sibling is load-bearing. ``ProviderConfig`` is
resolved from the ambient shell, because an API key has nowhere else to
live. :class:`EngineConfig` reads **nothing**: a caller constructs one
and passes it in. A turn ceiling that changed with whichever shell
launched the process would make two runs of the same benchmark task
incomparable, and the resulting failure — a task that stalls on one
machine and completes on another — looks like a model regression rather
than like configuration.

The reference harness spells this as sixteen ``With…`` functional
options mutating the engine in place. A frozen record plus
:meth:`EngineConfig.with_overrides` says the same thing without letting a
live engine's budget be edited underneath a running loop.

**Leaf-adjacent.** ``omicsclaw.schema`` and the standard library only —
and in this module, only the standard library.
"""
``````````

## omicsclaw/engine/conversation.py

Source SHA-256: `752c03eb7405b02677bd63d4d5ad9018d1ff1e93737405c9492f2b76f2d7257b`.

``````````python
"""The seam through which an exchange reads its history and hands it back.

Plan 0027 §12.4.2. The loop still owns no conversation: a
:class:`Conversation` is read at the start of an exchange and written at
the end of it, and between those two points the trajectory lives where it
always did, in the run itself.

**``commit`` replaces; it does not append.** The temptation is an
``extend`` that takes only what this exchange added, and it is wrong for
a reason that shows up nowhere until a conversation gets long enough to
compact. A compactor that keeps its rewrite replaces the *run's* history
(``loop.py:259-260``), so the messages it folded away no longer exist in
the trajectory — only the summary does. An append-only seam would write
the folded-away originals back into storage and leave the summary in the
:class:`~omicsclaw.engine.types.RunResult` alone, where the next exchange
never sees it: compaction would then work perfectly within one exchange
and not at all between two, which is the only place it was needed.
Replacement has no such failure mode, and it is what every implementation
of this idea in reach already does.

**The engine strips its own system message.** It put one there, so it
takes it back out rather than making every implementation remember to.

**Storage is not this seam's business.** ``commit`` is handed a
trajectory, not a transaction: an implementation that persists is free
to, and one that only holds the conversation in memory — leaving a
registry to decide when a session reaches a disk — is the arrangement
this was written for. Nothing about compaction *state* crosses here
either; that belongs to whoever configured the compactor.

This module imports :mod:`typing` and :mod:`omicsclaw.schema`, and
nothing else.
"""
``````````

## omicsclaw/engine/loop.py

Source SHA-256: `f709e32c91d9f9bd6aed60dc8bd70048b9531621090029d1a4cdea72ef1ac412`.

``````````python
"""``omicsclaw/engine`` — the ReAct loop itself.

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
"""
``````````

## omicsclaw/engine/prompt.py

Source SHA-256: `9168122d155b89127a6e441697d533d1863510e34579fa02bda47aaf8b429878`.

``````````python
"""The seam through which the loop asks for the prompt it opens with.

Plan 0027 §12.4.1. The loop has never assembled a system prompt and
still does not: what changes is the direction of the arrow. Until now a
caller rendered one, spliced it in front of a conversation and handed the
result to :meth:`~omicsclaw.engine.loop.AgentEngine.run`; an engine given
a :class:`PromptSource` asks for the render itself, once per exchange,
and so owns the one ordering rule that matters — the system message is
message zero and there is exactly one of it.

**Two Protocols rather than one method returning ``str``.** The narrower
spelling — ``system_prompt() -> str`` — would force every renderer worth
using through an adapter, and the adapter would drop everything the
render carries besides the text: section statistics, token estimates, the
answer to "which block is eating the window". Those belong to whoever
rendered them, and a layer that cannot read them should also not be able
to destroy them. Returning the renderer's own object passes them through
untouched, and lets a structural implementation satisfy this seam with no
adapter at all.

This module imports :mod:`typing` and nothing else — not even
:mod:`omicsclaw.schema`. A prompt is text until the engine makes it a
message, and that conversion is the engine's business.
"""
``````````

## omicsclaw/engine/retry.py

Source SHA-256: `6a0f82099afba9eae0ef21633d0eacb24935ffdc9e4d227d55571cd3a50ac6e4`.

``````````python
"""``omicsclaw/engine`` — the model call that survives a bad minute.

Plan 0027 §3, and trap 12. A vendor SDK retries what fails before the
first byte; everything after it — a proxy that drops a half-streamed
response, a 5xx that arrives once the headers are already out — escapes
to this layer, which is where the legacy loop killed the run and threw
the trajectory away. One transient hiccup should not cost an agent its
work.

**Two budgets, and neither ever borrows from the other.** Transport
failures (TLS, DNS, connection establishment) are far more intermittent
than a 5xx, and the reference harness found that one budget could not
absorb both: three benchmark tasks hit the same x509 error on turn 1 and
abandoned the turn once ~3s of backoff was spent. So a transport failure
draws on ``network_retries`` and anything else on ``generate_retries``,
and exhausting one leaves the other untouched. (That failure reaches
*this* runtime spelled differently — see :data:`_TRANSPORT_MARKERS`,
where believing otherwise is what once made the wider budget
unreachable.)

**Classification is structural here, and textual in the harness it comes
from.** That harness matches substrings of the error message because
Go's SDK wrappers lose the concrete type. We have
:attr:`~omicsclaw.provider.ProviderError.status_code` instead: 429 and
5xx are worth another attempt, while any other 4xx is a request the model
must *fix* rather than one worth repeating. The substring check survives
only as the fallback for ``status_code is None`` — which is exactly the
"never reached the wire" case step 2 designed that ``None`` to mean, and
the strings it looks for have to be the ones our own adapters produce
rather than the ones that harness was reading.

**Leaf-adjacent.** ``omicsclaw.provider`` and the standard library.
"""
``````````

## omicsclaw/engine/types.py

Source SHA-256: `8411483a74b62b0c249532902841c7653d05843fc7e13944e69ec6e712d27a45`.

``````````python
"""``omicsclaw/engine`` — what one ReAct run returns, and what it emits.

Plan 0027, step 3 of the staged framework rebuild. Step 1
(:mod:`omicsclaw.schema`, ADR 0077) gave the system a vocabulary and step
2 (:mod:`omicsclaw.provider`, plan 0026) gave it a way to speak; this
layer is the loop that drives

    Message → ToolCall → ToolResult → Message

to convergence. The types here are that loop's two outputs:
:class:`RunResult`, what a blocking run returns, and :class:`EngineEvent`,
what a streaming run yields.

Three names were drafted in step 1 and then removed again. This is the
step that owns them, and the answer is **one of the three**:

``StopReason``
    Kept. A run has three ways to stop and only an error string told them
    apart in the reference harness; an enum is the cheapest way to let
    "did it finish, or did it run out of room" be answered by data.

``Trajectory``
    Dropped. The trajectory *is* the ``tuple[Message, ...]`` on
    :class:`RunResult`. Wrapping a list of messages in a type pays for
    itself only when the list needs annotating, and nothing needs that
    yet.

``AgentStep``
    Dropped. A per-turn record's only plausible consumer is persistence,
    which is step 6. Fixing its shape before the layer that stores it
    exists is precisely the mistake step 1 avoided by removing it.

**Leaf-adjacent.** ``omicsclaw.schema`` and the standard library, nothing
else. No I/O, no logging, no vendor SDK.
"""
``````````

## omicsclaw/entry/assembly.py

Source SHA-256: `a4ff3b488859bd62ad853065a81636b10445fe3c6ffaa85b2bdce2551e683659`.

``````````python
"""The composition root: five layers, wired once, by one function.

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
and :func:`~omicsclaw.skills.search_skill_tool`, and
:func:`default_sections` adds a skill catalogue section.
``AppConfig.skills_index`` is the switch, and ``off`` drops all three. The
prompt is the contract, safety rules and tool guidance, then the
optional planning, execution sandbox and skills sections, the
environment, and the optional long-term memory block;
:func:`default_sections` documents when each optional one appears.
"""
``````````

## omicsclaw/entry/desktop/__init__.py

Source SHA-256: `660042eea2de4138f2e6c9345b5a02840a87f4145cb56a549410c791e9077ae2`.

``````````python
"""``omicsclaw.entry.desktop`` — the Desktop Surface's HTTP routes.

The client on the other side is a **separate project**, ``OmicsClaw-App``
(Electron + Next.js), whose server-side proxy forwards to this backend.
The backend defines the wire contract and versions it
(:mod:`~omicsclaw.entry.desktop.wire_contract`); the client implements the
version ``GET /health`` publishes.

Routes: ``POST /chat/stream``, ``POST /chat/permission``,
``POST /chat/abort``, ``POST /chat/session-permission-profile``,
``GET``/``PUT /workspace``, ``GET /env/doctor``, ``GET``/``HEAD
/health``, and the management routes ``GET /skills``,
``GET /skills/{domain}/{name}``, ``GET /mcp/servers``,
``GET``/``PUT /providers``, ``POST /providers/test`` and
``POST /chat/title``, and the read-only file routes ``GET /files/tree`` and
``GET /files/serve``
(:data:`~omicsclaw.entry.desktop.wire_contract.SERVED_PATHS`).

:class:`~omicsclaw.entry.desktop.providers.DotenvSettings` is the
deployment's ``.env`` as ``create_desktop_app(settings=...)`` takes it;
the process shell builds it.

**Importing this package never imports a web framework.** ``fastapi`` is
imported inside :func:`~omicsclaw.entry.desktop.server.create_desktop_app`,
so the wire contract, the request bounds and the frame projection are all
readable and testable where no server is installed.
"""
``````````

## omicsclaw/entry/desktop/catalog.py

Source SHA-256: `c7a5f6a493a8445f49cf5e06d4a5d7847f38a12731b4c8cd98afb40a82ab35c9`.

``````````python
"""``GET /skills``, ``GET /skills/{domain}/{name}`` and ``GET /mcp/servers``.

Read-only views of what the running :class:`~omicsclaw.entry.AgentApp`
loaded at start-up: the skill index and the MCP configuration with each
server's connection state. Nothing is rescanned; a change on disk shows
after a restart.

The catalog payload::

    {"domains": [{"domain": "spatial", "domain_name": "spatial",
                  "primary_data_types": [],
                  "skills": [{"name": "spatial-de", "description": "...",
                              "domain": "spatial", "collection": "curated",
                              "status": "ready"}]}],
     "total": 94}

A skill with no domain directory is listed under :data:`GENERAL_DOMAIN`.

One skill::

    {"name": "spatial-de", "domain": "spatial", "description": "...",
     "aliases": [], "script_path": null, "tags": ["..."],
     "skill_md": "<SKILL.md without its frontmatter, or null>",
     "resources": [{"path": "scripts/run.py", "kind": "script"}]}

``resources`` holds paths relative to the skill's directory only.

The MCP payload is ``{"servers": [...]}``, configured servers in file
order and then rejected entries. ``env`` and ``headers`` carry their keys
with every value replaced by :data:`MASK`; ``command``, ``args`` and
``url`` are the text written in ``.mcp.json`` before ``${VAR}``
expansion, and are left out when that file can no longer be read. An
``error`` has the values of the start-up environment replaced by
:data:`MASK` and is cut to :data:`MAX_ERROR_CHARS`.
"""
``````````

## omicsclaw/entry/desktop/doctor.py

Source SHA-256: `449c629baf1b64212008e3649f299f31ab8294798efa76c7b573160862d69ea7`.

``````````python
"""``GET /env/doctor``: what this running backend was assembled with.

:func:`doctor_report` reads facts off the live
:class:`~omicsclaw.entry.AgentApp` — the provider and the model it calls,
the skill index, the MCP servers, the sandbox, memory, the permission
mode and the workspace — and answers them as a checklist. Nothing is
probed from scratch: every fact is what start-up already produced.

The payload::

    {
      "generated_at": "2026-09-28T08:00:00Z",
      "workspace_dir": "/path/to/project",
      "omicsclaw_dir": "/path/to/project",
      "overall_status": "ok" | "warn" | "fail",
      "failure_count": 0,
      "warning_count": 0,
      "checks": [
        {"name": "provider", "status": "ok" | "warn" | "fail" | "info",
         "summary": "...", "details": ["..."]},
        ...
      ]
    }

``omicsclaw_dir`` is the same value ``/health`` reports. A check is
``warn`` or ``fail`` only for something a person should act on; a
deliberate configuration that is merely off (no sandbox, no memory) is
``info``.

:func:`effective_model` is the model the provider calls, which is also what
``/health`` and the ``result`` frame report.
"""
``````````

## omicsclaw/entry/desktop/files.py

Source SHA-256: `669a2e635e1d72efbc6ad30ef511023e127b509a9488c9770751db7237fa54c7`.

``````````python
"""``GET /files/tree`` and ``GET /files/serve``: read-only views of the workspace.

Both routes take a ``path`` that is absolute or relative to the workspace
and answer only for what lies inside it. ``~`` is not expanded, and
``..`` is collapsed on the spelling before any symlink is followed.

* A path outside the workspace, as written or once its symlinks are
  followed, is refused with 403 ``path_outside_workspace``.
* A path with a segment below the workspace that starts with ``.``, as
  written or once its symlinks are followed, is refused with 403
  ``hidden_path``. The directories above the workspace do not count.
* A missing path is 404 ``file_not_found`` or ``directory_not_found``; a
  path through a directory the backend may not search, or a file it may
  not read, is 403 ``permission_denied``; a path with a NUL or one that
  cannot be resolved is 422 ``invalid_path``.

The tree payload::

    {"root": "/ws/data", "truncated": false,
     "tree": [{"name": "fig", "path": "/ws/data/fig", "type": "directory",
               "children": []},
              {"name": "a.csv", "path": "/ws/data/a.csv", "type": "file",
               "size": 12, "extension": "csv"}]}

``root`` and every ``path`` are the requested directory joined with the
entry names as written, so the entries under a symlinked directory keep
the link's name. Directories come first, then names in case-insensitive
order. ``extension`` has no dot and is absent when the name has none. A
directory at the depth limit, or whose listing cannot be read, has
``children: []``. Hidden entries, entries whose real path is outside the
workspace or hidden, broken links, and the directories in
:data:`IGNORED_DIRECTORIES` are left out, and so is a directory link back
to one of its own ancestors. The walk is breadth-first and stops after
:data:`FILES_TREE_MAX_NODES` nodes with ``truncated: true``.

A served file is sent whole when it is at most
:data:`FILES_SERVE_MAX_BYTES`; a larger one needs a ``Range`` request.
See :func:`byte_span` for the ``Range`` rules and :func:`served_media_type`
for the ``Content-Type``.
"""
``````````

## omicsclaw/entry/desktop/interactions.py

Source SHA-256: `5d4887bd47c24bef2fa6700c31571c208bfdf270f44d3fc8b7f32de836675932`.

``````````python
"""Process-local state behind the Desktop return channels, and their logic.

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
"""
``````````

## omicsclaw/entry/desktop/jobs_manager.py

Source SHA-256: `3780bd4b012acfa1a678000215199fc91415bb522d6362db2a1c4d2287d87e68`.

``````````python
"""The Desktop jobs plane: a manager, a store, and runner plugins.

P1 of the front-back plan. A *job* is one unit of work the desktop client
started on purpose — today ``kind="skill_run"``, a direct call into a
skill's ``_api.py`` entry function with no model in the loop. The manager
owns each job's asyncio task, its lifecycle events and its approval gate;
the routes in :mod:`~omicsclaw.entry.desktop.jobs` are a thin adapter over
this module, which like the rest of ``entry.desktop`` keeps no web
framework in its signatures.

**The event vocabulary** (the ``type`` of every ``/jobs/{id}/events``
frame, shared with the chat stream where both exist)::

    job.created -> job.started ->
      llm_chunk | tool_started{tool, human_description} |
      tool_output_chunk | progress{label, percent} | heartbeat{state} |
      tool_approval_request{call_id, risk, summary} | usage{tokens} |
      artifact.created{artifact_id, kind, title, path} ->
    job.done{status} | job.failed{error, phase}

``human_description`` is the one-line plain-language summary of what a
tool call does ("run spatial_domains clustering (n=12)"), generated from
the tool name and its arguments by :func:`human_tool_description`.
``artifact.created`` is P2's: the output-contract scanner
(:mod:`~omicsclaw.entry.desktop.artifacts_manager`) emits one frame per
artifact it registers for a finished job, before the terminal event.

Every event is persisted to the ``job_events`` ring (most recent
:data:`JOB_EVENTS_RETAINED` per job) *before* it is broadcast, so a
``Last-Event-ID`` reconnect replays from SQLite and then continues live
with no window in which an event can be lost or duplicated.

**The approval gate reuses the tool layer's channel.** A job runs its
runner inside :func:`~omicsclaw.tools.context.use_tool_context` with a
per-job :class:`JobApprovalGate` as the approval channel and a progress
sink that maps :class:`~omicsclaw.tools.context.ProgressUpdate` onto
``progress`` events — the same seams ``bash`` and every other tool
already speak, so anything that calls
:func:`~omicsclaw.tools.context.require_approval` inside a job is gated
without this module knowing about it.

Not thread-safe as a whole: routes, runners and the gate all run on the
server's event loop. The SQLite store is reached synchronously (it is a
local file and the writes are single-row), which the memory layer's
:class:`~omicsclaw.memory.database.Database` serialises under its own
lock anyway.
"""
``````````

## omicsclaw/entry/desktop/turn_submission.py

Source SHA-256: `63c0f7f52ecae8f8026f017f8bb7b5dfaadc239a97dc65d46c4a1e091c9c1741`.

``````````python
"""Strict Desktop ``POST /chat/stream`` ingress: bytes in, one turn out.

The module owns only transport parsing. It never runs a turn, never
touches a workspace and never decides what the agent does; it decides
whether a request is admissible and, if it is, what
:class:`~omicsclaw.entry.ingress.InboundMessage` it means.

:func:`parse_chat_stream_document` bounds the raw bytes — size, nesting
depth, duplicate keys, non-finite constants — before and while parsing,
because this is the hostile-input surface. :func:`decode_chat_stream_request`
then refuses, by published code, what this route does not serve:

* every attachment-shaped input (``attachments_supported: False``), rather
  than silently dropping files the caller believed were uploaded;
* a request without a ``source_request_id``
  (``source_request_id_required: True``);
* per-turn provider credentials, because a chat request is not where a
  deployment changes which backend it pays;
* an ``ingress_schema_version`` other than the contract's request version.
"""
``````````

## omicsclaw/entry/display.py

Source SHA-256: `31ea665bd1806ef5400ec18a545c104eebce29177b1e1394fd539163cfc752eb`.

``````````python
"""Text made safe to show a person, for every surface.

A reason, a tool name, an argument or a line of output reaches a terminal
or a chat from a model that may have read a hostile file. Everything here
returns text in which no character can act on the display: control and
format characters are escaped by
:func:`~omicsclaw.tools.preview.escape_unsafe`, and line breaks are either
drawn as a mark (:func:`inert_line`), kept behind a fixed prefix
(:func:`inert_body`), so the text cannot start a line of its own, or
kept as they are (:func:`inert_prose`), for an answer that is printed on
lines of its own. :func:`inert_body` renders the body of a card, bounded
and folded, and :func:`inert_body_note` repeats its note.
:func:`approval_body` is the body of an approval card, with
credential-named values in the call's arguments hidden by
:func:`~omicsclaw.tools.preview.redact_credentials`;
:func:`approval_body_note` repeats its note, and
:func:`unreadable_arguments_note` is what stands for arguments that cannot
be read as JSON.
"""
``````````

## omicsclaw/entry/env_supply.py

Source SHA-256: `bef0935282b891a0202368b22a550934dde26ab197445ad4afffc1d7943b6acf`.

``````````python
"""The composition root's binding over the environment-supply mechanism.

The ``WorkspaceKernelBinding`` pattern, applied to P5: the
``manage_environments`` tool speaks the
:class:`~omicsclaw.tools.builtin.manage_env.EnvironmentSupply` Protocol
and this binding answers it on the ``envsupply`` package — probe one
interpreter, build and run the idempotent install script, list what
exists under the env root. One binding per deployment, shared by the
tool (chat face) and the ``env_install`` jobs runner, so both faces
provision the same directories with the same mirrors.

The blocking halves run under :func:`asyncio.to_thread` — a probe is a
subprocess and an install is minutes of one — and install output lines
reach the caller's ``on_output`` from that worker thread, which is the
contract the jobs runner's event emitters already accept.

Policy stays out: the mirrors, env root and timeouts arrive as
configuration (:func:`build_env_supply` reads them from
:class:`~omicsclaw.entry.config.AppConfig`), the approval happens in
the tool's gate, and a job is pre-consented by the person who posted
it. Nothing here asks anyone anything.
"""
``````````

## omicsclaw/entry/ingress.py

Source SHA-256: `f279de9da6be186efc102aaf7894e8a33c6d48fdb6667e03db943fdaabd9804d`.

``````````python
"""Who gets in, and whether the reply was taken.

Plan 0031 task A, decision Q23. Three surfaces — channel, desktop, CLI —
share exactly two questions at their edge: *is this sender allowed to
drive the agent*, and *did the message we sent back actually land*. Both
were answered by ``omicsclaw/control/``, which this rebuild deletes.
This module is that half of it, and only that half.

**The names are deliberately the old ones.** ``telegram.py:20`` and
``feishu.py:21`` import ``ChannelSurfaceBinding`` today;
``desktop/turn_submission.py:30`` imports ``RawInboundV1`` and
``RawContentBlockV1``. :class:`InboundMessage` stands where
``RawInboundV1`` stood and :class:`Acceptance` where
``DeliveryAttemptOutcome`` stood, because the reconnection is meant to
be a changed import path rather than thirteen small acts of translation.
Keeping a vocabulary three call sites already speak is not the same as
keeping the architecture that coined it.

**What was not brought across, and why it matters more than what was.**
``ControlRuntimePorts`` had twenty-two fields; five of them were a bare
:data:`~typing.Any` and a sixth was ``dict[str, Any] | None``. It is the
shape the owner's "high cohesion, low coupling" instruction was aimed at,
and the same shape plan 0030 already cut down once in
``ContextAssemblyRequest``. So: **no field in this module mentions**
:data:`~typing.Any` **at all**. :attr:`InboundMessage.values` is
``Mapping[str, object]``, which is what ``Any`` was being used to avoid
saying — a caller that puts something in gets ``object`` back and has to
admit it is narrowing.

**Both classes fail closed, and that is the whole of their value.** A
sender allowlist is not a feature that can be added later without
regression, because the regression is silent: a surface with no policy
does not look broken, it looks like anybody may drive the agent. So
:class:`SenderPolicy` has no default allowlist — omitting it is a
:exc:`TypeError` — and an empty one is refused at construction rather
than quietly admitting nobody.
"""
``````````

## omicsclaw/entry/shadow_review.py

Source SHA-256: `f3bba1d71db4464041e960ac402a1e57254fc98e3e8f1fccf943d4497ab0d2b7`.

``````````python
"""Checkpoint-triggered shadow review (P6 item 4).

A small automatic reviewer that watches the P2 artifact plane and, at a
checkpoint, makes one bounded model call whose verdict — ``pass``,
``warn`` or ``fail`` — is injected back into the main loop. The two
triggers:

* **count** — :data:`SHADOW_REVIEW_ARTIFACT_THRESHOLD` (3) or more new
  artifact records since the last checkpoint, and
* **structured block** — one new artifact of a structured kind
  (:data:`STRUCTURED_ARTIFACT_KINDS` — ``report`` and ``table``, the two
  kinds the results tray renders as a structured block rather than a
  media card) arrives.

The detector is a *checker*, not a listener: at each turn submission the
desktop surface asks the shared :class:`~omicsclaw.memory.artifacts.
ArtifactStore` for the newest records of the session and feeds them to a
per-session :class:`ShadowReviewCheckpoint` watermark, so no insert path
is touched and every writer (the ``save_artifact`` sink and the job
scanner) is covered by one query.

The review itself is fire-and-forget: it is scheduled as a background
task, and a failure or timeout is logged and skipped — it **never blocks
the main loop**. A ``warn``/``fail`` verdict lands in two additive
places:

* a one-shot context note attached to the **next** turn's request
  (:class:`ShadowReviewNudge`, mounted by
  :func:`~omicsclaw.entry.nudges.build_augmentor`), and
* a ``shadow_review`` frame published into the running exchange's stream
  when one is live (the desktop surface's publisher), following the
  additive frame precedent of the R1 ``suggestions`` frame.

``pass`` is quiet in both places.

The whole mechanism is off unless :attr:`~omicsclaw.entry.config.
AppConfig.shadow_review` is set: a deployment that has not asked for it
keeps byte-identical turns.
"""
``````````

## omicsclaw/entry/suggestions.py

Source SHA-256: `1e2ecd6d902cf69a70ce9a87ab4f934e20394f12182b954eaab0db685e754d4f`.

``````````python
"""``omicsclaw/entry`` — the follow-up suggestion block an answer may end with.

P6 item 2 of the front-back plan. A model instructed by
:data:`~omicsclaw.entry.assembly.FOLLOW_UP_RULES` may close its answer
with a sentinel line followed by up to three one-line follow-up
questions. Two halves live here:

:func:`split_suggestions` / :class:`SuggestionSplitter`
    The **streaming** half. Answer text reaches surfaces as
    ``TEXT_DELTA`` frames, so by the time a whole answer exists the
    block would already have been shown. The splitter is fed each delta
    as it is published, holds back only the bytes that could still turn
    out to be the start of the sentinel, and returns the visible text;
    once the sentinel has been matched everything after it is captured
    instead of shown. The turn kernel
    (:meth:`~omicsclaw.entry.turn.TurnKernel._sequence`) drives it.

:func:`strip_suggestions`
    The **after-the-fact** half, for text that is read whole rather than
    streamed — the exchange's ``reply``, which a Channel posts verbatim.
    RunResult history is deliberately *not* stripped: those bytes are
    the model's own record, matched byte-for-byte by prompt-prefix
    caches and replay evidence.

The block is optional by design (plan: "chips are OPTIONAL for the
model to produce, absent = no chips"). A model that never writes the
sentinel costs nothing: the splitter passes every delta through
untouched apart from the held-back-tail scan, and no ``SUGGESTIONS``
event is published.
"""
``````````

## omicsclaw/entry/turn.py

Source SHA-256: `7b4dfa7405ae3236ec3120bbb5f64bc9e59d444b6fcac4bdd51b139189afc29d`.

``````````python
"""One exchange: assemble its collaborators, run the loop, keep the result.

``AgentEngine.exchange`` renders a prompt, puts a history in front of it
and hands the finished trajectory back. This module is where all three of
those come from. Each turn hands the engine:

1. the app's prompt assembler, so an edited ``OMICSCLAW.md`` and
   today's date are both picked up — the engine renders
   it once, at the start of the exchange;
2. a :class:`_Carried`, this exchange's history and the place the engine
   leaves the conversation to carry forward;
3. a :class:`~omicsclaw.context.ProgressiveCompactor` that measures and,
   from the configured tier up, compacts the conversation before every
   model call of the run — and a
   :class:`~omicsclaw.planning.PlanInjector` that puts the session's
   outstanding plan back at the end of it afterwards, so that what
   compaction summarized away cannot take the plan with it.

:func:`_assemble` builds all of that in one place for all three paths
below (plan 0027 §12.4). The deadline stays out here: how long a
deployment is willing to wait is its own policy, not the loop's.

Each of the three paths below also wraps its exchange in
:meth:`~omicsclaw.observability.Telemetry.run` and feeds it the engine's
events. That is the whole of this layer's telemetry wiring: the scope
opens the interaction span, the events give it the turn boundaries, and
an unobserved deployment gets a scope whose spans are the shared no-op.
:mod:`omicsclaw.observability` explains why the engine was not given an
observer seam instead.

The history belongs to the caller. Nothing here is stored between calls
except a named session's durable Run projection and what a caller chooses
to carry: :class:`TurnOutcome` gives back
the conversation to keep and the compaction state to pass in next time.
"""
``````````

## omicsclaw/envsupply/script.py

Source SHA-256: `3298521fe31ae35630a239d5c0f92d54313de1dc43864e207f9c18dc400fd06b`.

``````````python
"""The local first-use install script, its disclosure, and its runner.

C3's ``build_env_install_script`` (remote) is the vocabulary this module
mirrors: ``set -e``; locate uv; ``uv python install``; ``uv venv``;
``uv pip install`` the base + stack; verify by importing ipykernel at
the end. Idempotent by construction the same way — every uv step is a
no-op when its output exists, so a retried install heals a
half-finished one instead of fighting it.

The local differences are the plan's, not inventions:

* **the uv ladder has no curl rung.** The App ships the uv binary
  (Tier 0), so resolution is: the ``OMICSCLAW_UV_BIN`` environment
  variable (what the App points at its shipped installer), then
  ``$OMICSCLAW_UV_INSTALLERS_DIR/uv``, then ``command -v uv`` — and a
  machine with none of the three is *reported* (exit 127, manual
  commands in the error), never fixed over the network unprompted;
* **mirrors are defaults, not opts-in.** ``UV_INDEX_URL`` prefixes the
  pip line and ``UV_PYTHON_INSTALL_MIRROR`` the interpreter line with
  the CN-friendly defaults from :mod:`.pins`, overridable per
  deployment — the mirror URLs always come from configuration, never
  from anything a model sent;
* **step markers** (``===OMICSCLAW_ENV_STEP:<name>===``) echo before
  each phase so the jobs plane can stream honest phase-level progress
  without parsing uv's output format.

POSIX sh, like the remote script: the desktop backend's install paths
run where ``sh`` exists. A Windows-first-run bootstrap is the App's
business (it drives the shipped ``uv.exe`` directly), not this
script's.
"""
``````````

## omicsclaw/hooks/audit.py

Source SHA-256: `472f4c7e73a3dc1904aa65313081e3c080fc496f0cbbd5a165acf6ef7602e7a6`.

``````````python
"""A record per tool call: what ran, how it ended, and nothing else.

The one hook this package ships. The other three the reference harness
mounts already have homes in this tree — permission and the danger
patterns are :mod:`omicsclaw.permission`, oversized output is
:mod:`omicsclaw.context`'s ``Offloader``, plan persistence is
:mod:`omicsclaw.planning` — and re-implementing any of them here would
give a deployment two answers to the same question.

Observability is what was left. ``docs/plans/0031-entry-layer.md``
deferred it by name ("hooks：permission / danger / offload /
observability | 不做（Q13）"), and
``entry/channel/telegram.py`` records the consequence: *"plan 0031 has no
audit log yet, and inventing one here would be a second unowned place
that writes about users."* This is the owned place.

**No OpenTelemetry.** The reference's equivalent
(``observability/hook.go``) starts a span and records two instruments;
this layer imports the standard library and nothing else, like every
other leaf in the rebuild, and hands a :class:`AuditRecord` to a
:class:`AuditSink` the composition root supplied. A deployment that
wants OTEL writes a twelve-line sink; a deployment that does not is not
made to carry the dependency.

**No duration.** Two layers already time a tool call and a third number
would be a third answer: ``ToolResult.metadata["duration_s"]``
(``tools/registry.py:416-422``, measured around the tool itself) and
``EngineEvent.duration_s`` (``engine/types.py:229-247``, measured around
the executor seam and *including* any approval wait). The second is the
one that reaches a surface. This record says a call happened and how it
ended; asking it to also say how long would have needed per-call state
on a hook object shared by every concurrent call.
"""
``````````

## omicsclaw/kernel/handles.py

Source SHA-256: `fa7c657cda59cd9709a82f689e043e12da6c2b3b9c84224b2628ec5060bad567`.

``````````python
"""AnnData handles across the host/kernel boundary, with revision gating.

The sandwich the plan took from OmicOS ``session_store.py`` +
``adata_kernel_runtime.py``: the caller (a job, a tool result) only ever
sees an ``adata_id`` and a one-line shape summary; the object itself
crosses as an h5ad snapshot, and the kernel holds it under
``OV_KERNEL_ADATA[adata_id]`` — a plain dict in the kernel namespace,
populated by :meth:`HandleRegistry.sync_in`.

**Revision gating is the half OmicOS got wrong** (its agent face does a
full h5ad round-trip every turn and its own comment expects that to fall
over at 170K features). A handle records the host-side revision —
``(st_mtime_ns, st_size)`` of the h5ad the kernel actually read — and a
``sync_in`` whose revision matches the resident one is a **zero-copy
skip**: no read, no write, the kernel keeps the object it already has.
The host file's mtime is the version, which is honest for the inputs the
job face declares (``inputs.adata_path`` names a file on the workspace)
and requires nothing of the caller but a stat.

**Cross-session access is refused, not routed.** A handle belongs to the
session whose kernel holds it; a second session asking for the same id
gets :exc:`HandleError`, because silently handing a second kernel the
same mutable object through a fresh file read is how two analyses
diverge while both believe they are looking at one thing.

**This round's scope.** The job face ``code_run`` uses ``sync_in`` (the
job declares ``adata_path``, the kernel gets ``OV_KERNEL_ADATA`` and the
convenience binding ``adata``); ``sync_out`` writes ``*.out.h5ad`` back
to the host for callers that ask. The chat face never crosses this
bridge — its variables live in the session kernel's namespace already,
which is the entire point of a persistent kernel there. R cells would
cross the same seam when an R kernel lands (via IRkernel, same
jupyter_client channel); today R stays on the step-runner subprocess.
"""
``````````

## omicsclaw/kernel/manager.py

Source SHA-256: `52903015b6566b5ce143f78fe0c2304f2c75dc807ad7937a8a5b3c48b97f3dbb`.

``````````python
"""The session-scoped persistent kernel manager.

One :class:`PersistentKernelManager` per deployment; one
:class:`~omicsclaw.kernel.session.SessionKernel` per ``session_id``. A
kernel is a **serial resource** — every session's executes run under that
session's :class:`asyncio.Lock`, because an IPython kernel executes one
request at a time and a second request would queue invisibly behind the
first while its caller believes it is running.

**The cancel ladder is four tiers** (OmicOS ``adata_kernel_runtime.py``
evidence): ``interrupt_kernel()`` → a 10 s grace for the executing task
to observe it → reset (kill the kernel, drop the handle cache, leave the
session lazily rebuildable) → a ``pass`` probe that proves whatever
kernel is left actually answers. :meth:`cancel` runs on the event loop
while the blocked execute sits on a worker thread, which is the only
safe arrangement — the ladder climbs from outside the cell.

**Restart is lazy and announced.** A kernel that dies — killed
externally, recycled by the reaper, or reset by a timeout — is *not*
rebuilt at that moment. The next :meth:`execute` cold-starts one, and
that cell's stderr begins with a handover notice carrying the dead
kernel's namespace summary (variable / type / shape, AnnData
special-cased) when one could be captured before the death, plus the
standing advice to restore from artifacts and files rather than
recompute. A kernel killed under us gets the no-summary variant of the
same notice — an honest "variables are gone" beats a fabricated one.

**Restart-need is a double signal** (the plan's fusion: CS's memory
threshold and OmicOS's counter, each alone too trigger-happy): a
session is marked for recycling-before-next-cell only when **both** the
cell count and the peak RSS have crossed their thresholds, probed after
every cell via ``/proc`` (:meth:`SessionKernel.peak_rss_kb`) — the same
usage probe whose numbers ride the ``usage`` event.

**A session's kernel also has a location** (C3): ``"local"`` (the whole
of P4, and the default) or ``"remote:<ssh alias>"``. The state key grows
a third component, so one session may hold a local python kernel and a
remote one at once — separate namespaces, separate lifecycles, the same
id. The manager knows the location *shape* and nothing more: a remote
kernel is built by the injected *remote_runtime* (an async
``start(session_id, host=..., ...) -> kernel`` plus its own environment
provisioning, living in :mod:`omicsclaw.remote.kernel` — this package
imports nothing of it, the way ``jobs_manager`` holds its planes). With
no runtime bound, a remote location is refused at the door: a deployment
that never asked for remote kernels behaves exactly as before, which a
test pins. The reaper, the cancel ladder and the obituary notices serve
remote kernels identically — only ``_start_locked`` and the park's kill
(which now runs on a worker thread, so a remote kill can pay its ssh
round trip without blocking the loop) know where a kernel lives.
"""
``````````

## omicsclaw/kernel/reaper.py

Source SHA-256: `753146d4975852eccb8acab57099a7f286aad28aa49a4ed7373937b881333fa6`.

``````````python
"""The kernel reaper: idle soft top, hard top, protection set.

Fused from the two houses, per the plan: Claude-Science's
``kernel_idle_timeout=1800s`` with a protection set (never reap a
session that is executing or waiting on a human) and ``21600s`` hard
ceiling, plus the double restart signal the manager evaluates per cell
(OmicOS's counter and CS's memory threshold together, because either
alone fires on healthy sessions — a long-running analysis is not a
leak).

The reaper is a plain asyncio task the manager starts on the first
execute and cancels at :meth:`~omicsclaw.kernel.manager.PersistentKernelManager.aclose`
— the desktop surface's ``finally: await app.aclose()`` is the hook the
plan asked for, and no FastAPI lifespan wiring is needed for a task the
app object already owns.

Every recycle goes through the manager's planned path: **the namespace
summary cell runs first** (the kernel is still alive, which is the only
moment it can be captured), then the kill, then the handle-cache drop,
and the next execute cold-starts with the handover notice. A sweep that
cannot take a session's lock (an execute snuck in) simply skips it —
the next sweep reconsiders, which is the low-drama answer to the
reaper-versus-execute race.
"""
``````````

## omicsclaw/kernel/session.py

Source SHA-256: `64cec605efbc76c0acbef85eb43e64a6bb6faebb9dd86c04546492e6bc802756`.

``````````python
"""One live IPython kernel and the iopub consumption primitive.

P4 track B of the front-back plan. The module below is the OmicOS
``session_notebook_executor`` pattern re-derived for OmicsClaw: start one
ipykernel through :mod:`jupyter_client`, send code with ``kc.execute``,
then **poll** ``get_iopub_msg(timeout=1.0)`` and dispatch by message
type, keeping only frames whose ``parent_header.msg_id`` is the request
we are waiting for (a kernel that posts status for a comm or a
background thread must not be read as our cell finishing), and ending at
``status: idle``.

The kernel starts from a temporary kernelspec whose ``argv`` names the
selected interpreter. IPython and Jupyter directories point
into a per-session scratch folder under ``<workspace>/.omicsclaw/kernel/``,
so the user's ``~/.ipython`` startup profile never runs inside an
analysis kernel, and the kernel never writes under ``$HOME``.

**This class is synchronous and thread-confined.** Every method blocks
(zmq reads, process waits); the manager calls it through
``asyncio.to_thread`` and owns the asyncio half (locks, cancellation,
the reaper). ``interrupt``/``kill`` are the two methods safe from any
thread — jupyter_client's own shutdown path relies on the same.

**The output ceilings are three, not one** (Claude-Science
kernel_worker.py:113/126/176, re-derived here as characters because a
Python ``str`` cannot cut a code point in half — the argument
``bash.py`` makes for its own limit):

1. *streaming* — once a stream (stdout, stderr — each measured alone)
   has delivered ``stream_emit_max`` characters to the callback, the
   callback stops being called and one truncation marker is emitted.
   The run keeps going; the audience stops paying.
2. *cell buffer* — the in-memory buffer of one cell keeps at most
   ``cell_buffer_max`` characters per stream; whatever overflows is
   counted and reported exactly: ``...(N further bytes dropped)``.
3. *result* — the :class:`CellResult` text fields are clamped to
   ``result_max`` characters each, a last line of defence before
   anything serialises the outcome into JSON or a model's context.
"""
``````````

## omicsclaw/mcp/config.py

Source SHA-256: `049f4b86af38205b813190ce1812e15980b836f224d27fa0982dbf7a833502fd`.

``````````python
"""Reading ``.mcp.json``: which MCP servers to connect, and how.

The file uses the ``mcpServers`` shape most MCP clients share::

    {
      "mcpServers": {
        "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
        "remote": {
          "type": "http",
          "url": "https://example.org/mcp",
          "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"}
        }
      }
    }

Per server:

``type`` / ``transport``
    ``"stdio"`` or ``"http"`` (``"streamable-http"`` and
    ``"streamable_http"`` are accepted spellings of ``"http"``). When
    omitted it is inferred: ``command`` means stdio, ``url`` means http.
    ``"sse"`` and ``"websocket"`` are rejected as unsupported.
``command``, ``args``, ``env``
    The stdio server's program, its arguments, and extra environment
    variables — an object, or a list of ``"KEY=VALUE"`` strings.
``url``, ``headers``
    The HTTP server's endpoint and request headers.
``enabled`` / ``disabled``
    A server can be switched off without deleting it.
``tools``
    An allowlist of the server's own tool names; absent means all.

``${VAR}`` and ``${VAR:-default}`` are expanded in ``command``, ``args``,
``env`` values, ``url`` and ``headers`` values. Unknown keys are ignored.

A missing file is an empty configuration. A file that cannot be read or
parsed raises :exc:`MCPConfigError`. An invalid entry is *rejected* — kept
on :attr:`MCPConfig.rejected` with its reason — and the other entries still
load.
"""
``````````

## omicsclaw/memory/__init__.py

Source SHA-256: `bc35364ff13ef686f09587548af139b4e8ba5828bb4b2fb4217d6633e53914fd`.

``````````python
"""Session persistence and long-term recall.

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
"""
``````````

## omicsclaw/memory/artifacts.py

Source SHA-256: `ff92f2b67d999ee88f7ab61d7b9855335970baf6d2815c7b7951f963c0497264`.

``````````python
"""The ``artifacts`` table over one :class:`~omicsclaw.memory.database.Database`.

P2 of the front-back plan. An *artifact* is one file a run produced that
is worth showing a person as a result — a figure, a table, the processed
``.h5ad`` — registered with a stable id, a kind, a checksum and the job
or session that produced it. The schema lives in
:mod:`omicsclaw.memory.database` (created on open, like ``jobs``), this
module owns the reads and writes, and both halves of the desktop entry
layer — the jobs-plane scanner and the ``save_artifact`` tool — reach the
same table through it. It deliberately sits in the memory layer rather
than in ``entry.desktop`` because the tools layer may not import the
entry layer, and ``save_artifact`` needs a store.

Kinds are a closed starter vocabulary, the one the plan fixed:
``figure | table | h5ad | rds | report | pdf | other``. The words and
the extension tables live in :mod:`omicsclaw.schema.artifacts` — the
shared vocabulary package — because the tools layer's ``save_artifact``
offers the same enum to the model and may not import this module. The
mapping from a filename is by extension, and every unrecognized
extension lands on ``other`` rather than being refused — a file the
scanner found is a fact about the run, not an argument to validate.
"""
``````````

## omicsclaw/observability/attributes.py

Source SHA-256: `1fa67e3d9288174168995e9be09b050a1bbaa3430b15b90015170f104829ec87`.

``````````python
"""Every name this layer writes: spans, attribute keys, instruments.

One module of constants, for the reason the reference harness gives its
own (``observability/attributes.go``): these strings are a **wire
contract** with whatever reads them — a Langfuse project, a Grafana
dashboard, a Jaeger query — so renaming one silently breaks a dashboard
nobody in this repository can see. Collecting them here means a rename is
a diff in one file rather than a grep, and means the parity table in
``docs/plans/0043-observability-layer.md`` can be checked against code
instead of against memory.

Four groups, and the split matters:

1. **Span names** — the shape of the trace tree.
2. **This layer's own attribute and metric names** — internal analysis
   dimensions, prefixed ``omicsclaw.`` where they are instruments and
   left unprefixed where they are attributes, exactly as the reference
   does. They are ours and no external specification constrains them.
3. **GenAI semantic-convention keys** — OpenTelemetry's, not ours. A
   backend recognises an LLM call by these, and getting one wrong costs
   token accounting and cost estimation rather than a missing label.
4. **Langfuse ingestion keys** — one vendor's, and the reason they are
   segregated into their own block: they are the group most likely to
   change under us, and a reader deciding whether an upgrade affects this
   repository should be able to answer it by reading one screen.

**Stdlib only, and no code.** Importing this module must stay free enough
that :mod:`omicsclaw.observability.config` can read it before deciding
whether observability is switched on at all.
"""
``````````

## omicsclaw/observability/config.py

Source SHA-256: `798a782f22a1c8ca5207866121a726b37b12764f2ede272dd37ca52caa2e3956`.

``````````python
"""Whether to observe, where to send it, and whether payloads may leave.

One frozen record read from the environment, mirroring
``observability/config.go``. Four of its five fields are the reference's;
the fifth — :attr:`ObservabilityConfig.capture_content` — is this
repository's, and it is the one to read before anything else in this
package.

**Why the environment, when :class:`~omicsclaw.engine.EngineConfig`
argues against it.** That argument is specific and does not reach here:
a turn ceiling read from the ambient shell makes two runs of the same
benchmark incomparable and the resulting stall looks like a model
regression. Telemetry changes no decision the agent makes — switching it
on cannot alter a trajectory — so the failure mode that ruling exists to
prevent does not exist for these fields. What does apply is the reason
:mod:`omicsclaw.provider` reads its own credentials: an OTLP bearer token
has nowhere else to live, and threading five more fields through
:class:`~omicsclaw.entry.config.AppConfig` would put a secret in the
record a surface prints when it reports its configuration.

Stdlib only. Reading this module must not require the OpenTelemetry SDK
to be installed, because deciding *not* to observe is the common case.
"""
``````````

## omicsclaw/observability/console.py

Source SHA-256: `f5b32a2516b84a46e3551088c57c9f03051e9a61966a424fac160a27e6080fb0`.

``````````python
"""A working backend with nothing installed: JSON lines and a metric summary.

Satisfies :data:`~omicsclaw.observability.config.ExporterType.STDOUT`.
The reference's equivalent is an OpenTelemetry SDK component
(``stdouttrace`` / ``stdoutmetric``); this one is **standard library
only**, and the difference buys two things worth having:

- a contributor can see the trace tree of a real run without installing
  anything, which is the state a bug report usually arrives in;
- the test suite can exercise a genuine backend end to end, so the
  contract in :mod:`omicsclaw.observability.contract` is pinned by
  something other than the no-op that trivially satisfies it.

**Where it writes, and why that is not stdout.** The name of the exporter
type is the reference's, kept for parity; the stream is
:data:`sys.stderr`, kept for survival. Two of this repository's three
surfaces draw on stdout — ``oc tui`` owns the whole screen and ``oc
interactive`` owns the line — so a span printed there corrupts the thing
the user is looking at. stderr is also where the reference sends its OTEL
*error* handler output, and for the same stated reason.

Imports this package and the standard library. No vendor SDK, which is
what makes it the fallback when :mod:`omicsclaw.observability.otel`
cannot load.
"""
``````````

## omicsclaw/observability/contract.py

Source SHA-256: `10fca04f9e998402db0f8f5d7347ecc9ce60fa19faf35d019a3e9ab1b918bfcb`.

``````````python
"""The three shapes this layer records into, and the free default.

:class:`Span`, :class:`Tracer` and :class:`Meter` — Protocols, plus a
no-op implementation of each. Everything else in this package programs
against these and never against a vendor, which is the same ruling
:mod:`omicsclaw.provider` makes about model backends and for the same
payoff: adding a backend is a new file, not an edit to the recorder.

**Why not simply depend on ``opentelemetry-api``.** That package ships
its own no-op and would make this module three lines. It was rejected on
two grounds, both checkable rather than aesthetic:

1. *It would be a hard dependency of an optional feature.* Every other
   layer in this rebuild is importable with nothing installed —
   ``provider/base.py`` says so in its own docstring — and a telemetry
   contract that cannot be imported without a third-party package would
   make ``import omicsclaw.entry`` fail on a machine that never wanted
   telemetry.
2. *It would decide the data model before the deployment does.* The
   OTEL API's ``Span`` carries events, links, status codes and a context
   API this package deliberately does not use (see
   :mod:`omicsclaw.observability.scope` on explicit parenting). Writing
   against the full interface and using a tenth of it is how the tenth
   becomes hard to find.

What is given up is real and is named so nobody rediscovers it: a
third-party OpenTelemetry instrumentation — an HTTP client, a database
driver — will **not** nest inside these spans, because it programs
against OTEL's context and this package does not put anything there.
:mod:`omicsclaw.observability.otel` does set the global tracer provider,
so such a library still exports to the same pipeline; its spans simply
arrive as roots of their own. Closing that gap means putting each span
into OTEL's context as well, which is a change to one adapter and not to
this contract.

**Three methods on a span, not ten.** The set is exactly what
``internal/observability`` uses across its three instrumented seams, and
adding a fourth should require a seam that needs it.

Stdlib only.
"""
``````````

## omicsclaw/observability/hook.py

Source SHA-256: `df5218a01980aac3bd9b82dd1d6e15d78cde07bb4c22918106fe2501ef508cfe`.

``````````python
"""One span and two measurements per tool call, through the hook seam.

The counterpart of ``observability/hook.go``, and the reason
:mod:`omicsclaw.hooks` exists as a mechanism at all — plan 0042 built the
seam and shipped the one hook nobody owned, naming this as the piece it
was deliberately not doing ("**No OpenTelemetry.** … A deployment that
wants OTEL writes a twelve-line sink"). This is that deployment's side of
the bargain, written once here rather than twelve lines at a time.

**It is not a second audit log.** :class:`~omicsclaw.hooks.AuditHook`
writes a durable local record, one line per call, digest only, mounted
**first** so that it hears a refusal from a hook mounted after it. This
one opens a span and records two instruments, and is mounted **last** so
that its span measures the tool and not its neighbours. They answer
different questions, they are switched on by different settings, and
neither can be derived from the other — but they share one vocabulary for
*how a call ended*, :class:`~omicsclaw.hooks.AuditOutcome`, because two
spellings of that would be two answers to one question.

**Where it sits in the chain, and the blind spot that follows.** The hook
chain runs *inside* the permission gate, so a call a rule denied never
reaches a hook and therefore never appears as a span. That is the same
blind spot ``observability/hook.go`` has, for the same reason
(``cmd/harness9/main.go:411-416``), and it is the right trade: a refusal
is a permission event, and
:class:`~omicsclaw.hooks.AuditHook`'s record is where it is already
written down.

Imports :mod:`omicsclaw.hooks` and this package.
"""
``````````

## omicsclaw/observability/otel.py

Source SHA-256: `206cc8a6d933aa527462abbe498b8198e7216ccb0d02ed9bef8b91427c980546`.

``````````python
"""The OpenTelemetry adapter — the only module here that knows OTEL exists.

The counterpart of ``observability/setup.go``, and the same relationship
to this package that ``openai_provider.py`` has to
:mod:`omicsclaw.provider`: one file where a vendor's names live, so that
importing the contract never requires the vendor to be installed.

**The import is inside the function**, which every other module in this
rebuild is forbidden from doing —
``tests/hooks/test_hooks_is_a_leaf_layer.py`` fails a package for it. The
exemption is argued rather than assumed: a runtime import is banned
because a module named as a string is a dependency the import rules
cannot see, and the fix is to make the dependency *visible*. Here it is
visible — the package is named in ``pyproject.toml`` under the ``otel``
extra, this module does nothing else, and
``tests/observability/test_otel.py`` skips itself when the import fails.
What a top-level import would buy is nothing, and what it would cost is
``import omicsclaw.entry`` failing on every machine that never wanted
telemetry.

**Failure is always a degradation, never a raise.** A missing SDK, an
unset endpoint, an exporter that cannot be constructed — each returns
``None`` and one log line, and
:func:`~omicsclaw.observability.telemetry.build_telemetry` falls back to
the no-op. **The reference does the same**, and saying so is a
correction: an earlier version of this docstring claimed ``main.go``
treats a failed ``Setup`` as fatal and counted the difference as this
layer being more forgiving. It is not a difference —
``cmd/harness9/main.go:127-131`` logs the error and substitutes
``observability.NewNoopProviders()``, and its three other
observability constructors degrade the same way. The claim was never
checked, which makes it the defect it would be anywhere else in this
tree.

**Export failures after construction are surfaced too**, which is the
one piece of ``setup.go`` this module was missing and its parity table
did not mention; see :func:`_surface_export_failures`.
"""
``````````

## omicsclaw/observability/provider.py

Source SHA-256: `130c4b18ce2444dcd6f149767008bdef894fe0bbe140e5783de4ae7b859972bd`.

``````````python
"""One span per model call, by wrapping the contract the loop programs against.

The counterpart of ``observability/provider.go``, and the seam that needs
the least argument: :class:`~omicsclaw.provider.LLMProvider` is a
structural :class:`~typing.Protocol`, so a decorator satisfying it is
invisible to the engine. Nothing in :mod:`omicsclaw.engine` or
:mod:`omicsclaw.provider` changes, and a deployment that does not observe
never constructs one —
:meth:`~omicsclaw.observability.telemetry.Telemetry.trace_provider` hands
the inner provider straight back.

**Decorating a provider is safe in a way that decorating the registry was
not.** ``entry/assembly.py`` refuses to let anything wrap
:class:`~omicsclaw.tools.ToolRegistry` because the engine probes it with
:func:`isinstance` for two optional Protocols and a wrapper that forgets
one fails silently (defect R3). ``LLMProvider`` has no optional
Protocols: four members, all of them required, all forwarded here — and
``tests/observability/test_provider.py`` asserts the member list against
the Protocol so that a fifth one added upstream fails a test rather than
disappearing.

Imports :mod:`omicsclaw.provider`, :mod:`omicsclaw.schema` and this
package.
"""
``````````

## omicsclaw/observability/serialize.py

Source SHA-256: `508b956e6fc114f2e774be287a691e85b07f9345995c8089bbac78acc562c4ef`.

``````````python
"""Turning a conversation into an attribute value, safely and briefly.

Three functions, the analogue of ``observability/helpers.go``. Nothing
here is called unless
:attr:`~omicsclaw.observability.config.ObservabilityConfig.capture_content`
is on — that check belongs to the callers, so that a deployment which
captures nothing never pays for a serialization it will discard.

**The hazard this module exists to contain** is that a span attribute is
a protobuf ``string`` field going over a network to somebody else's
service. Three properties have to hold and none of them hold for the
input:

1. It must be valid UTF-8. A tool's output can contain a lone surrogate,
   because reading a binary file through ``errors="surrogateescape"`` is
   how the standard library hands bytes back as ``str``.
2. It must be bounded. A ``read_file`` on a count matrix is megabytes,
   and an exporter that refuses an oversized payload drops the whole
   batch — so one large tool result loses the traces either side of it.
3. It must not silently become a different string. Truncation that cuts
   a multi-byte character in half produces exactly the corruption a
   reader will blame on their own pipeline.

Stdlib only.
"""
``````````

## omicsclaw/observability/telemetry.py

Source SHA-256: `81d32c3246e95e3e302e3945f9cde0cbf191685c199259a714657b978c600d3e`.

``````````python
"""The one object a composition root holds, and the one function that builds it.

:class:`Telemetry` is this layer's ``Providers`` (``setup.go:33-43``) with
the three seams folded in, so that ``entry/assembly.py`` asks for
telemetry once and then never branches on whether it got any. That is the
design constraint this module exists to satisfy:

**a deployment that does not observe must not be a different deployment.**
``Telemetry()`` — the default instance — is a real, usable object whose
tracer is the no-op, whose :meth:`trace_provider` hands the provider
straight back, and whose :meth:`tool_hooks` returns ``()``. So the
composition root has no ``if telemetry is not None`` anywhere, and the
object graph of an unobserved run is byte-for-byte what it was before this
package existed — the property :func:`~omicsclaw.hooks.hook_tools`
established for hooks, restated here because it is what makes "could
telemetry be implicated in this bug?" answerable by looking.

**Blocking work is moved off the event loop.** ``force_flush`` and
``shutdown`` are synchronous in every OTEL SDK — the first waits on an
HTTP round trip, the second joins a background thread — and the reference
simply blocks its caller. Here they are awaited through
:func:`asyncio.to_thread`, because the caller is a turn that has just
finished and the next thing it wants to do is answer somebody.
"""
``````````

## omicsclaw/permission/__init__.py

Source SHA-256: `7b48d5ffdfebcdf4dd585721db8e3982dc30c52532d5eba0b434a3c2e1f92bb5`.

``````````python
"""Human-in-the-Loop permission control: whether a tool call happens at all.

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
"""
``````````

## omicsclaw/permission/danger.py

Source SHA-256: `f61970d907cb395b1f3bc48884e2a2edbbf4e7b64f9c7d5c58f3285a7d685bb4`.

``````````python
"""Shell-command patterns that should be read by a person before they run.

Scans a shell command for syntax that destroys data, escalates privilege, or
sends local files somewhere else, and reports the match so a caller can put
the call to a human.

What this module guarantees, and what it does not:

- A match escalates to *ask*, **never** to *deny*. A pattern match is a
  suspicion, and only a person can tell ``rm -rf ./tmp_run7`` from
  ``rm -rf /``.
- Matching is **case-insensitive**, so ``chmod -R`` and ``chmod -r`` are the
  same command.
- Patterns are anchored so a command name is not matched inside a longer word
  or as part of a flag: ``sudo`` does not match ``--sudo-mode`` or
  ``pseudobulk``, ``sh`` does not match ``shuf``. They are **not** shell
  parsing: a command name appearing as an argument to something else
  (``grep sudo notes.txt``) still matches, and a command quoted inside a
  string still matches. The cost of both is one prompt.
- This looks at one command string. Deciding *which* tool arguments are
  command strings is the caller's job; :data:`COMMAND_ARGUMENT` is the
  convention it can use.
"""
``````````

## omicsclaw/planning/guidance.py

Source SHA-256: `780dc9ab0afe28b77c3b5303f9b38d348c097f4f4e4d708325e509a55fff0118`.

``````````python
"""The prompt section that makes planning a habit rather than a feature.

Plan 0039 §1. The reference harness is explicit that planning is a
**native capability, not a mode** (``plan.go:3-5``, after its Plan Mode
was removed): there is no switch to flip, no tool filtering and no
runtime detection. What replaces all of that is one section of the system
prompt plus one tool, and the model decides when a task is worth planning.

This module is the text. :mod:`omicsclaw.planning` renders it;
:func:`~omicsclaw.entry.assembly.default_sections` places it — the same
split :mod:`omicsclaw.skills` has with the catalogue, and for the same
reason: the layer that writes text and the layer that decides where text
goes should not import each other.

**Mount the section only when the tool is mounted.** The reference
harness gates it on ``planEnabled`` (``builder.go:123``) and the
consequence of getting it wrong is specific: a prompt that instructs the
model to call ``plan_write`` when no such tool exists produces a call
that fails, which the model then tries to correct — spending turns on a
capability the deployment turned off.
"""
``````````

## omicsclaw/planning/injector.py

Source SHA-256: `51339413a72b55b1338b1681be74fb3c9acb5d93b0ccfe80a5fd7e374d653849`.

``````````python
"""Putting the plan back in front of the model, before every call.

Plan 0039 §2.2. This is the half of the planning system that makes the
other half worth having: a plan the model wrote four compactions ago is
only authoritative if it is still *present*, and the conversation is
exactly the place it will not be.

Two things are appended, in this order, and both only to the copy that is
sent:

1. the **planning gate** — one nudge, at most once per exchange, when the
   model has spent many turns of that exchange reading without writing a
   plan or touching anything;
2. the **plan block** — every outstanding item, verbatim, with a header
   saying it outranks whatever the history now says.

The order is the reference harness's (``loop_phases.go:201-216``) and it
is the useful one: the nudge only ever fires when there is no plan, so
the two never appear together, and if that ever changes the instruction
to make a plan should come before the plan it is asking for.

**Nothing here is persisted and nothing accumulates.** The engine appends
these to the conversation it sends and then discards them; the next call
recomputes from the store. That is what makes a plan edited by
``plan_write`` visible on the very next call with no invalidation
anywhere.

:class:`PlanInjector` satisfies
:class:`omicsclaw.engine.TurnAugmentor` **structurally**. It does not
import it, and a layering test pins that.
"""
``````````

## omicsclaw/provider/__init__.py

Source SHA-256: `a66b11743eab060f5925847b3c3dbc166308b43025789f9c0000c434a9061461`.

``````````python
"""``omicsclaw.provider`` — the simultaneous interpreter.

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
"""
``````````

## omicsclaw/provider/_model_limits.py

Source SHA-256: `eb9b470bbbbac5ccb24fe736ad84d8422282a429ef5ffab0a8a37849a767cebd`.

``````````python
"""``omicsclaw/provider`` — how much a model can be asked to hold and emit.

A static table, mirroring the reference harness's ``model_limits.go``.
Two numbers per model: the context window a conversation must be
compacted to fit, and the output ceiling a request may name.

Three rules, each earned rather than chosen:

**Keys are bare model names.** A gateway prefixes the vendor
(``"openai/gpt-4o"``, ``"anthropic/claude-sonnet-4.6"``), so the lookup
strips everything up to the last ``/`` before matching. Without that,
every model would need one entry per gateway that resells it. An exact
match on the full identifier is tried first, for the cases where a
gateway genuinely publishes a different window than the vendor does.

**Unknown models get a conservative fallback, never zero.** 256K context
and 8K output — big enough that a modern model is not throttled, small
enough that a genuinely small model is not overrun. A table miss must
degrade, not fail: local Ollama tags and brand-new releases are missed
routinely and by design, since this table is maintained by hand.

**Context windows come from this repo's catalog where it has one.**
``omicsclaw/providers/models.py`` is the in-repo source of truth and
disagrees with the reference harness on several shared models (it lists
``claude-opus-4-7`` at 1M, the harness at 200K); the local figure wins
here. Output ceilings are the reverse — the repo records none, so the
harness's figures are used where they overlap and the conservative
default stands in everywhere else. An entry's output number being the
default therefore means "not known", not "known to be 8192"; a caller
that needs a larger ceiling sets ``max_tokens`` explicitly.

Last reviewed 2026-09.
"""
``````````

## omicsclaw/provider/anthropic_provider.py

Source SHA-256: `f3f077ad9d710414122b7333662ab00debd701ead0b775f5685f3fa85975ab34`.

``````````python
"""``omicsclaw/provider`` — the Anthropic Messages adapter.

Plan 0026 §8, task C. This is where the "simultaneous interpreter" claim
is tested, because Anthropic disagrees with :mod:`omicsclaw.schema` about
almost every structural choice:

===============  ==============================  ==========================
Concern          schema                          Anthropic
===============  ==============================  ==========================
System prompt    a ``Role.SYSTEM`` message       a top-level ``system``
                                                 parameter, lifted out of
                                                 the message list
Tool call        ``Message.tool_calls``          a ``tool_use`` block
Tool result      a ``Role.TOOL`` message         a ``tool_result`` block
                                                 inside a **user** turn
Arguments        raw JSON text                   a decoded object
Reasoning        ``Message.reasoning_content``   a ``thinking`` block
Stop signal      —                               ``stop_reason``
===============  ==============================  ==========================

Every one of those translations lives in this file and nowhere else. The
conversions are module-level functions over plain data rather than
methods, so the whole outbound and inbound mapping is testable without a
client, a key, or a socket.

**The SDK is loaded lazily, through one seam.** ``anthropic`` is an
optional extra and is *not* installed in this repo's environment. Binding
it at import time would make the module unimportable here and would drag
a vendor package into every test that only wants to check a conversion.
:func:`_build_async_client` is therefore the single function that needs
it; everything else in this module runs on the standard library.
"""
``````````

## omicsclaw/provider/base.py

Source SHA-256: `816c7f0a94338fea91f2da020127279f0d3b9260776359f683a8c3b2e238165a`.

``````````python
"""``omicsclaw/provider`` — the one contract the Main Loop programs against.

Plan 0026, step 2 of the staged framework rebuild. ADR 0077 gave the
system a vendor-neutral vocabulary; nothing could yet turn those types
into a request. This layer can::

    Engine ──▶ LLMProvider ──▶ vendor SDK
                 (here)         (OpenAI / Anthropic / DeepSeek / Ollama …)

A provider is a *simultaneous interpreter*: :mod:`omicsclaw.schema` types
go in, :mod:`omicsclaw.schema` types come out, and every vendor-shaped
field name lives and dies inside one adapter module. Adding a backend is
then a new file, not an edit to the loop.

**What is absent from the interface is the point.** ``generate`` takes the
conversation and the tools available for this turn, and nothing else — no
model, no temperature, no ``max_tokens``, no thinking budget. Those live
on the instance, in a :class:`~omicsclaw.provider.config.ProviderConfig`
fixed at construction, so the Engine cannot acquire model configuration:
there is nowhere to put it. Per-request variation goes through
:meth:`LLMProvider.bind`, which hands back a *different provider* rather
than widening the hot-path signature.

**Leaf-adjacent.** This package imports ``omicsclaw.schema`` and the
standard library, nothing else. No vendor SDK is imported here — that is
each adapter's private business, so importing the contract never requires
an optional extra to be installed.
"""
``````````

## omicsclaw/provider/config.py

Source SHA-256: `13f3c7759541208da895e14813cfac0f0d545cc72284898ed3c892c65134f23e`.

``````````python
"""``omicsclaw/provider`` — where a model backend is configured.

Plan 0026 §4. The endpoint table in ``omicsclaw/providers/registry.py``
is the highest-value asset of the package this layer eventually replaces:
thirteen presets, their base URLs, their API-key environment variables,
and the order in which a key is auto-detected. That knowledge is copied
forward here, shed of the LangChain factory it was entangled with. The
old module stays live and untouched until the closing migration task.

Two shapes:

``ProviderPreset``
    Static per-vendor facts — endpoint, default model, key variable,
    curated model list. Data, never resolved.

``ProviderConfig``
    One *resolved* call configuration, frozen. It holds precisely what
    :class:`~omicsclaw.provider.base.LLMProvider`'s two hot-path methods
    deliberately refuse to take as parameters: model, temperature,
    ``max_tokens``, thinking budget, timeouts. An adapter is built from
    one and never mutates it; ``bind()`` is :meth:`ProviderConfig.with_overrides`
    plus a fresh adapter instance.

**Stdlib only.** ``providers/timeout.py``'s policy is ported as plain
numbers rather than as an ``httpx.Timeout``: this layer may not import a
transport library, so each adapter builds its own SDK timeout object out
of :attr:`ProviderConfig.timeout_seconds` and
:attr:`ProviderConfig.connect_timeout_seconds`. For the same reason an
unparseable environment value is silently replaced by its default here
rather than logged, as ``timeout.py`` does — a configuration module that
depends on logging setup is a module that cannot be imported early.
"""
``````````

## omicsclaw/provider/factory.py

Source SHA-256: `38c5bd050b64fb8e48e5e6081a3eb1980d2f19a5508b921aa0d8c5fb3997489a`.

``````````python
"""``omicsclaw/provider`` — the assembly point: config in, adapter out.

Plan 0026 §1. The layer exists so that switching or adding a backend never
touches loop logic, and that promise has one more hole in it than the
:class:`~omicsclaw.provider.base.LLMProvider` interface alone can close:
*somebody* still has to decide which adapter class to instantiate. If that
somebody is the Main Loop, the loop imports ``anthropic_provider`` by path
and hard-codes which preset names speak Messages — which is precisely the
coupling the interface was introduced to prevent, reintroduced one import
line lower down.

So the decision lives here, as the reference harness's ``NewFromEnv``
lives in its provider package: one unified assembly point that ``main``,
a sub-agent, and a benchmark runner all call instead of each growing its
own copy of the mapping.

The mapping itself is *data* — :data:`ADAPTERS`, keyed by
:class:`~omicsclaw.provider.config.ProviderDialect`, whose value for a
given preset is recorded on the preset itself (see
``ProviderPreset.dialect`` for why it belongs there rather than here). A
third backend is then a new adapter module, a new dialect member, and one
entry in this table; no branch in this file changes.

**Importing this module installs no SDK requirement.** Both adapters load
their vendor package inside their client factory, so naming their classes
here — and re-exporting them from the package — costs an import of two
stdlib-only modules and nothing else.
"""
``````````

## omicsclaw/provider/openai_provider.py

Source SHA-256: `a6264329b26fd8ee3103de7276e6ab8c9a523d56b43e27edcddf63de93a9dfe3`.

``````````python
"""``omicsclaw/provider`` — the adapter for everything that speaks OpenAI.

Plan 0026 §8, task B. One file covers OpenAI itself, DeepSeek, Ollama and
every gateway that resells other vendors behind the Chat Completions
dialect (OpenRouter, SiliconFlow, DashScope, Volcengine, Moonshot, Zhipu,
NVIDIA). They disagree about field names and about what has to be
injected into the request body; they do not disagree about the shape. One
adapter plus a few named gates therefore serves twelve of the thirteen
presets in :mod:`omicsclaw.provider.config`.

Everything vendor-shaped stops here. The conversion is written as
module-level pure functions over plain dicts rather than as private
methods, because that is what makes both halves of the interpreter —
:func:`encode_messages` and :func:`decode_message` — assertable against
literal payloads with no client, no socket, and no SDK.

**The SDK is imported lazily**, inside :meth:`OpenAIProvider._create_client`.
``openai`` is an optional extra in this repo, and the conversion half of
this module has to stay importable and testable on an installation that
lacks it. A missing SDK then surfaces as a :class:`ProviderError` at call
time, which is the same failure mode this layer already promises for
every other backend problem, instead of as an ``ImportError`` raised
somewhere far from the call that caused it.
"""
``````````

## omicsclaw/remote/__init__.py

Source SHA-256: `c0e3d723837c5f203acdd38e48f47a5d15d43061e0d934bcff1d47bdfb7407d7`.

``````````python
"""``omicsclaw.remote`` — the remote execution plane (C1/C2).

The remote runtime plan's main route: orchestration stays local, the
remote host is asked for ``sshd`` and a POSIX shell, and every remote
fact that outlives a call is a durable handle in SQLite. Four layers,
each importable without the one above it::

    ssh        argv construction + process spawning (the transport)
    probe      the read-only probe script + its tolerant parser
    jobs       job scripts, handles, the status state machine (pure)
    store      remote_hosts / remote_jobs over the memory Database
    plane      RemotePlaneBinding: the tool layer's injected plane
    bridge     the P1 jobs-plane runner for runtime="remote:<alias>"
    kernel     C3's remote persistent kernel: env supply + the tunnel
    reconcile  re-estimate in-flight handles after a restart

The tool family itself lives in the leaf layer
(:mod:`omicsclaw.tools.builtin.remote`) with this package injected as
its :class:`~omicsclaw.tools.builtin.remote.RemotePlane`; the
composition root binds :class:`RemotePlaneBinding` when
:attr:`~omicsclaw.entry.config.AppConfig.remote_execution` is on, and
nothing remote exists in a deployment that leaves it off — no tool, no
prompt section, no socket opened on the plane's behalf.

**Standard library only**, like everything this package spawns: the
SSH client is the system's ``ssh``/``sftp`` (zero pip dependencies, the
user's config as the contract, host keys the system's business).
"""
``````````

## omicsclaw/remote/bridge.py

Source SHA-256: `6ddaaca7ed5d37e22f6ac627d8af8ac9c335b7d9542cd6a4f57c053a2b1d70a6`.

``````````python
"""The P1 jobs-plane bridge: one ``POST /jobs`` request, run remotely.

C2's routing half. A job created with ``runtime="remote:<alias>"`` is
executed by :class:`RemoteJobsBridge` instead of its kind's local
runner, and the bridge speaks the P1 event vocabulary through the
:class:`~omicsclaw.entry.desktop.jobs_manager.JobExecutionContext` it is
handed — ``job.started`` and ``job.done`` come from the manager's own
``_run`` exactly as for a local job, and the bridge adds ``progress``
frames for submission and each poll, plus ``tool_output_chunk`` frames
carrying the *new* bytes of the remote ``job.log``.

**Today the remote job is a command.** ``inputs.command`` is what runs;
``inputs.inputs`` / ``inputs.outputs`` / ``inputs.scheduler`` map onto
the tool-plane ``remote_submit`` arguments. A remote *skill* (running
skill code next to the data) is C3's persistent-kernel territory, not
this bridge's — saying so here is cheaper than discovering it in a
stack trace.

Observation timeout or controller shutdown detaches this runner while
the admitted job keeps running. JobsManager sends an explicit cancel
through the plane and waits for observed terminal evidence. The existing
stores and reconciler remain responsible for job state.
"""
``````````

## omicsclaw/remote/jobs.py

Source SHA-256: `65c0807bd6047cc6552d432c6648f2735d62627b2bce3b287ece927d8a61ec60`.

``````````python
"""Job scripts, handles and the state vocabulary — the pure half of C1.

A remote job is two small files and a number. The files are ``cmd.sh``
(the model's command, verbatim) and ``job.sh`` (a wrapper that records
how the command ended); the number is a SLURM job id or a process-group
id, and it is the whole handle — Claude Science's ruling, which this
package adopts: the remote side runs what the host already knows how to
run (``sbatch``, ``setsid nohup``), and the local side needs no daemon,
no agent and no version matrix to stay in touch with it, only the
commands built here.

**Everything in this module is pure.** Script generation, submission
command construction, and the parsing of what comes back are functions
over strings, so the whole submission state machine is tested without a
network; the process half (:mod:`omicsclaw.remote.plane`) only stitches
them together. The remote-side contract is deliberately readable: a
person who ``ssh``es into the work directory finds ``job.log``,
``_omicsclaw_exit_code`` and, for a nohup job, ``_omicsclaw_pgid`` —
plain files, no magic, so the rare manual intervention needs no
documentation from us.

**The ``#SBATCH`` promotion rule.** ``scheduler="auto"`` submits with
``sbatch --parsable`` exactly when the command itself carries ``#SBATCH``
directive lines — the form a user's existing cluster script already has
— and otherwise runs it under ``setsid nohup``. The user's directives,
not a re-implementation of them: this package never parses resource
requests out of the command to synthesize its own ``sbatch`` flags,
which is how a wrapper silently disagrees with the script it wraps.

**nohup jobs report their own death.** The wrapper writes
``_omicsclaw_exit_code`` on every exit path (no ``set -e``, so a failing
command still gets its code recorded), and the launcher writes its own
process id — which, under ``setsid``, is the session id and therefore
the process-group id — to ``_omicsclaw_pgid`` before ``exec``-ing the
wrapper. Status is then two facts the host already knows: does the group
still exist (``kill -0``), and if not, what code did it leave behind.
"""
``````````

## omicsclaw/remote/kernel.py

Source SHA-256: `d7f6050d41a4ad2cc431b9af9911db31541c428695130a86715f26f8290b13da`.

``````````python
"""C3: the remote persistent kernel — ipykernel beside the data.

The one piece of remote state the plan keeps (§4 C3, absorbing OmicOS's
real advantage): a session's analysis kernel runs **on the remote host**,
so ``adata`` loads once and survives turns without the data ever
travelling to this machine. The supply side stays minimal — the remote
host is asked for one Python environment under ``~/.omicsclaw/kernel-env``
(uv venv + ipykernel + the scanpy core stack), not a product stack.

**The wire is the standard jupyter protocol.** The remote ipykernel
writes its own connection file (five ports on the remote loopback plus a
random key); we read that file back over SFTP-less ``ssh cat``, rewrite
nothing but the ``ip`` (defensively — both sides are 127.0.0.1), and run
one ``ssh -N`` process carrying five ``-L`` forwards bound to the **local
127.0.0.1 only**. :mod:`jupyter_client` then *attaches* —
``load_connection_file`` + ``client()`` + ``wait_for_ready`` — never
``start_kernel``: the process belongs to the remote host, and the local
side owns only the tunnel.

**The kernel object is P4's, subclassed.** :class:`RemoteSessionKernel`
extends :class:`~omicsclaw.kernel.session.SessionKernel` and overrides
exactly the process-management face — ``start``/``is_alive``/
``interrupt``/``kill``/``shutdown`` and the two ``/proc`` probes —
because the cell-execution half (the three output ceilings, figure
capture, the idle notice, the interrupt ladder) is location-agnostic and
is inherited, not copied. The manager treats it as one more kernel:
same reaper, same idle soft top, same cancel ladder, same obituaries.

**Liveness has two witnesses.** The heartbeat channel is the cheap local
one; when it falls silent the two failure modes — kernel died, tunnel
died — are indistinguishable locally, so ``ssh kill -0 <pid>`` settles
it (the pid the launch recorded, nothing else). A tunnel that died with
the kernel alive is *not* a death: the next cell re-spawns the forwards
and the zmq channels reconnect, and the variables are still there — the
acceptance scenario (kill the tunnel, reconnect, the namespace survives)
is a property of this split, not a recovery path bolted on later.

**Every remote command from a kernel method runs on a worker thread**
(the manager's ``asyncio.to_thread`` boundary). The sync driver here
enforces that invariant: it refuses to run on the event loop, because
``asyncio.run`` cannot nest — the one P4 call site that used to kill
from the loop thread (``_park_with_obituary``) now offloads to a thread
so the remote kill can pay its SSH round trip off-loop.

**Two seams deliberately untouched.** The ``X-OmicsClaw-Runtime``
response header (P3's routing seam) is not spoken here: it answers
*where an HTTP request's backend lives*, and a remote kernel is reached
by a tool argument or a job body, not by a wire route — widening it to
mean two things would cost the header its one meaning. And ``bash``
stays location-blind: remote shell-shaped work is C1's ``remote_exec``
family's trade, and a location-dispatching ``bash`` would be a second
shell seam with none of the kernel's persistent state to offer.
"""
``````````

## omicsclaw/remote/plane.py

Source SHA-256: `680ec5ba7dea98321a0baec913110c53aeefb05c7daeb63c251989c27ce65d6f`.

``````````python
"""The plane: one object that does what the remote tool family asks for.

The composition half of C1. :class:`RemotePlaneBinding` implements the
:class:`~omicsclaw.tools.builtin.remote.RemotePlane` Protocol
structurally — it imports nothing from the tool layer except the
result shapes, which is the same direction ``bash.py``'s environment
seam faces — and is what :func:`~omicsclaw.entry.assembly.build_app`
binds when :attr:`~omicsclaw.entry.config.AppConfig.remote_execution`
is on.

**One host, one executor, one connection policy.** All traffic for a
host goes through :class:`RemoteExecutor`, which builds every command
with the pure functions of :mod:`omicsclaw.remote.ssh` and
:mod:`omicsclaw.remote.jobs` and runs them through the injected
:class:`~omicsclaw.remote.ssh.SshSpawner`. ControlMaster multiplexing
(the spawner's fixed options) makes the second command on a host cheap;
the plane adds no connection state of its own on top of that.

**The scratch is probed once and remembered.** Submission needs a
directory to work in; the plane reads the host knowledge base for the
last probe and probes fresh when there is none, then creates
``<scratch>/omicsclaw/<stamp>-<rand>`` per job. A probe that cannot
parse still yields a scratch (the home directory), because a host with
a broken ``free`` is more often a working cluster than a paperweight.

**Declared outputs travel with the job, not in a schema column.**
``remote_submit`` writes the names it was given into
``_omicsclaw_outputs`` inside the work directory, so what a fetch by
``job_ref`` should bring back is a fact the remote side holds and any
later session can read — no column to migrate, and a person poking at
the workdir sees the same list the agent sees. With nothing declared,
fetch brings back ``job.log``, which is the one file every job has.

**Fetch refuses rather than transports, past the threshold.** Sizes are
checked over SSH before any byte moves; a file over ``max_mb`` is
answered with a ``remote://<host>/<path>`` reference and nothing
travels. The reference is the plan's "data stays put, the handle
travels" rule made concrete.
"""
``````````

## omicsclaw/remote/probe.py

Source SHA-256: `4e3f9461a4c6885e42c6eb8dc68055d9164b1d2086fa21c36be93143678ac484`.

``````````python
"""The host probe: one read-only script, one JSON object, best effort.

The knowledge-gathering half of C1. The plan's ruling (Claude Science's
probe, adapted): a host is characterised by running **one POSIX shell
script in a login shell** and parsing the JSON it prints between two
sentinel lines — not by installing anything, not by assuming Python
exists remotely, not by parsing human-formatted tool output a second
time.

**Read-only, and visibly so.** The script touches nothing: ``nproc``,
``free -m``, ``nvidia-smi -L``, ``command -v``, ``sinfo``, ``hostname``,
``uname``, and existence tests on scratch candidates. A host
administrator reading the base64 blob can decode it and find no writes
anywhere, which is the only audit story a "just probe it" feature can
offer.

**Everything is optional.** Every field is emitted best-effort: a
machine without ``nvidia-smi`` answers an empty GPU list, a machine
without SLURM answers no partitions, a shell without ``free`` answers
``null`` memory. :func:`parse_probe_output` carries the same tolerance —
a missing field, a truncated tail, a marker pair that never closes, or
JSON that will not parse all yield a :class:`HostProbe` rather than an
exception, with :attr:`HostProbe.parse_error` saying what was imperfect.
A probe that crashes on half the fleet is worse than no probe.

**The logic is local, the facts are remote.** Which scratch candidate to
prefer (:meth:`HostProbe.scratch_root`) is decided in Python where it
can be tested and changed; the script merely reports which candidates
are defined and which exist. The script is delivered base64-wrapped
(:func:`~omicsclaw.remote.ssh.encode_login_script`) so no quoting
question exists in either shell, and runs under ``bash -l`` so the
module/conda environment a user actually gets is the one measured.
"""
``````````

## omicsclaw/remote/reconcile.py

Source SHA-256: `fed86584095a0e71913d4da3c484e7218c0d6eee2ef4cbac6ff871db24633bc6`.

``````````python
"""Re-estimating in-flight remote jobs after a restart or on demand.

The C2 half that makes a handle worth persisting. A backend restart
loses nothing on the remote side — the job is SLURM's or init's — so
the only question the new process must answer is "what happened while
nobody was watching?", and this module is the asking.

**Tolerance is the contract.** A host that cannot be reached moves its
rows to ``unknown`` and keeps them; the plan is explicit that an
outage must not cost a handle, and the reconciler honours that by
catching :exc:`~omicsclaw.remote.ssh.RemoteHostUnreachable` and every
``OSError`` the transport can raise on the way down, rather than by
deleting anything. Rows leave the in-flight set only when the host
itself says the job ended.

**One seam: the plane.** :func:`refresh_in_flight` takes anything with
``job_store()`` and ``executor_for()`` —
:class:`~omicsclaw.remote.plane.RemotePlaneBinding` in production, a
pair of fakes in the tests — and the jobs-plane bridge additionally
hands it a callback so a restarted desktop backend can move the
*local* job row with the remote answer.
"""
``````````

## omicsclaw/remote/store.py

Source SHA-256: `3d0ba217f1d12585f34c2c8cb88d4caae13a588b780727df740661d6038ebfa9`.

``````````python
"""The local knowledge: probed hosts and job handles, in SQLite.

The persistence half of C1/C2. Two tables live in
:mod:`omicsclaw.memory.database`'s schema (created by every
:class:`~omicsclaw.memory.database.Database`, which is this codebase's
migration mechanism — ``CREATE TABLE IF NOT EXISTS`` on open):

* ``remote_hosts`` — one row per alias: the probe's JSON, when it ran,
  and a JSON list of notes (the answers ``ask_about_host`` collected).
  This is the "host knowledge base" the plan names: the resource card a
  probe produces plus the facts only the user knows (partition, account,
  module activation), kept together so the next session starts from
  both.
* ``remote_jobs`` — one row per submitted job: the durable handle
  (:class:`~omicsclaw.remote.jobs.RemoteJobHandle` flattened), the
  last-observed status, and ``local_job_id`` linking a job the P1 plane
  started to its handle row. This is what survives a backend restart:
  the reconciler walks the in-flight rows and re-estimates them, so
  "close the laptop, the job keeps running" needs no daemon on either
  side.

**Rows are never deleted on doubt.** A host that cannot be reached
moves its jobs to ``unknown``; a job that left no trace stays
``unknown``; only an explicit terminal answer moves a row to ``done`` /
``failed`` / ``canceled``. The plan is explicit that an unreachable host
must not cost the handle, and the cheap way to guarantee that is to
have no code path that drops rows on error at all.

Both stores are synchronous like their neighbours in
``entry/desktop``: they are reached from one event loop via SQLite's
own busy handling, and the callers that can block off-loop use
``asyncio.to_thread`` themselves.
"""
``````````

## omicsclaw/sandbox/egress.py

Source SHA-256: `6b9d5e19d2b0b3ade54e5dcdbe491df1fc3654ec079fe4a676ed9c30b72ccc23`.

``````````python
"""Default-deny HTTP/HTTPS CONNECT egress filtering proxy with SSRF protection.

Pure standard-library asyncio implementation:
- Preset domain groups (``EGRESS_GROUPS``) and non-overridable exfiltration
  denylist (``EGRESS_DENYLIST``).
- Enforces the 6-step evaluation order:
  1. Valid hostname check (``invalid-host``)
  2. Resolved IP SSRF check (``blocked-by-resolved-ip``), unless matched by
     ``egress_endpoints`` or ``allow_private_cidr``
  3. Exfiltration denylist check (``blocked-by-denylist``)
  4. Port check ``{80, 443}`` or ``egress_endpoints`` (``blocked-by-port``)
  5. Group / allowlist / dynamic grant check (``allowed``)
  6. Default deny (``blocked-by-allowlist``)
- Supports Unix domain socket listener (Linux ``bwrap`` / ``netns``) and
  localhost TCP listener with optional 64-hex ``Proxy-Authorization`` token
  verification (macOS ``seatbelt`` / Windows ``appcontainer``).
- Supports upstream HTTP/HTTPS proxy chaining when ``HTTP_PROXY`` / ``HTTPS_PROXY``
  is configured on the host.
"""
``````````

## omicsclaw/schema/artifacts.py

Source SHA-256: `2a001cbc1fbf7fd5268947dbb6c75b1531f57641a137600ccbd2bc3cd9ed10f2`.

``````````python
"""The artifact vocabulary: the kinds every layer names artifacts by.

P2 of the front-back plan gave this vocabulary exactly seven words —
``figure | table | h5ad | rds | report | pdf | other`` — and two layers
have to agree on it without either importing the other: the tools layer
(``save_artifact`` offers the enum to the model) and the memory layer
(the store persists it). That is precisely what this package is — the
one vocabulary the layers share — so the words live here rather than in
either of them, and the extension tables that pick a kind come with
them because two of those tables would be two places the same
``.h5ad`` could stop meaning ``h5ad``.

``.md`` reads as ``report``: ``report.md`` is the convention every
curated skill's CLI writes for its summary. An unrecognized extension
lands on ``other`` and ``application/octet-stream`` — never an error,
because the kind of a file that exists is a fact to record, not an
argument to validate. Additive-only within a schema version: a client
that meets an unknown kind falls back to a generic icon, exactly as it
falls back to ``other``.
"""
``````````

## omicsclaw/schema/message.py

Source SHA-256: `f359706a6507079e2dd7c626c7e6d9938eb5879e1cece9ba44ec909026d1ff66`.

``````````python
"""``omicsclaw/schema`` — the standard types every component exchanges.

Per ADR 0077. This is the system's unified blood. The Main Loop, the
model adapter, the tool registry, and the memory layer all pass these
types to each other and nothing else.

It is defined before the Main Loop for the same reason the reference
harness defines it first: the loop body *is* the movement of these types,

    Message → ToolCall → ToolResult → Message

so there is no loop to write until they exist.

**Vendor-neutral by construction.** OpenAI, Anthropic, DeepSeek and
Ollama disagree about nearly everything — where a tool call's name lives,
whether a tool result is a role or a content block, what a cached token is
called. None of that appears here. Translating between a vendor payload
and these types is the model adapter layer's only job. A vendor field
name leaking into this package is a bug.

The two ReAct moves this package exists to carry:

=========  ==============================================================
Thought    ``Message.reasoning_content``
Action     ``Message.tool_calls`` → :class:`ToolCall` → :class:`ToolResult`
=========  ==============================================================

**Leaf package.** Standard library only: no ``omicsclaw`` imports, no
third-party dependency, no I/O, no logging. Every layer may depend on it;
it depends on none. That is what keeps the dependency graph acyclic.
"""
``````````

## omicsclaw/skillenv/overlay.py

Source SHA-256: `e545b6b7fff237b271a0034073610036578dc2f35ed5e605a3d3798efc70ddeb`.

``````````python
"""Overlay environments: a virtual environment over the base interpreter holding only what the base lacks.

An overlay is created with ``--system-site-packages`` from the interpreter
``bash`` runs, so every base package stays visible and the overlay's own
site-packages comes first. Its directory is ``<root>/<key>/.venv``, where the
key is a digest of the base's identity, the distributions in the base's own
site-packages and the requested requirements: any change to the base gives a
new key. An overlay with a fingerprint file is finished and is never changed
or deleted here; a directory without one is a half-built overlay and is
removed under the key's lock, which lives in ``<root>/.locks`` and is never
deleted.

:class:`OverlayBuilder` builds one overlay: resolve with ``pip install
--dry-run --only-binary=:all: --report``, keep what the base already has
(:func:`fill_only`), install the rest pinned with ``--no-deps``, compare what
was installed with what was planned, check where every installed file landed
and which top-level names it adds, compare ``pip check`` before and after,
import the skill's modules, and only then write the metadata and the
fingerprint. Any failure, cancellation or time-out removes the half-built
directory.
"""
``````````

## omicsclaw/skills/__init__.py

Source SHA-256: `54e56f94e401fbbb83781555b1ec88416e5af792fe24adc3451f34fcbed5b6d4`.

``````````python
"""``omicsclaw.skills`` — the Skill loader.

Skills are the third source the prompt composer draws on, after the
engine's own minimal core and the workspace's ``AGENTS.md``. They are
disclosed progressively: the system prompt carries one line per skill,
the ``search_skills`` tool queries that catalogue by keyword, and the
``use_skill`` tool loads a body when the model needs one. Over
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
"""
``````````

## omicsclaw/subagent/__init__.py

Source SHA-256: `2a417d96678db420176cfca9bf1a6e99eebf86b36b425ff965ec4e8e9a138916`.

``````````python
"""``omicsclaw/subagent`` — handing a bounded sub-task to a second agent.

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
"""
``````````

## omicsclaw/subagent/frontmatter.py

Source SHA-256: `c465b89ac65af1e1393ef673eaa7f6b22b2c0d21e3df069e9bb2bec22f383c92`.

``````````python
"""Reading a sub-agent definition out of a Markdown file with a YAML header.

The supported subset is what an agent file uses and no more:

* ``---`` on the first line and a closing ``---`` on its own line;
* ``key: value`` at column zero, with matching surrounding quotes removed;
* block sequences — ``- item`` lines under a bare key;
* comma-separated scalars, accepted wherever a sequence is.

A key whose value continues onto further lines — a block scalar (``>``,
``|`` and their chomping variants) or a plain scalar folded over an
indented line — is outside that subset, and the **whole key is dropped**
rather than half-read. Keeping the first line would hand back ``'>'`` or
a sentence cut at its first newline, which passes validation and is worse
than the field being absent: an absent ``description`` falls back to the
body's first line and an absent ``name`` to the file's stem.

Everything after the closing delimiter is the sub-agent's system prompt.
Nothing here touches the filesystem; :func:`parse_agent_file` takes text.
"""
``````````

## omicsclaw/tools/_html.py

Source SHA-256: `19d47ac50a711d066e70b1adce9cfc65cbdb1974a5e5ca2ca6f7eecc9961b6ce`.

``````````python
"""HTML reduced to something a model can read, standard library only.

Modelled on harness9's ``internal/tools/web_content.go``, and the one
place in this work where the reference cannot be ported, only answered:
its pipeline is ``go-readability → html-to-markdown``, both third-party
Go libraries, and this package may import nothing outside the standard
library. There is no readability in it and no HTML-to-Markdown converter.

**So what ships is a better version of the reference's fallback path.**
Tags that are never content (``script``, ``style`` and neighbours) are
dropped, as ``extractPlainText`` does; tags that are structurally
boilerplate (``nav``, ``header``, ``footer``, ``aside``, ``form``) are
dropped too; and structure is kept rather than flattened — headings as
``#``, list items as ``-``, links as ``[text](url)``, ``pre`` in a fence.

**The gap, stated rather than papered over:** ``go-readability`` finds
the article by *scoring* nodes, so it discards a sidebar built from a
``<div class="sidebar">``. A fixed tag list cannot. On a documentation
page or a blog post the two are close; on a news site heavy with
promotional furniture this returns more noise.

**Truncation** aligns the cut to the last newline before the ceiling when
that newline is past the halfway point, so a page is cut at a paragraph
rather than mid-sentence. What is *not* ported is the loop after it in
``web_content.go:91-93``, which walks backwards off a partial UTF-8
character: a Go ``string`` is bytes and can be split mid-character, while
a Python :class:`str` is code points and ``text[:n]`` cannot be.

**Leaf-adjacent.** The standard library only — :mod:`html.parser` and
:mod:`re`.
"""
``````````

## omicsclaw/tools/base.py

Source SHA-256: `2fba884b3e2f746b644e4e66a980c27f13198dcf290492cbb934dbaf6ce06ff5`.

``````````python
"""``omicsclaw/tools`` — what a tool *is*, and what may be said about one.

Plan 0028 §2. Steps 1–3 gave the system a vocabulary
(:mod:`omicsclaw.schema`), an interpreter that speaks it
(:mod:`omicsclaw.provider`) and a loop that moves it
(:mod:`omicsclaw.engine`). This module answers the question that layer
deliberately left open: what does the loop's :class:`ToolExecutor` seam
actually dispatch *to*?

**A tool is one object.** Its name, the schema the model is shown, and
the code that runs are three faces of a single thing, so they live
together. The arrangement being replaced splits them across a metadata
record, a separate ``dict[str, Callable]``, and a string key matched at
start-up — under which "what does this tool do" has no single place that
answers it, and a parameter change has to be made twice with nothing
checking that both were made.

**Two vocabularies, deliberately not one.**
:class:`~omicsclaw.schema.ToolDefinition` is what a *model* is told;
:class:`ToolPolicy` is what this *machine* is allowed to do. They are
separate types because they have separate destinations: the first is
serialized into a prompt, the second must never be. Keeping policy off
:class:`Tool` entirely means there is nowhere for it to leak from — the
same argument :class:`~omicsclaw.engine.executor.ToolExecutor` makes for
its own two-method surface.

**Leaf-adjacent.** ``omicsclaw.schema`` and the standard library. Nothing
else in the ``omicsclaw`` namespace — not ``provider``, not ``engine``,
and above all not ``omicsclaw.runtime.tools``, the legacy layer this one
is built to replace. ``tests/tools/test_tools_is_a_leaf_layer.py`` is the
only thing enforcing that, so it is enforced by test rather than implied
by a package name.
"""
``````````

## omicsclaw/tools/builtin/manage_env.py

Source SHA-256: `b1d690ef096554c0a4764d4daf6020e0bc3f2e298866b76d21b815b71f2a130c`.

``````````python
"""``manage_environments`` — inspect and provision local environments.

P5's "环境即工具" half: the chat face of the supply plane. The model can
ask what environments exist, whether one can host the kernel, and
install (or finish installing) one — so "帮我装 squidpy" is a first-class
conversation instead of a README the person has to go run. The jobs
face of the same mechanism is ``POST /jobs {kind: "env_install"}``;
both bind the same entry-layer seam.

**Leaf-layer rule, like ``python``'s and ``save_artifact``'s**: this
module imports ``omicsclaw.schema`` and the tool layer's own modules,
nothing heavier. The supply itself is an :class:`EnvironmentSupply`
Protocol the composition root binds
(:func:`omicsclaw.entry.env_supply.build_env_supply` over the
``envsupply`` package); a tool constructed without one refuses at
execution time, because no argument a model can send could supply a
process.

**Approval follows the ``install_skill_deps`` category**: installing
packages is ``HIGH`` risk, ``ASK`` mode, and only the ``install``
action asks — ``list`` and ``probe`` read state and never trigger a
card. The reason names the environment, the packages, and the mirror
*domain* the configured URL will serve, without pasting the URL itself
onto the card (the mirror is configuration, not something the model
chose — see the whitelist note below).

**The mirror whitelist is structural.** The model names *packages*,
never URLs: ``packages`` entries must be bare distribution names
(letters, digits, dots, underscores, dashes — no specifiers, no
``pip install`` URL forms, no shell metacharacters), and the seam
receives only those names. Which index serves them is decided entirely
by the composition root's configuration; there is no argument path
from the model to a URL.
"""
``````````

## omicsclaw/tools/builtin/python_kernel.py

Source SHA-256: `34dad73e1594ee18a1b598e49b28ef31874cf9e784afd64d9d7c7180954c315a`.

``````````python
"""``python`` — a cell in the session's persistent kernel.

P4's chat-face half. What the model sends here runs on **one IPython
kernel per session that survives between calls**: a variable defined in
one ``python`` call is still there in the next, which is the entire
difference from piping a snippet through ``bash`` and the reason this
tool exists.

**The division of labour with ``bash``, stated in the description so the
model picks for itself:** short shell-shaped work — list a directory,
probe a file, run a CLI — belongs to ``bash``; anything that builds on
earlier Python state — load once, cluster next, plot after — belongs
here. Nothing in ``bash`` changes.

**Leaf-layer rule, like ``save_artifact``'s**: this module imports
``omicsclaw.schema`` and the tool layer's own modules, nothing heavier.
The kernel itself is a :class:`KernelRunner` Protocol the composition
root binds (:func:`~omicsclaw.entry.assembly.build_app` mounts
:class:`~omicsclaw.kernel.PersistentKernelManager` behind it); a tool
constructed without one refuses at execution time with a
:exc:`RuntimeError`, because no argument a model can send could supply
a process. The Protocol is structural, so a test double stubs one
method.

**Approval follows the ``bash`` category**: arbitrary code is arbitrary
code, whatever interpreter runs it — ``HIGH`` risk, ``ASK`` mode, the
prompt quoting the first lines of the code the model actually sent
(hand-written schema, hand-written reason, the ``write``/``save_artifact``
discipline).

**Streaming goes through the tool layer's progress channel.** The
runner hands this tool a synchronous ``on_output`` callable it invokes
from the kernel's worker thread as iopub stream frames arrive; the tool
forwards each through :func:`~omicsclaw.tools.context.report_progress`,
which the chat surface renders as ``tool_output`` frames — the same seam
every other tool's progress speaks, so no new event type exists on the
chat wire. The forwarding is coalesced (at most ~4 frames/s) because a
runaway ``print`` loop must not become a frame storm.

**The timeout arithmetic is ``bash``'s**, with its collision stated
rather than hidden: the engine's per-call budget (:data:`DEFAULT_TIMEOUT`
+ :data:`ENGINE_TIMEOUT_MARGIN` <= ``EngineConfig.tool_timeout``) bounds
what a chat cell can do; a longer computation belongs on the job face
(``POST /jobs {kind: "code_run"}``), whose ceiling is the job timeout.

**C3's location argument is mounted, not always present.** A deployment
with a remote plane constructs this tool with ``allow_remote=True`` and
the schema grows an optional ``location`` ("local" — the default, the
whole of P4 — or ``"remote:<ssh alias>"``); without the flag the schema
is byte-identical to P4's, which is the "off changes nothing" contract
a test pins. A remote cell rides the same ASK approval its local twin
does, with the host named on the card, and the *first* remote cell on a
host additionally surfaces the one-time environment-provisioning card
the runtime asks for (the runner passes the location down; the approval
itself happens in the entry layer, which is the only layer that may
hold a tool context).
"""
``````````

## omicsclaw/tools/builtin/web_search.py

Source SHA-256: `39f4bba2c679b97e9e08429a7fedcd2f182b1d6dee6ed0a776e4ded15d77e6c8`.

``````````python
"""``web_search`` — one query to one search backend, no API key.

Modelled on harness9's ``internal/tools/web_search.go``, whose smallest
decision is the one worth keeping: it posts to DuckDuckGo's HTML endpoint
and parses the page. No key, no account, no vendor SDK, nothing to
configure wrong. For a layer that may import nothing outside the standard
library it is also the only shape available.

**What that costs is a dependency on somebody else's markup.**
``div.result``, ``a.result__a`` and ``a.result__snippet`` are class names
on a page DuckDuckGo may restyle any week, and the failure mode when they
change is **zero results rather than an error**: the request succeeds, the
parse finds nothing. :func:`parse_results` cannot tell "nothing matched"
from "the page is no longer shaped like this", so :func:`_report` names
both possibilities — the reference says only "no results found", which is
indistinguishable from a restyled page.

**The query is egress**, which is what the ``ASK`` policy is for: a search
query is text sent to a third party, and a model asked "is this variant
known" may put the variant in it. The whole query is in the approval
prompt, verbatim.

**World failure is not tool failure.** No results, a non-200 from the
backend and an unparseable page are returned as text with ``is_error``
false. A malformed query raises, because the model fixes that by sending
different arguments.

**Against the legacy ``web_search``**, which is untouched: it takes a
``topic`` enum (``general``/``news``/``finance``) that has no equivalent
here, and it returns **whole pages as markdown** by fetching every hit,
so one old call becomes one search plus N ``web_fetch`` calls, each with
its own approval. The split is the better design — a model should choose
what to read — but it is a workflow change, not a like-for-like
replacement. What is gained: no Tavily API key and no ``research`` extra.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools._html``,
``omicsclaw.tools._websafety``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``, and the
standard library.
"""
``````````

## omicsclaw/tools/function_tool.py

Source SHA-256: `7f1480def054257a9553c207bf2c74681b891bd60658761f71c985a7b1474032`.

``````````python
"""``FunctionTool`` — a plain callable, wearing the :class:`Tool` shape.

Plan 0028 §4 Q1. Most tools are a function: some arguments in, some text
out. Making each of them a hand-written class with three members would be
ceremony, and the fifty tools waiting to be migrated are all plain
functions today, so the adapter is what they arrive through.

What the adapter adds is the part a plain function has no way to do for
itself: **turning the model's mistakes into something the model can fix.**
Two of them, and they are different mistakes:

*The payload was unreadable.* :meth:`ToolCall.parsed_arguments
<omicsclaw.schema.ToolCall.parsed_arguments>` answers ``{}`` for a
truncated or malformed payload, by design — it exists so a bad argument
string cannot crash a run. But ``{}`` is also what a genuinely
argument-free call looks like, so a tool that used it would run with no
arguments and report success for a call that was never fully transmitted.
This adapter never calls it. A payload that does not decode is reported as
undecodable, with the decoder's own complaint and a bounded excerpt of
what actually arrived, and the function is not invoked. Plan 0028 trap 8.

*The arguments were readable but wrong.* Checked against the tool's own
declared schema, and every problem is listed at once so one retry can fix
all of them. Plan 0028 trap 9.

**Both are reported by raising** :exc:`ToolArgumentError`, which is how
this seam spells failure: ``execute`` returns ``str``, so there is no
in-band way to say "this is an error", and
:meth:`~omicsclaw.tools.registry.ToolRegistry.execute` is what turns an
exception into an ``is_error`` Observation. What trap 9 forbids is a
validation failure that ends the run; one raised inside ``execute`` never
leaves the registry.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools.base``, and the
standard library.
"""
``````````

## omicsclaw/tools/registry.py

Source SHA-256: `da8a3619e1a1149c7644fc62673767ca336dbc64ba61d9269cf8e92efcaf8c25`.

``````````python
"""The hub: registration, exposure, dispatch — and nothing else.

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

``use_request_context`` relays immutable model-request facts from schema
into a tool's private context. It does not interpret those facts or select
tools. Visibility, surface policy and stage subsets remain assembly concerns
(plan 0028 §5). The registry exposes the full set in stable order and
``policy_for`` so the assembly can filter it.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, and the standard library.
"""
``````````
