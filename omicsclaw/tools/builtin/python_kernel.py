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
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Callable, Protocol, runtime_checkable

from omicsclaw.schema import ToolDefinition

from ..base import ApprovalMode, RiskLevel, ToolPolicy
from ..context import context_value, report_progress, require_approval
from ..function_tool import ToolArgumentError, decode_arguments, validate_arguments

TOOL_NAME = "python"

_DESCRIPTION = (
    "Run a Python cell on this session's persistent IPython kernel and "
    "return a summary (stdout tail, errors, figures, memory usage). "
    "Variables persist between calls: something assigned in one python "
    "call is still in scope in the next, in the same session. Use this "
    "for analysis steps that build on earlier state (load data once, "
    "then cluster, then plot); use `bash` for short shell commands "
    "(ls, file inspection, CLI tools) — do not route those through here. "
    "Plots are captured automatically: anything the cell displays as an "
    "image is written to figures/ and registered as an artifact. "
    "`code` is the Python source of the cell. `description` is a "
    "one-line note of what the cell is for."
)

PYTHON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "Python source to execute in the session kernel. May "
                "reference variables defined by earlier python calls."
            ),
        },
        "description": {
            "type": "string",
            "description": "One-line note shown while the cell runs.",
        },
        "timeout_secs": {
            "type": "number",
            "minimum": 1,
            "description": (
                "Cell budget in seconds; defaults to 45. A cell that "
                "needs longer than the engine's per-call budget belongs "
                "on the jobs plane (code_run)."
            ),
        },
    },
    "required": ["code"],
    "additionalProperties": False,
}

DEFAULT_TIMEOUT = 45.0
ENGINE_TIMEOUT_MARGIN = 15.0
"""The ``bash`` arithmetic, copied on purpose: this tool's budget plus
this margin must stay under ``EngineConfig.tool_timeout`` (60 s default).
The coupling is held by a test this module cannot see, for the layering
rule the ``bash`` docstring spells out."""

_POLICY = ToolPolicy(
    risk_level=RiskLevel.HIGH,
    approval_mode=ApprovalMode.ASK,
    prompts_for_itself=True,
    read_only=False,
    concurrency_safe=False,
    writes_workspace=True,
    allowed_in_background=False,
    tags=frozenset({"workspace", "kernel", "mutation"}),
)
"""The ``bash`` claim, verbatim category: arbitrary code, the approval
gate is the boundary. ``concurrency_safe=False`` because two cells on
one session kernel are serialised by the manager anyway — pretending
they could race productively would only mis-schedule the turn."""


@runtime_checkable
class KernelRunner(Protocol):
    """Where the cell actually runs. Injected, never imported.

    The tools layer is a leaf; the kernel manager is infrastructure.
    This one-method seam is the whole contract: run *code* on the
    session's kernel, hand output lines to *on_output* as they stream
    (from a worker thread; the tool owns the hop back to the loop), and
    answer with the summary text the caller reads.
    """

    async def run_python(
        self,
        code: str,
        *,
        description: str,
        session_id: str,
        on_output: Callable[[str], None] | None = None,
        timeout_s: float | None = None,
    ) -> str: ...


class _StreamGate:
    """Coalesce a thread-side stream into ~4 progress reports a second.

    A runaway ``print`` loop would otherwise turn one cell into hundreds
    of ``tool_output`` frames; the kernel-side 10 MB streaming ceiling
    eventually stops the source, and this keeps the channel quiet until
    then. Pending text is never dropped — only delayed.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._buffer: list[str] = []
        self._last = 0.0

    def __call__(self, text: str) -> None:
        self._buffer.append(text)
        now = time.monotonic()
        if now - self._last < 0.25:
            return
        self._last = now
        payload = "".join(self._buffer)[-2000:]
        self._buffer.clear()
        self._loop.call_soon_threadsafe(
            asyncio.ensure_future, report_progress(payload, tool_name=TOOL_NAME)
        )

    def flush(self) -> None:
        if not self._buffer:
            return
        payload = "".join(self._buffer)[-2000:]
        self._buffer.clear()
        self._loop.call_soon_threadsafe(
            asyncio.ensure_future, report_progress(payload, tool_name=TOOL_NAME)
        )


class PythonKernelTool:
    """One cell on the session's persistent kernel.

    Satisfies :class:`~omicsclaw.tools.base.Tool` structurally, exactly
    as ``save_artifact`` does. ``runner`` is the injected kernel seam;
    omitting it constructs a tool that refuses at execution time.
    """

    policy = _POLICY

    def __init__(self, *, runner: KernelRunner | None = None) -> None:
        self._runner = runner
        self._definition = ToolDefinition(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            input_schema=copy.deepcopy(PYTHON_SCHEMA),
        )

    @property
    def name(self) -> str:
        return TOOL_NAME

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = decode_arguments(arguments)
        issues = validate_arguments(decoded, PYTHON_SCHEMA)
        if issues:
            listed = "\n".join("  - " + issue for issue in issues)
            raise ToolArgumentError(
                "the arguments do not match this tool's schema:\n"
                f"{listed}\nRe-send the call with all of these corrected."
            )
        code = str(decoded["code"])
        if not code.strip():
            raise ToolArgumentError(
                "input.code is required and must be Python source, not whitespace"
            )
        description = decoded.get("description")
        description = description.strip() if isinstance(description, str) else ""
        timeout = decoded.get("timeout_secs")
        timeout = float(timeout) if isinstance(timeout, (int, float)) else None
        if timeout is not None:
            timeout = min(max(timeout, 1.0), DEFAULT_TIMEOUT)

        head = "\n".join(code.splitlines()[:6])[:400]
        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=f"run a Python cell on this session's kernel:\n{head}",
        )

        if self._runner is None:
            raise RuntimeError(
                "python has no kernel runner bound; the surface assembling "
                "the persistent kernels must provide one"
            )
        session_id = str(context_value("session_id", "") or "") or "default"
        gate = _StreamGate(asyncio.get_running_loop())
        try:
            return await self._runner.run_python(
                code,
                description=description,
                session_id=session_id,
                on_output=gate,
                timeout_s=timeout,
            )
        finally:
            gate.flush()


__all__ = [
    "DEFAULT_TIMEOUT",
    "ENGINE_TIMEOUT_MARGIN",
    "KernelRunner",
    "PYTHON_SCHEMA",
    "TOOL_NAME",
    "PythonKernelTool",
]
