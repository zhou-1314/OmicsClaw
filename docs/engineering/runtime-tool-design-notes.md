# Runtime file-tool design notes

This archive preserves six module introductions moved on 2026-10-16. The quoted text records the implementation and migration context at the time it was written. References to retired code, missing isolation, old deployments and measurements are historical; consult the current code and deployment documentation for present behavior.

The source modules retain short descriptions and their runtime contracts. Function and class documentation remains beside the code. The relocation leaves the executable code, tool descriptions, packaging rules and skill files intact.

## omicsclaw/tools/builtin/bash.py

```text
``bash`` — the tool whose only boundary is the human being asked.

Plan 0029 §4 Q2/Q3/Q4/Q5/Q6/Q11 and traps 5, 6, 7, 13, 14, 15 and 16. The
reference is harness9's ``internal/tools/bash.go``, and of the three
foundation tools this is the one where the *reference* is worth more than
the design: four of its 239 lines carry measured numbers from failures
somebody else had, and each of those four is reproduced below with the
measurement re-taken under Python.

**This tool has no path argument, and therefore no path attack surface.**
``bash.go:88-107`` takes ``command`` and ``timeout_secs`` and nothing
else, and its ``Execute`` never calls ``safePath``. Neither does this one.
That is not an oversight in either: ``cd ../.. && cat /etc/passwd`` is not
a boundary being evaded, it is a boundary that was never claimed, and a
path check bolted onto the one argument that is a whole programming
language would be security theatre a model steps around by spelling the
path differently. What bounds this tool is the approval gate of Q2 and,
one day, the injection seam of Q11 — written here because plan 0029 §10-9
originally aimed its adversarial path-boundary criterion at this file, and
an implementer reading that would either skip the criterion silently or
invent a check no plan designed.

**Q6 — a non-zero exit is not a tool failure.** This is the tool that
makes plan 0028's "world failure versus tool failure" distinction earn its
keep. ``pytest`` reporting three failures is a fact about the world; the
tool did exactly what it was asked. ``bash.go:145-147, 190-191`` returns
that text with ``err == nil``, so it arrives as an ordinary Observation,
and the reason is what the model does next: told ``is_error=True`` it goes
looking for a broken tool, told the exit status it goes looking for the
broken test. The criterion in full, and it is the same one
``builtin/read.py`` states for its own half: **what should the model
change next?** A non-zero exit gives it nothing to change about the call,
so it is not an error; a payload that will not decode, a command that is
the empty string, a ``timeout_secs`` of zero — each is fixed by sending
different arguments, so each is raised and the registry marks it. The two
live adjacently in ``tests/tools/test_bash.py``; the ``read`` half of the
pair is in ``tests/tools/test_read.py``, because the two tools shipped in
different tasks and neither may edit the other's test file.

**One deliberate departure from the reference, on that same criterion.**
``bash.go:119-121`` answers an empty command with the *string*
``"Error: 命令为空字符串"`` and ``err == nil`` — a failure delivered as a
success whose text happens to begin with "Error". That shape is precisely
the legacy defect this layer exists to end:
``runtime/tools/builders/engineering.py`` returns ``"Error: file not found
or outside allowed roots: …"`` as a tool *result*, so the run never sees
``is_error``, and both ``builtin/read.py`` and ``builtin/write.py``
document rejecting it. An empty command is also squarely on the
correctable side of the criterion above. So the reference's *detection* is
kept and its *reporting* is not: :exc:`ToolArgumentError`, which the
registry turns into ``is_error=True``.

**Q5 / trap 13 — the subprocess writes to a temporary file, never to a
pipe.** ``bash.go:153-165`` is the most valuable comment in the reference.
``CombinedOutput()`` collects output through a pipe, and ``Wait()`` waits
for every holder of the write end to close it; a command like ``A && B &``
leaves a backgrounded grandchild holding that end, so the call blocks
until something else kills it. Measured there: **20 s of hanging became
~5 ms, with the background process still running**, once output went to a
file instead.

That is a property of pipes and process inheritance, not of Go, and it
reproduces here exactly. Measured on this machine, Python 3.13, with
``bash -c 'echo hi && sleep 30 &'``:

* ``create_subprocess_exec(..., stdout=PIPE)`` + ``communicate()`` — still
  blocked when a 6 s test deadline gave up.
* the same command with ``stdout=<temp file>`` — returned in **0.003 s**,
  exit status 0, ``hi\n`` captured.

``tests/tools/test_bash.py`` pins the second measurement, because a
"simplification" back to ``PIPE`` would leave every other test in this
file green.

The cost is inherited along with the fix and is stated to the model in
:func:`_description`: the temporary file is deleted when this call
returns, so anything the command backgrounds should redirect its own
output (``nohup cmd > out.log 2>&1 &``) rather than inherit this one.

**Trap 16 — a process killed by a signal did not succeed.** This one is
not from harness9. It is an archived bug in ``earendil-works/pi``, whose
bash tool tested ``exitCode !== 0 && exitCode !== null`` — and a
signal-terminated process reports ``null``, so everything ``SIGKILL`` or
``SIGTERM`` stopped sailed past the failure branch and was returned as a
success with whatever partial output it had managed. Translating that
predicate literally would be wrong here too, for a different reason:
:mod:`asyncio.subprocess` reports a signal as a **negative**
``returncode`` (measured: ``kill -9 $$`` → ``-9``, ``kill -TERM $$`` →
``-15``), and ``-9 != 0`` is true but ``-9`` is not a status any shell
ever prints. :func:`_exit_status` normalises to the shell's own
``128 + signum``, so a ``SIGKILL`` reads as ``137``, which is both non-zero
and recognisable. The normalisation is applied to an injected
environment's answer as well, because an environment built the obvious way
— on :mod:`asyncio.subprocess` — would hand back the negative form and
carry pi's bug across the seam.

**Q4 / trap 6 — the budget is 45 seconds because the engine's is 60.**
``bash.go:29-32`` uses 120 s with a 600 s ceiling the model may negotiate
up to. Copying either number would make this module's timeout handling
*unreachable*: ``EngineConfig.tool_timeout`` defaults to **60 s** and
wraps the whole of ``executor.execute(call)``
(``omicsclaw/engine/executor.py::_execute``), so at 120 s the engine
cancels first and the model is told ``tool 'bash' timed out after 60s``
instead of reading the banner below. :data:`DEFAULT_TIMEOUT` is therefore
45 s, leaving the :data:`ENGINE_TIMEOUT_MARGIN` of 15 s, and
:attr:`BashTool.max_timeout` equals the configured timeout rather than
being a second number — the model may ask for **less** and never for more.

That last part is a simplification of the plan, which specifies a ceiling
that happens to equal the default. Two constants whose equality is the
invariant are two constants that can drift apart in one edit; one
attribute derived from the other cannot. What the plan wanted the ceiling
for is unchanged: ``timeout_secs`` stays in the schema because a short
probe is genuinely useful, and if the engine's budget ever becomes
configurable, the ceiling follows the operator's ``timeout`` without a
second edit.

**This module may not import** :class:`~omicsclaw.engine.config.EngineConfig`
**to check any of that** — the layering rule forbids it — so the coupling
is implicit and is held by a test instead: ``tests/tools/test_bash.py``
imports both and asserts the arithmetic with no override on either side.
A test directory is not under the layering guard, which only walks
``omicsclaw/tools/``.

**Q3 / trap 14 — truncation keeps the tail.** 16,000 with a head of one
third and a tail of two thirds (``bash.go:23, 205-222``), and the reason
is in its comment: a test runner prints verbose progress first and the
``FAILED`` lines, the traceback and the ``=== N failed ===`` summary
**last**, and the implementation that kept only the head cut off exactly
the part that was worth reading. See :data:`HEAD_CHARS`.

**Trap 15 — what was deliberately not translated.**
``trimToValidUTF8Suffix`` / ``trimToValidUTF8Prefix``
(``bash.go:224-238``) trim a partial rune off a slice boundary. A Go
``string`` is a byte sequence and slicing one really can split a
multi-byte character; a Python :class:`str` is a sequence of code points
and ``s[:n]`` **cannot**. Translated, those loops would be two functions
whose condition is never true — unreachable code wearing a safety
measure's clothes. They are absent, and the boundary problem is absent
with them, because the bytes are decoded **once** with
``errors="replace"`` before anything is measured or sliced. That is also
what settles the open question plan 0029 Q3 leaves to this task, of
whether 16,000 counts characters or bytes: slicing bytes would put the
split-character problem back and then demand the trimming loops to undo
it, while :class:`str` makes the whole class of defect unspellable. See
:data:`MAX_OUTPUT_CHARS`.

**Trap 5 — cancellation passes through.** No ``except Exception`` and no
``except BaseException`` anywhere below. The engine's per-tool deadline
arrives as :exc:`asyncio.CancelledError`, and reported to the model as a
tool failure it would send the model to fix a command that was fine. It
is *named* only where a subprocess would otherwise be orphaned by the
abandoned turn, while the shell runs and while it is still being started:
the command's process group is killed, the shell is reaped, and the
exception is re-raised untouched, which is passing it through rather than
handling it.

**Q11 — the injection seam, and it is live.** ``environment=None`` means
this machine. Anything else has :meth:`BashEnvironment.run_bash` awaited
instead of a local process being started, and
``tests/tools/test_bash.py`` drives a fake through it. harness9 holds an
environment on its file tools and never reads it (``read_file.go:35-37``
is a TODO), which is the dead state trap 15 asks an assessor to report.
harness9's own ``LocalEnvironment`` is deliberately **not** reproduced:
its ``RunBash`` calls ``CombinedOutput()`` (``local_environment.go:28``),
which is the pipe bug the paragraph on Q5 above exists to avoid, and it is
harmless there only because nothing in production calls it.

**No OS isolation ships with this.** Until something implements
:class:`BashEnvironment`, the only thing between a model's command and
this machine is the approval channel — which is why :data:`_POLICY` is
``ASK``, and why plan 0029 §12 carries the isolation step forward as its
own piece of work.

**Two residuals, stated rather than hidden.**

*The whole output is read into memory before it is truncated.* A command
that emits gigabytes is a memory event no 16,000-character ceiling
prevents; the reference has the identical hole (``bash.go:180``). Closing
it means reading bounded windows from each end of the file, which forces
the elision count into bytes while everything else here counts characters
— a reconciliation worth designing once, not improvising here.

*A process can leave the group it is killed through.* The shell runs in a
session of its own, and a deadline or a cancellation kills that whole
process group, so a pipeline, a backgrounded job or a script the shell is
waiting on goes with it. A process that moves itself out of the group,
with ``setsid`` or as a job started after ``set -m``, is not reached. A
command that finishes in time is not killed at all, so what it
backgrounded keeps running.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools._workspace``,
``omicsclaw.tools.base``, ``omicsclaw.tools.context``,
``omicsclaw.tools.function_tool``, ``omicsclaw.tools.builtin.read``, and
the standard library.

```

## omicsclaw/tools/context.py

```text
Out-of-band channels: how a running tool reaches the caller that started it.

Plan 0028 §4 Q4. The seam step 3 published is ``execute(call)`` — one
argument, and deliberately not a ``ctx``. Go's ``context.Context`` carries
*values* as well as cancellation, and the reference harness uses that half
for seven keys, of which this module reproduces two: an approval callback
handed to the tool executor (``hooks/decision.go:60``) and a progress sink
handed to sub-agent streaming (``hooks/subagent_progress.go:18``). Python
has an environment-carried equivalent that does not require widening a
published Protocol, so this module is what the Go ``ctx`` value half
becomes: :mod:`contextvars`.

**Of the five not reproduced, three are observability spans and two are a
mechanism worth naming.** ``approvedContextKey`` and
``explicitlyAllowedContextKey`` (``hooks/decision.go:74`` and ``:89``,
read at ``hooks/hook.go:87``) mark a *call* as already decided, so one
tool call asks a human **at most once** however many layers of the hook
chain want to ask. There is no equivalent here: :func:`require_approval`
asks every time it is called. Every tool in this package calls it once, so
nothing is wrong today — but a wrapping tool (a skill wrapper, an MCP
proxy, a future sub-agent tool) would prompt twice, and the layer that
would own the fix is the hook layer this package deliberately does not
have.

That this works at all rests on one property of the engine:
``engine/executor.py`` runs each call in its own :class:`asyncio.Task`,
and ``Task.__init__`` calls :func:`contextvars.copy_context`, so every
worker runs on a **copy of the context current when the turn's tools were
scheduled**. A variable set by a surface before the turn is therefore
readable inside a tool running three levels down, and the engine needs no
change at all. A property nobody wrote down is a coincidence waiting to be
refactored away, so it is written down here, and
``tests/tools/test_context.py`` asserts it through the real
``execute_tool_calls`` rather than by reading a variable back in the
coroutine that set it.

**What would break it is narrower than it looks.** Neither
:class:`asyncio.TaskGroup` nor :func:`asyncio.gather` is a threat —
both create Tasks, and a Task copies. The two that are: awaiting a call
**inline** rather than in a Task, which leaves every tool writing into
the turn's own context; and creating the Tasks with one shared
``context=``, which is the only spelling that makes several workers share
a mutable context on purpose.

**The five questions plan 0028 §4 Q4 requires answering, answered.**

1. *What is carried — a value or a reference?* **A reference.** The
   variable holds the approval *callback* and the progress *sink*, never
   an approval result. A decision that was computed before the tool ran
   cannot be about the arguments the tool actually received, and a tool
   that must block until a human clicks needs something it can await.
   :func:`require_approval` awaits the callback, so the round trip is the
   callback's own business — see :class:`ApprovalRequest`.

2. *Who sets it?* **The surface** (CLI, Desktop, Channel), once, before
   entering the agent turn — :func:`use_tool_context` or
   :func:`set_tool_context`.

3. *Who reads it?* **The concrete tool**, inside its own ``execute``.
   :class:`~omicsclaw.tools.registry.ToolRegistry` reads neither channel
   and still has no opinion about approval: it makes them exist and takes
   no part in what travels down them. A registry that *enforced* approval
   would have to know who is asking, which is precisely the knowledge
   plan 0028 trap 12 keeps out of it.

   It does publish two things, and it has to. The first is the policy in
   force for a call, resolved by
   :meth:`~omicsclaw.tools.registry.ToolRegistry.policy_for` — the
   deployment's ``register(policy=)`` outranking the author's
   ``tool.policy``, per plan 0028 §4 Q5 — and a tool asking with the
   policy *it* holds would be asking with the author's, never the
   deployment's. That made the whole of Q5 decorative in the one
   direction that matters: a deployment tightening ``AUTO`` to ``ASK``
   gated nothing, because the tool went on self-approving from its own
   ``AUTO``. So :meth:`~omicsclaw.tools.registry.ToolRegistry.execute`
   binds its resolution through :func:`use_effective_policy` and
   :func:`require_approval` reads it from there. Publishing a resolution
   is not enforcing it; the registry still neither asks nor decides.

   The second is the scheduler's :data:`TimeoutPause`, bound through
   :func:`use_timeout_pause` from what the engine handed the registry for
   this call. Same shape and same reason: the thing a tool needs is owned
   by a layer the tool may not import, so the dispatcher in the middle
   passes it down instead.

4. *What happens when nothing is set?* **Fail closed.**
   :func:`require_approval` raises :exc:`ApprovalUnavailable` when a tool
   needs consent and no channel is bound, which the registry turns into an
   ``is_error`` Observation naming the reason. The absent channel is never
   read as consent. Progress is the opposite and deliberately so: a
   missing sink is the normal case, so :func:`report_progress` is a no-op
   that reports having done nothing.

5. *Can two sessions cross?* **Only if they share an asyncio Task.**
   ``contextvars`` isolates per Task, not per coroutine call, so two
   concurrent sessions in one process — which the Channel Surface has by
   construction, Telegram and Feishu conversations interleaving on one
   event loop — are isolated **iff each top-level session runs in its own
   Task**. Two sessions binding a context inside one shared Task is a
   real defect with a plausible-looking diff: the second ``set`` wins, and
   one user's tool call is offered to the other user's approval prompt.
   Both halves have a test.

**Not a general-purpose bag with two extras bolted on.** The two named
channels are named because they are the two that need a *reference*.
:attr:`ToolContext.values` carries the flat per-turn facts the existing
tool layer injects through ``ToolSpec.context_params`` — ``session_id``,
``chat_id``, ``surface``, ``workspace``, ``thread_id`` and the rest — and
exists so plan 0028 §5's "must have a one-for-one replacement" is true
rather than aspirational. It is deliberately untyped: this layer must not
learn what a ``surface`` is.

**A third channel carries a question to the person.**
:attr:`ToolContext.question` is bound the way the approval channel is and
read by :func:`ask_question`, which raises :exc:`QuestionUnavailable` when
none is bound.

**A human's thinking time is no longer charged to the tool's timeout.**
``engine/executor.py::_execute`` wraps ``executor.execute(call)`` in
``asyncio.timeout(config.tool_timeout)`` — sixty seconds by default —
while Q4.3 above puts the approval await *inside* the tool's own
``execute``, so the two nest the wrong way round and a user who took
longer than ``tool_timeout`` to press "approve" used to have the tool
cancelled and the model told ``tool 'X' timed out after 60s``: false
(nothing ran long, nobody answered) and actively misleading, since it
sends the model to make the tool faster. :func:`asyncio.shield` was never
the fix — the outer budget still raises at the next await point in this
task — so the fix is the engine lifting its own deadline: it offers the
call a :data:`TimeoutPause`, :func:`require_approval` enters it around
the round trip, and the seconds that were left are restored on the way
out. The engine's half is ``DeadlineAwareExecutor``, and the registry is
what carries it here.

A pause stops the clock; it does not bound the wait. **A surface that
asks a person still owns the deadline on its own prompt** — an IM bot
posting an approval card runs its own timer and answers "nobody
responded" itself, so the refusal the model reads is the true one. What
changed is that missing that deadline is now the surface's own report
rather than a fabricated tool timeout.

It owns one more thing, and it is the shape that hangs. **Whatever
answers an :class:`ApprovalRequest` must be able to make progress while
the consumer of ``execute_tool_calls`` is suspended between events** — a
separate Task, or an ``await`` on the request queue from inside the
``async for`` body. A consumer that only *polls* for requests between
events never sees one, because the request is issued after it has gone
back to waiting for the next event. Before the pause, such a consumer was
released by ``tool_timeout`` — with the false sentence above — so this is
a hazard the pause exposed rather than one it introduced. The reference
harness has the same hazard in its own shape and spends eight lines on it
at ``stream.go:51-58``.

**Leaf-adjacent.** ``omicsclaw.tools.base`` and the standard library.

```

## omicsclaw/tools/builtin/read.py

```text
``read_file`` — how the model looks at a file it is allowed to see.

Plan 0029 §4 Q2b, Q9, Q11 and traps 7, 10 and 11. The reference is
harness9's ``internal/tools/read_file.go``; what was taken from it is its
**structure** — two modes, a line-number prefix, and truncation notices
that tell the model what to send next — while every *literal* is
re-derived below under Python semantics, because a number that was true
of ``bufio.Scanner`` is not thereby true of :func:`open`.

**Two modes, and the precedence is deliberate.** ``start_line`` selects
line mode and makes ``offset``/``limit`` inert
(``read_file.go:127-130``); with no ``start_line`` the call is byte mode.
Line mode is the one a model should reach for — a line number is what it
will cite back, and what ``edit`` will be asked about — so byte mode is
documented as the fallback for the files line mode refuses (see
:data:`MAX_LINE_CHARS`) rather than as an equal option.

**The line-number prefix is a cross-tool contract, not decoration.**
``read`` renders ``%6d\t%s`` (:func:`numbered_line`,
``read_file.go:218``) and ``edit`` — a later step — has to recognise and
strip it, because a model that pastes a line straight back into
``source_text`` pastes the number too. harness9 says so twice, in the
tool description and again in a code comment (``read_file.go:64-67`` and
``215-218``), and the repetition is the point: the two tools ship
separately, and whoever writes ``edit`` will read this file's description
even if they never read plan 0029. Both copies are reproduced here, and
``tests/tools/test_read.py`` pins the format and pins that the warning is
still in the description.

**Trap 7 — a missing file is an error; an empty file is not.** The
criterion is not "did something go wrong", it is **what should the model
change next**. A path that does not resolve to a readable file is
something the model can fix by sending a different ``path``, so it is
raised and the registry turns it into ``is_error=True``. A file that
exists and happens to be empty is a fact about the world with nothing for
the model to correct, so it comes back as ordinary output. Both halves
are pinned, adjacently, in ``tests/tools/test_read.py``. (The other half
of plan 0029's trap 7 — a non-zero exit from ``bash`` being
``is_error=False`` — belongs to the task that delivers ``bash``; the two
could not be put in one file because they ship separately.)

**An** :exc:`OSError` **from the filesystem is rephrased, never leaked.**
``Workspace.resolve`` deliberately does not touch the filesystem for a
non-existent leaf, so a 5,000-character filename resolves cleanly and
then fails at :func:`os.stat` with ``ENAMETOOLONG``. That is correctable
— the model must send a shorter path — so it is caught and reported as a
sentence naming the operating system's own reason, rather than allowed to
reach the model as a bare ``OSError`` traceback fragment.

**Why this one is a** :class:`~omicsclaw.tools.function_tool.FunctionTool`
**and ``write`` is not.** Plan 0029 §5 says all three foundation tools
should be hand-written, and gives one reason: they are ``ASK`` tools, and
an approval prompt has to show the bytes the model actually sent, which
:class:`~omicsclaw.tools.function_tool.FunctionTool` cannot do. That
reason does not apply here, because plan 0029 Q2b makes ``read`` ``AUTO``
— it never asks anyone anything, so it never needs the raw payload. What
it does need is the other half of the adapter: an undecodable payload
reported with a character count and an excerpt (plan 0028 trap 8) and
every schema problem listed at once (trap 9), in wording byte-identical
to the fifty tools already in service. Hand-writing it would mean
re-deriving both, and plan 0028's ``save_gene_panel`` is the recorded
evidence of how that goes: its private copy of ``decode_arguments`` had
already dropped the excerpt, which is why that helper was made public.
(The tool itself was removed with the other two reference tools; the
lesson it paid for is the reason this one is a ``FunctionTool``.)

The five arguments are exactly a Python signature, so
``func(**arguments)`` makes the schema and the code two statements of
one thing.

**The ``Environment`` seam (plan 0029 Q11), at the width that matters.**
Isolation that covers ``bash`` but not the file tools is decoration — a
model wanting out would simply use ``write``. So this tool takes an
``environment`` too, and *uses* it: ``None`` means the local filesystem,
and anything else has its :meth:`FileReadEnvironment.read_file` awaited
instead. harness9 stores the environment on its file tools and never
reads it (``read_file.go:35-37`` is a TODO), which would be dead state
here and is what plan 0029 trap 15 forbids. The Protocol is narrowed to
the one method this tool calls rather than copying harness9's five-method
``sandbox.Environment``: Protocols are structural, so one object
implementing the whole shape satisfies this one and ``write``'s and, in
due course, ``bash``'s — and a test double for ``read`` is not forced to
stub a shell. **No isolated implementation is written here**, per plan
0029 Q11; in particular harness9's ``LocalEnvironment`` is not copied,
for the reason that plan gives.

**Compared against** ``omicsclaw/runtime/tools/builders/engineering.py``
``file_read`` (line 601), which this layer may not import (plan 0028
§9-4). Plan 0029 §6 requires every row to be accounted for:

* ``_validate_file_read_input`` (``engineering.py:44``) rejects
  ``end_line < start_line``. **Kept**, and with a better claim to exist
  than the reference has: harness9 lets that combination through, reads
  nothing, and reports "start_line is past the end of the file", which
  points the model at the wrong argument. Same check, this layer's
  wording.
* The line-number prefix is ``f"{index}: {line}"`` there and ``%6d\t%s``
  here. **Changed deliberately**, per plan 0029 §6: a tab is one
  separator to strip, while ``": "`` also occurs inside real source
  lines. *What breaks on migration*: anything that parsed the old form,
  and the two header lines the old tool prepended (``File: <path>`` and
  ``Lines: A-B of N``), which are gone — a caller that wants the total
  line count now learns it only when it reads past the end. Nothing in
  this repository parses either; the strings are quoted only in prose
  (``runtime/agent/loop_pathology.py`` keys on the *tool name*, not on
  its output).
* ``max_chars`` (``_bounded_int(..., maximum=100000)``) has no
  counterpart in harness9, which lets a model ask for any number of
  bytes. **Kept** as :data:`MAX_LIMIT_BYTES`; dropping it would be a
  silent capability regression on the one axis this project cares about,
  the context window. It clamps rather than refuses, and the truncation
  notice states the number of bytes actually read, so the model is told.
  Two differences inside the "kept": the old one counts **characters of
  the rendered, numbered output** while ``limit`` counts **bytes of the
  file**, and the old default of 12,000 becomes :data:`MAX_READ_BYTES`.
* **The old tool reports failure as ordinary output** — it returns
  ``"Error: file not found or outside allowed roots: …"`` as the tool's
  result, so the run never sees ``is_error``. This one raises, and the
  registry marks the Observation. That is plan 0029 trap 7, and it is a
  behaviour change the migration has to expect: a model that had learned
  to read the word "Error" out of a successful result will instead get a
  result that says it failed.
* ``_bounded_int`` coerces an unusable argument to a default —
  ``start_line="abc"`` silently becomes 1. Here the schema names the
  argument and the type, because a tool that answers a different question
  than the one it was asked is worse than one that asks again.
* ``start_line``/``end_line`` defaulting to the whole file is **not**
  kept: the old tool reads every line and then truncates a character
  budget, so a 200,000-line file is fully read into memory first. Line
  mode here is bounded before reading, by :data:`MAX_LINES`.
* Multi-root discovery (``_resolve_path_for_read`` tries the workspace,
  the pipeline workspace, the project root and trusted data roots in
  turn) is **not** kept. One workspace, one answer — see
  ``omicsclaw/tools/_workspace.py``, which records the same decision.

**Leaf-adjacent.** ``omicsclaw.tools._pathlock``,
``omicsclaw.tools._workspace``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``, and the
standard library.

```

## omicsclaw/tools/_workspace.py

```text
The sandbox boundary ``read``, ``write``, ``edit`` and ``bash`` share.

Plan 0029 §3 and §4 Q1/Q8/Q10. One question is answered here, once: **a
model sent a path — which real file, if any, is that?** Every tool that
touches the filesystem asks it, and a tool that answers it for itself is
a tool that answers it slightly differently, which is how one of four
tools ends up being the one that can be talked out of the workspace.

The attack this exists to stop is one line long::

    workspace = /home/alice/run-7
    the model sends "../../../etc/passwd"

and the defence is that :meth:`Workspace.resolve` is the only way in. It
applies three rules to every path, in this order:

1. **Resolve it** — ``..`` normalised *and symlinks followed*, so what is
   judged is the file that will actually be opened rather than the name
   the model chose for it.
2. **Refuse credentials outright** — :func:`is_sensitive`, applied to the
   resolved path, and applied whether or not the path is inside the
   workspace. A workspace containing an SSH key does not make the key
   readable.
3. **Require containment** — the resolved path must be the workspace root
   or live under it.

Rules 2 and 3 are in that order on purpose, and it is the one ordering
difference from the reference implementation. ``<workspace>/creds`` that
turns out to be a symlink into ``~/.ssh`` fails both rules; reporting it
as "outside the workspace" would file a credential-access attempt under
the same heading as a model that mistyped a relative path. The operator
reading the log needs those to look different, so the sharper rule
answers first.

**What came from harness9, and in what form.**
``internal/tools/safe_path.go`` is the reference. Its *structure* is kept
whole — one shared resolver, a hard credential list that the workspace
whitelist cannot override, and a containment check that cannot be fooled
by a shared name prefix. Two of its *literals* are deliberately not
translated, because both are artefacts of Go's standard library rather
than of the problem:

*The separate branch for absolute input* (``safe_path.go:72-87``) exists
because ``filepath.Join(workDir, "/abs")`` yields ``/workDir/abs`` — Go's
``Join`` does not treat a rooted second argument as rooted, so an
absolute path that was already inside the workspace came back doubled,
passed the prefix check, and named a file that does not exist. Python's
``/`` operator has the opposite convention: ``Path("/ws") / "/etc/passwd"``
is ``/etc/passwd``, not ``/ws/etc/passwd``. So the branch would be a
branch whose two arms compute the same value, and translating it would
add an untestable line while the behaviour it protects — an absolute path
is never concatenated onto the root — is already free. The behaviour is
what is pinned, by
``test_an_absolute_path_inside_the_workspace_is_returned_undoubled``.

*The ``strings.HasPrefix(abs, workDir + os.PathSeparator)`` comparison*
(``safe_path.go:90-92``) is the same rule as :meth:`Path.is_relative_to`,
which compares path components and therefore cannot be tricked by
``/project-evil`` beginning with ``/project``. Keeping the string form in
Python would mean re-deriving the separator handling that pathlib already
gets right. The trap is kept as a test — swap :func:`_is_within` for a
``str.startswith`` and
``test_a_sibling_sharing_the_workspaces_name_is_not_inside_it``
goes red.

**Three ways this is stricter than harness9.**

*Symlinks are resolved* (plan 0029 §4 Q10). ``safePath`` uses
``filepath.Abs``, which normalises ``..`` lexically and follows nothing,
so a symlink planted inside the workspace and pointing anywhere at all
passes its check. :meth:`Path.resolve` follows links, so it does not.
**This is a net improvement and not an alignment defect** — written down
here because the obvious way to "match the reference" later is to swap in
``os.path.abspath``, which would silently reopen the hole.
``strict=False`` is the default and is wanted: ``write`` names files that
do not exist yet, and a non-existent *leaf* is fine, while a symlinked
*parent* is resolved and caught before anything is created.

*The credential list is not anchored to a home directory.* harness9 builds
``~/.ssh``, ``~/.aws`` and the rest from ``os.UserHomeDir`` and returns an
**empty list** when that fails (``safe_path.go:27-29``), on the grounds
that bash's DangerHook is the backstop. This project has no DangerHook,
so an empty list would be a silent total loss of the rule. Rather than
handling the failure, the dependency is removed: a path is credential-ish
because of the components in it, not because of where the current user's
home happens to be. That is strictly broader than the anchored form — it
also catches a second user's home, a copied ``.aws`` inside the workspace
and a container image with no ``HOME`` set — and it makes the "cannot find
home" branch unreachable rather than dangerous.

*The list is re-derived for this project.* ``~/.kube`` stays because it
costs nothing, and ``.env`` is added because it is where **this**
repository keeps ``LLM_API_KEY``, ``TELEGRAM_BOT_TOKEN`` and
``FEISHU_APP_SECRET`` (see ``.env.example``). An omics agent
that can read one ``.env`` can impersonate the bot it is running as.

**Compared against** ``omicsclaw/services/path_validation.py``, which does
this job for the layer being replaced and which this layer may not import
(plan 0028 §9-4 — ``tests/tools/test_tools_is_a_leaf_layer.py`` enforces
it, whitelist, AST, dynamic-import and behaviour probes). Its
``validate_path(filepath, allowed_root)`` is the overlapping half, and on
the overlap the two **agree exactly**: both resolve each side before
comparing, both compare by component so a shared name prefix is not
containment, both accept the root itself, both accept a path that does
not exist yet, and both refuse a path containing a NUL byte — the legacy
one because :meth:`Path.resolve` raises :exc:`ValueError` and it catches
``ValueError`` to mean "no". ``tests/tools/test_workspace.py`` runs both
over a shared corpus and asserts zero disagreements, loading the legacy
module by file path so that assertion does not become an import.

Four differences remain, all deliberate:

* *Where a relative path is joined.* ``validate_path`` takes an
  already-joined path, so a relative one resolves against the **process
  CWD**; ``resolve`` joins to the workspace root and never consults the
  CWD. A boundary that moves when something calls :func:`os.chdir` is not
  a boundary.
* *Credentials.* ``validate_path`` has no notion of them: an SSH key
  inside a trusted root is allowed. Rule 2 above has no equivalent there.
* *What a refusal is.* ``validate_path`` returns ``False`` and
  ``validate_input_path`` returns ``None``; both lose which rule fired.
  Here a refusal is a typed exception carrying a sentence the model can
  act on — and the two subclasses are distinguishable for an operator,
  for the reason given above.
* *One root, not a search path.* ``validate_input_path`` tries the
  Desktop workspace, then the project root, then every trusted data dir,
  and additionally requires the file to exist. That is a *discovery*
  policy, and it belongs to whatever binds :data:`WORKSPACE_KEY` rather
  than to the boundary. ``resolve_dest`` goes further still and **falls
  back to a default directory** when a path escapes, which is the one
  behaviour deliberately not carried over: silently writing somewhere
  else is how a model learns that its escape attempt "worked".

**Leaf.** The standard library, and nothing else — not even the rest of
``omicsclaw.tools``. Whether a refused path is reported to the model as a
correctable argument error is the calling tool's decision, so this module
declines to make it.

```

## omicsclaw/tools/builtin/write.py

```text
``write_file`` — the first tool in this layer that can destroy something.

Plan 0029 §4 Q2, Q11 and traps 3, 5, 7 and 12. The reference is
harness9's ``internal/tools/write_file.go``, whose whole design is three
sentences in its package comment (``write_file.go:3-9``) and all three
are kept: the path goes through the sandbox boundary, missing parent
directories are created, and an existing file is **replaced**.

**Overwrite semantics are a fact the model has to be told** (plan 0029
trap 12). ``write_file.go:8-9`` says it and says why: the behaviour
matches :func:`os.WriteFile`, there is no append and no merge, and
deciding whether to read the file first is the *model's* job because the
tool will not do it. That sentence is in :data:`_DESCRIPTION` for the
model and in :meth:`WriteTool._reason` for the human, and the second is
the one that changes an approval prompt from "may I write a file" into
"may I replace the 4,000 bytes currently at this path".

**Hand-written against the** :class:`~omicsclaw.tools.base.Tool`
**Protocol, and the reason is the approval prompt** (plan 0029 §5, plan
0028 §11). :class:`~omicsclaw.tools.function_tool.FunctionTool` calls its
function with decoded keyword arguments, so a function wrapped by it can
only show a human something re-encoded from what survived decoding — key
order, spacing and any field the schema did not mention are gone, and
those are exactly the differences a person reviewing a destructive call
is looking at. ``execute`` here is handed the payload unparsed and hands
that same string to
:func:`~omicsclaw.tools.context.require_approval`. This and ``bash`` are
the "some tools must be hand-written" set: both ask a human, so both need
the bytes the model actually sent rather than the arguments a decoder
reconstructed from them.

**Order of operations, which is the design.** Decode, validate, resolve,
**ask**, then write. Nothing before the approval touches the filesystem
in a way that survives — resolution reads, it does not create — so a
refusal at any point leaves no file behind. Resolving *before* asking is
not an optimisation either: it is what lets the prompt name the real path
the bytes will land at rather than the string the model chose, which for
a path containing a symlink are different places.

**Trap 3 is this file's to carry, and the helper layer cannot carry it.**
:meth:`~omicsclaw.tools._workspace.Workspace.resolve` follows symlinks,
so a link inside the workspace pointing at ``/tmp`` resolves to ``/tmp``
and is refused — but nothing in that helper can force *this* module to
call it before :meth:`~pathlib.Path.mkdir`, and ``mkdir(parents=True)``
on an unresolved path would create directories out in ``/tmp`` before any
check ran. So the test that matters is not "the tool returned an error";
it is **"the external directory is still empty"**, and
``tests/tools/test_write.py`` asserts that.

**Trap 7 — the criterion is what the model should change next.** Every
failure this tool produces is correctable by sending different arguments:
a path outside the workspace, a directory where a file was meant, a name
longer than the filesystem allows. All of them raise, and
:meth:`~omicsclaw.tools.registry.ToolRegistry.execute` turns each into
``is_error=True`` so the model reads the reason and retries differently.
A denied approval is the one refusal that is *not* correctable by new
arguments, and it says so in its own words — it comes from
:exc:`~omicsclaw.tools.context.ApprovalDenied`, not from here.

**Trap 5 — cancellation is not a failure to report.** Every ``except`` in
this module names :exc:`OSError`. The engine's per-tool deadline arrives
as :exc:`asyncio.CancelledError`, which is a :exc:`BaseException`, and it
must pass through untouched: a human still deciding when the budget
expires (a collision ``omicsclaw/tools/context.py`` documents in full)
must not be reported to the model as a broken tool.

**The ``Environment`` seam (plan 0029 Q11).** ``environment=None`` means
the local filesystem. The width of that seam is the point of the
decision: if only ``bash`` ran under isolation while ``write`` wrote
straight to the host, the isolation would be decoration a model could
step around by choosing the other tool. So this tool takes one too and
uses it. :class:`FileWriteEnvironment` states the contract that harness9's
own interface leaves implicit — an implementation must create parent
directories itself, because ``sandbox.Environment`` has no ``mkdir`` and
the auto-mkdir promise in the tool description does not stop being made
when a call is routed.

**Compared against** ``omicsclaw/runtime/tools/builders/engineering.py``
``file_write`` (line 772), which this layer may not import. Plan 0029 §6
requires every row to be accounted for:

* Auto-mkdir: both do it. The old tool exposes it as a ``create_dirs``
  argument defaulting to ``true``; that argument is **dropped**, matching
  harness9, because its ``false`` branch only turns a working write into
  "Error: parent directory does not exist" — a refusal the model answers
  by setting the flag it should not have had to think about.
* Return text: ``"Wrote file: {target}"`` there, a byte count and the
  created/overwrote distinction here. The old form cannot tell a model
  whether it just destroyed something.
* Multi-root discovery (``_resolve_path_for_write`` falls back to
  ``allowed_roots[0]``) is **not** kept, for the reason
  ``_workspace.py`` gives about ``resolve_dest``: writing somewhere other
  than where the model asked is how it learns that an escape attempt
  worked.
* Encoding: both write UTF-8 text. The old tool uses
  :meth:`~pathlib.Path.write_text` with default permissions; this one
  opens with :data:`FILE_MODE` explicitly, matching
  ``write_file.go:113``.
* No path locking there, and no equivalent anywhere in the old layer
  except the serial barrier in ``runtime/tools/orchestration.py`` that
  the new engine does not have — see ``omicsclaw/tools/_pathlock.py``.
* **The old tool reports failure as ordinary output** (``"Error: cannot
  write outside allowed roots: …"``), so a refused write reaches the run
  as a success. This one raises and the registry marks ``is_error``.
* **No approval in practice.** The old layer has the machinery —
  ``ToolSpec.approval_mode`` and ``orchestration.py``'s policy decision —
  but ``file_write``'s spec leaves ``approval_mode`` at its ``auto``
  default and declares only ``risk_level=medium`` plus a speculative
  classifier, so the whole-workspace write runs unattended. Here the ask
  is in ``execute``, before the bytes, and is not optional.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools._pathlock``,
``omicsclaw.tools._workspace``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``,
``omicsclaw.tools.builtin.read``, and the standard library.

```

## omicsclaw/tools/_pathlock.py

```text
One lock per file, held on the event loop rather than on a thread.

Plan 0029 §4 Q7. Two coroutines writing the same file is not a race this
layer might one day have — it is a race it has **today**, and the lost
update is real.

Part of it is now stopped upstream. ``engine/executor.py`` reads
:attr:`~omicsclaw.tools.base.ToolPolicy.concurrency_safe` through
:meth:`~omicsclaw.tools.registry.ToolRegistry.is_concurrency_safe` and
runs a call that has not claimed it as a **barrier**, alone, so two
``write`` calls the model emits in one assistant turn are ordered by the
scheduler — plan 0028 §11 debt #1, closed.

What remains is everything that is not one turn, and this module is the
only defence there is for it:

* **Overlapping turns.** The barrier orders the calls of the turn it is
  scheduling and knows nothing about a second one. Two Channel
  conversations, a sub-agent, a background run — each drives its own
  ``execute_tool_calls`` on the same event loop and the same files.
* **A tool that declared itself safe and is not.** ``concurrency_safe``
  is the author's claim, and the scheduler has no way to check it.
* **Concurrency inside one call.** A tool that fans out its own writes is
  one call to the barrier.

Which is why it ships with the first tool that can write rather than with
the optimisation pass it looks like.

**asyncio primitives, and the reason is not taste.** The reference
implementation (``harness9/internal/tools/path_locker.go``) uses
``sync.RWMutex`` because a goroutine can be preempted between any two
instructions, so a real mutual-exclusion primitive is the only thing that
makes a section atomic. (Not because goroutines are OS threads — they are
multiplexed onto them, and a blocked one yields its thread to the
runtime.) Ours are Tasks on one event loop, preempted only at an
``await``, and a :class:`threading.Lock` there is wrong in both
directions at once:

* ``with threading.Lock():`` around an ``await`` does not make the
  section atomic. The lock is released only when the block exits, but the
  coroutine *yields* at every ``await`` inside it, so a second coroutine
  runs in the middle of a "protected" read-modify-write. It looks like a
  lock and is not one.
* And when the second coroutine reaches ``lock.acquire()``, it blocks the
  **thread** — which is the event loop — so nothing can ever release it.
  A single-threaded deadlock, from a lock that appeared to be working
  moments earlier.

:class:`asyncio.Condition` has neither failure: a waiter suspends the
Task and returns control to the loop.
``test_the_event_loop_keeps_running_while_a_task_waits_for_a_lock``
pins the second half, because it is the half that turns into a hang
rather than into a wrong answer.

**The key is the resolved path, never the string the model sent.**
``./results/a.csv``, ``results/../results/a.csv`` and
``/work/run-7/results/a.csv`` are one file with three names, and locking
the names takes three different locks — a lock that is held, that costs
something, and that protects nothing. :func:`_key` resolves before
looking anything up, so aliases collapse. Note that harness9 keys on
``filepath.Clean`` (``path_locker.go:40``), which normalises ``.`` and
``..`` but does **not** make a path absolute; it is safe there only
because its three callers — ``read_file.go:124``, ``write_file.go:104``
and ``edit_file.go:110`` — all happen to pass ``safePath`` output. Doing the
normalisation inside the table rather than trusting callers is the one
place this deliberately does not copy the reference.

**Reference counting, straight from the reference, for the reason given
there** (``path_locker.go:12-16``): an entry is created on first use and
**deleted when the last holder leaves**, so a long-running agent that
touches a million paths does not accumulate a million locks. The count is
incremented *before* acquiring and decremented *after* releasing —
crucially covering the waiting period, because an entry evicted while
somebody is queued on it would be recreated by the next arrival, and two
lock objects for one path is the aliasing bug wearing different clothes.

The same counting buys something Go had no need of: because a table entry
never outlives its last holder, an :class:`asyncio.Condition` created on
one event loop is never found by a later one. Repeated
:func:`asyncio.run` calls — which is what this project's test suite is,
:mod:`pytest-asyncio` not being installed — would otherwise hit
``RuntimeError: ... is bound to a different event loop`` on the second
run.

**The table itself takes no mutex, where the Go version takes one.**
``pathLocksMu`` guards against two OS threads mutating the map at once.
Here, ``_claim`` and ``_release`` contain no ``await``, and a coroutine
cannot be preempted except at an ``await``, so each is already atomic
with respect to every other Task on the loop. The assumption that buys
this is **one event loop per process**, which is what every surface in
this repository does (one uvicorn loop for Desktop, one loop for the
Channel runner). A deployment that ran two loops in one process would get
a loud ``RuntimeError`` out of asyncio's own loop binding rather than a
silently unsynchronised lock — the better of the two failures, and the
reason a :class:`threading.Lock` is not added "just in case": guarding
the dict while leaving the condition variable loop-bound would hide the
loud failure without fixing anything.

**Writer preference, matching ``sync.RWMutex``.** A reader that arrives
while a writer is queued waits. Without that, a tool loop doing many
reads and one write can starve the write indefinitely, and Go's RWMutex
blocks new readers once a writer is waiting for exactly this reason.

**Not re-entrant.** Taking a read lock and then a write lock on the same
path from one Task deadlocks — there is no owner tracking, and there
should not be, because the operation that needs both is a single
read-modify-write that should hold **one** write lock for its whole
length. That is a note for ``edit`` (plan 0029 §12), which is the first
tool shaped that way.

**Leaf.** The standard library, and nothing else.

```
