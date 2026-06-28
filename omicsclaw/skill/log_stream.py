"""Context-scoped sink for live skill subprocess log lines.

A surface (e.g. the desktop chat stream) can install a per-request *sink* into
:data:`skill_log_emitter_var`; :mod:`omicsclaw.skill.chain` then forwards every
captured skill stdout/stderr line to that sink (in addition to the
``omicsclaw.skill.chain`` logger) so the surface can stream the lines live to a
client.

Each *skill run* is bracketed by ``begin_skill`` (which returns an opaque run id)
and a series of ``emit`` calls tagged with that run id. The run id — not a tool
call id — is the correlation key, because at skill-execution time the surface
has no reliable signal of which tool call is running (tool-result callbacks fire
only after the whole tool batch finishes; analysis tools run as serial barriers).

The contract is deliberately tiny and dependency-free so it can be imported from
both the low-level skill chain and any surface without import cycles.

Threading note: ``begin_skill`` is called from the coroutine that runs the skill
(where the ContextVar is visible); the returned run id and the resolved sink are
then closed over by value, because the subprocess reader threads that invoke
``emit`` are raw ``threading.Thread``s that do NOT inherit ContextVars. ``emit``
is therefore called from those reader threads and MUST be thread-safe and must
never raise.
"""

from __future__ import annotations

import contextvars
from typing import Optional, Protocol


class SkillLogSink(Protocol):
    """Minimal duck-typed interface a surface installs for live skill logs."""

    def begin_skill(self, skill: str) -> str:
        """Register a new skill run and return its opaque run id."""
        ...

    def emit(self, run_id: str, stream: str, line: str) -> None:
        """Buffer one captured line (``stream`` is "stdout"|"stderr").

        Called from subprocess reader threads — must be thread-safe and never
        raise.
        """
        ...


skill_log_emitter_var: contextvars.ContextVar[Optional[SkillLogSink]] = (
    contextvars.ContextVar("omicsclaw_skill_log_sink", default=None)
)

__all__ = ["SkillLogSink", "skill_log_emitter_var"]
