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
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .handles import HandleRegistry
from .reaper import KernelReaper
from .session import CellResult, FigureRef, KernelCallbacks, OutputLimits, SessionKernel

__all__ = [
    "CANCEL_GRACE_S",
    "PersistentKernelManager",
    "SessionState",
    "SUMMARY_CELL_CODE",
    "format_cell_summary",
]

_log = logging.getLogger(__name__)

CANCEL_GRACE_S = 10.0
"""Tier 2 of the cancel ladder: how long an interrupt is given to land."""

DEFAULT_CELL_TIMEOUT_S = 1800.0
"""The retained default cell ceiling — 30 min, per the plan's explicit
refusal of CS's "no default timeout" gap."""

RESTART_AFTER_CELLS = 200
RESTART_RSS_KB = 6 * 1024 * 1024
"""6 GiB peak RSS: a fraction of the container, sized above a normal
scanpy session's working set so the signal means bloat, not work."""

SUMMARY_CELL_CODE = (
    "_ov_lines = []\n"
    "for _ov_n in list(globals()):\n"
    "    if _ov_n.startswith('_') or _ov_n in ('In', 'Out', 'exit', 'quit',"
    " 'get_ipython'):\n"
    "        continue\n"
    "    try:\n"
    "        _ov_v = globals()[_ov_n]\n"
    "    except Exception:\n"
    "        continue\n"
    "    _ov_t = type(_ov_v).__name__\n"
    "    if _ov_t == 'AnnData' or (hasattr(_ov_v, 'n_obs') and"
    " hasattr(_ov_v, 'n_vars') and hasattr(_ov_v, 'obs')):\n"
    "        try:\n"
    "            _ov_lines.append('%s: AnnData %dx%d' % (_ov_n, _ov_v.n_obs,"
    " _ov_v.n_vars))\n"
    "        except Exception:\n"
    "            _ov_lines.append(_ov_n + ': AnnData')\n"
    "    elif hasattr(_ov_v, 'shape'):\n"
    "        try:\n"
    "            _ov_lines.append('%s: %s shape=%s' % (_ov_n, _ov_t,"
    " tuple(_ov_v.shape)))\n"
    "        except Exception:\n"
    "            _ov_lines.append(_ov_n + ': ' + _ov_t)\n"
    "    else:\n"
    "        _ov_lines.append(_ov_n + ': ' + _ov_t)\n"
    "print('\\n'.join(_ov_lines[:200]))\n"
)
"""The namespace summary run before a planned recycle: names, types,
shapes, AnnData first-class. Every helper it defines starts with ``_ov_``
so the summary never lists itself."""

NOTICE_HEAD = "[kernel restarted for this session"
NOTICE_TAIL = (
    "Variables from the previous kernel are gone. Prefer restoring from "
    "saved artifacts/files (re-read the h5ad into OV_KERNEL_ADATA) over "
    "recomputing long steps.]"
)


@dataclass(slots=True)
class SessionState:
    """The manager-side record of one session's kernel."""

    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kernel: SessionKernel | None = None
    obituary: str = ""
    obituary_pending: bool = False
    born_at: float = 0.0
    last_activity: float = field(default_factory=time.time)
    cells: int = 0
    peak_rss_kb: int | None = None
    restart_needed: str = ""
    protected: set[str] = field(default_factory=set)
    executing: bool = False
    current_job: str = ""
    last_error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "alive": bool(self.kernel is not None and self.kernel.is_alive()),
            "executing": self.executing,
            "protected": sorted(self.protected),
            "cells": self.cells,
            "age_s": round(time.time() - self.born_at, 1) if self.born_at else None,
            "idle_s": round(time.time() - self.last_activity, 1),
            "peak_rss_kb": self.peak_rss_kb,
            "restart_needed": self.restart_needed,
            "obituary_pending": self.obituary_pending,
        }


class PersistentKernelManager:
    """Owns every session kernel, the handle registry and the reaper."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        limits: OutputLimits | None = None,
        cell_timeout_s: float = DEFAULT_CELL_TIMEOUT_S,
        on_figure: Callable[[Path, str, str, str], Any] | None = None,
        reaper: KernelReaper | None = None,
        python: str | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.limits = limits or OutputLimits()
        self.cell_timeout_s = float(cell_timeout_s)
        self.python = python
        self.handles = HandleRegistry()
        self.states: dict[str, SessionState] = {}
        self._on_figure = on_figure
        self._reaper = reaper or KernelReaper(self)
        self._reaper_task: asyncio.Task[None] | None = None

    # ---- session bookkeeping ----

    def state_for(self, session_id: str) -> SessionState:
        state = self.states.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            self.states[session_id] = state
        return state

    def snapshot(self) -> list[dict[str, Any]]:
        return [state.snapshot() for state in self.states.values()]

    def protect(self, session_id: str, reason: str) -> None:
        """Add *session_id* to the reaper's protection set (e.g. a pending
        approval). Idempotent per reason."""
        self.state_for(session_id).protected.add(reason)

    def unprotect(self, session_id: str, reason: str) -> None:
        state = self.states.get(session_id)
        if state is not None:
            state.protected.discard(reason)

    # ---- execution ----

    async def execute(
        self,
        session_id: str,
        code: str,
        *,
        origin: str = "agent",
        callbacks: KernelCallbacks | None = None,
        timeout_s: float | None = None,
        adata_in: str | None = None,
        adata_binding: str = "adata",
        job_id: str = "",
    ) -> CellResult:
        """Run *code* on *session_id*'s kernel, starting one if needed.

        *origin* records who asked (``"agent"`` everywhere this round; the
        ``origin == "user"`` screenshot semantics are a documented TODO).
        *adata_in* is an optional host h5ad path run through
        :meth:`HandleRegistry.sync_in` under the session lock, with the
        resident object additionally bound to *adata_binding* in the cell's
        namespace — the job face's input contract.
        """
        self._ensure_reaper()
        state = self.state_for(session_id)
        async with state.lock:
            state.executing = True
            state.protected.add("executing")
            state.current_job = str(job_id)
            try:
                if state.restart_needed and state.kernel is not None:
                    await self._recycle_locked(
                        state, reason=state.restart_needed, with_summary=True
                    )
                attempt = 0
                while True:
                    attempt += 1
                    notice = ""
                    if state.kernel is not None and not await asyncio.to_thread(
                        state.kernel.is_alive
                    ):
                        # The kernel died between cells (killed externally,
                        # or it crashed after the last cell): announce, then
                        # cold-start beneath this same call.
                        self._park_with_obituary(
                            state,
                            summary=None,
                            reason="the previous kernel died before this cell",
                        )
                    if state.kernel is None:
                        await self._start_locked(state)
                    if state.obituary_pending:
                        notice = state.obituary
                        state.obituary_pending = False
                    prefix = ""
                    sync_info: dict[str, Any] | None = None
                    if adata_in:
                        sync_info, prefix = await asyncio.to_thread(
                            self._sync_input, state, adata_in, adata_binding
                        )
                    result = await asyncio.to_thread(
                        state.kernel.execute,
                        prefix + code,
                        timeout_s=float(timeout_s or self.cell_timeout_s),
                        callbacks=callbacks,
                    )
                    if result.status == "dead" and attempt == 1:
                        # A kernel that dies *mid-cell* is not the cell's
                        # fault: park it, and retry the cell once on a
                        # cold-started kernel that carries the notice — the
                        # caller asked for work, and the work has not had a
                        # chance to run. A second death is reported as-is.
                        self._park_with_obituary(
                            state,
                            summary=None,
                            reason="the previous kernel died mid-cell",
                        )
                        continue
                    result.notice = notice
                    if notice:
                        # The plan puts the handover notice at the *top* of
                        # the next cell's stderr — the first thing a model
                        # or a person reads — not in a side channel.
                        result.stderr = notice + "\n" + result.stderr
                    if sync_info is not None:
                        result.usage["adata_sync"] = sync_info
                    state.cells += 1
                    state.last_error = (
                        (result.error or {}).get("evalue", "") if result.error else ""
                    )
                    probe = await asyncio.to_thread(state.kernel.peak_rss_kb)
                    if probe is not None:
                        state.peak_rss_kb = max(state.peak_rss_kb or 0, probe)
                    self._evaluate_restart_signals(state)
                    if result.status == "timeout":
                        # Unknown kernel state after a timeout: reset now so
                        # the notice is ready for the next call.
                        self._park_with_obituary(
                            state,
                            summary=None,
                            reason="the previous kernel was reset after a cell timeout",
                        )
                    return result
            finally:
                state.executing = False
                state.protected.discard("executing")
                state.current_job = ""
                state.last_activity = time.time()

    def _sync_input(
        self, state: SessionState, adata_in: str, binding: str
    ) -> tuple[dict[str, Any], str]:
        """The bridge half that runs on the worker thread, kernel in hand."""
        from .handles import OV_KERNEL_VAR

        path = Path(adata_in).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        adata_id = path.stem[:64] or "adata"
        info = self.handles.sync_in(
            adata_id, path, session_id=state.session_id, kernel=state.kernel
        )
        prefix = (
            f"{binding} = {OV_KERNEL_VAR}[{adata_id!r}]\n"
            f"# adata synced from {path.name}"
            f"{' (unchanged, no re-read)' if info['skipped'] else ''}\n"
        )
        return info, prefix

    def _evaluate_restart_signals(self, state: SessionState) -> None:
        if state.restart_needed:
            return
        if (
            state.cells >= self._reaper.restart_after_cells
            and (state.peak_rss_kb or 0) >= self._reaper.restart_rss_kb
        ):
            state.restart_needed = (
                f"memory signal (peak {state.peak_rss_kb} kB over "
                f"{self._reaper.restart_rss_kb} kB) plus cell count "
                f"({state.cells} over {self._reaper.restart_after_cells})"
            )
            _log.info("session %s marked for kernel restart: %s", state.session_id,
                      state.restart_needed)

    async def _start_locked(self, state: SessionState) -> None:
        def _on_figure(path: Path, mime: str) -> tuple[str, str]:
            artifact_id = ""
            if self._on_figure is not None:
                try:
                    artifact_id = str(
                        self._on_figure(
                            path, mime, state.session_id, state.current_job
                        )
                        or ""
                    )
                except Exception:  # noqa: BLE001 - registration is never a cell fault
                    _log.exception("figure registration failed: %s", path)
            return artifact_id, "figure"

        kernel = SessionKernel(
            state.session_id,
            workspace=self.workspace,
            python=self.python,
            limits=self.limits,
            on_figure=_on_figure,
        )
        await asyncio.to_thread(kernel.start)
        state.kernel = kernel
        state.born_at = time.time()
        state.cells = 0
        state.peak_rss_kb = None
        state.restart_needed = ""

    # ---- the cancel ladder ----

    async def cancel(self, session_id: str) -> str:
        """Interrupt what *session_id* is running. Answers the tier reached.

        ``"interrupted"`` — tier 1/2: the executing task observed the
        interrupt within the grace. ``"reset"`` — tier 3: the kernel was
        killed and its handles dropped; the next execute cold-starts with
        a handover notice. ``"dead"``/``"idle"`` — there was nothing to
        interrupt, or nothing answered even the tier-4 ``pass`` probe.
        """
        state = self.state_for(session_id)
        kernel = state.kernel
        if kernel is None:
            return "idle"
        await asyncio.to_thread(kernel.interrupt)
        deadline = time.monotonic() + CANCEL_GRACE_S
        while state.executing and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not state.executing:
            if not await asyncio.to_thread(kernel.is_alive):
                return "dead"
            probe = await self._probe(state)
            return "interrupted" if probe else "dead"
        # Tier 3: the interrupt did not land inside the grace.
        async with state.lock:
            if state.kernel is kernel:
                self._park_with_obituary(
                    state,
                    summary=None,
                    reason="the kernel was reset after an unresponsive interrupt",
                )
        return "reset"

    async def _probe(self, state: SessionState) -> bool:
        """Tier 4: a ``pass`` cell proving the kernel still answers."""
        kernel = state.kernel
        if kernel is None:
            return False
        try:
            outcome = await asyncio.to_thread(
                kernel.execute, "pass\n", timeout_s=15.0
            )
        except Exception:  # noqa: BLE001 - a probe that raises is a dead kernel
            return False
        return outcome.status == "ok"

    # ---- recycling and death ----

    def _park_with_obituary(
        self, state: SessionState, *, summary: str | None, reason: str
    ) -> None:
        """Kill (if anything is left) and leave an obituary for the next cell."""
        kernel = state.kernel
        if kernel is not None:
            try:
                kernel.kill()
            finally:
                state.kernel = None
        dropped = self.handles.drop_session(state.session_id)
        handle_note = (
            ", ".join(dropped) if dropped else "no registered adata handles"
        )
        if summary is None:
            body = "no namespace summary could be captured (" + reason + ")"
        else:
            body = "last known namespace:\n" + (summary or "(empty namespace)")
        state.obituary = (
            f"{NOTICE_HEAD} — {reason}.]\n{body}\n"
            f"Resident adata handles dropped: {handle_note}.\n{NOTICE_TAIL}"
        )
        state.obituary_pending = True

    async def _recycle_locked(
        self, state: SessionState, *, reason: str, with_summary: bool
    ) -> None:
        """Planned recycle: summary cell first, then the kill."""
        summary: str | None = None
        kernel = state.kernel
        if with_summary and kernel is not None and await asyncio.to_thread(
            kernel.is_alive
        ):
            try:
                outcome = await asyncio.to_thread(
                    kernel.execute, SUMMARY_CELL_CODE, timeout_s=60.0
                )
                summary = outcome.stdout.strip()[:4000] or None
            except Exception:  # noqa: BLE001 - the summary is best-effort
                _log.exception("namespace summary failed for %s", state.session_id)
        self._park_with_obituary(state, summary=summary, reason=reason)
        _log.info("recycled kernel of session %s (%s)", state.session_id, reason)

    # ---- shutdown ----

    async def aclose(self) -> None:
        """Stop the reaper and kill every kernel. Called by app shutdown."""
        self._stop_reaper()
        for state in list(self.states.values()):
            async with state.lock:
                kernel = state.kernel
                state.kernel = None
                if kernel is not None:
                    await asyncio.to_thread(kernel.shutdown)

    # ---- the reaper task ----

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper.run(), name="omicsclaw-kernel-reaper"
            )

    def _stop_reaper(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None

    @property
    def reaper(self) -> KernelReaper:
        return self._reaper


def format_cell_summary(result: CellResult) -> str:
    """The text a caller shows: stdout tail first, then the diagnostics.

    Tail-biased like ``bash``'s truncation for the same reason — the
    traceback and the summary line a computation prints come last.
    """
    parts: list[str] = []
    if result.notice:
        parts.append(result.notice)
    stdout = result.stdout.rstrip()
    if stdout:
        parts.append("stdout:\n" + _tail(stdout, 8000))
    stderr = result.stderr.rstrip()
    if stderr:
        parts.append("stderr:\n" + _tail(stderr, 4000))
    if result.error is not None:
        trace = _tail(result.error.get("traceback", ""), 4000)
        parts.append(
            f"{result.error.get('ename', 'Error')}: {result.error.get('evalue', '')}"
            + (f"\n{trace}" if trace else "")
        )
    if result.figures:
        listing = "\n".join(
            f"- {Path(f.path).name} ({f.artifact_id or 'not registered'})"
            for f in result.figures
        )
        parts.append("figures:\n" + listing)
    usage = result.usage
    parts.append(
        "usage: wall={wall_s}s cpu={cpu_s}s peak_rss={peak_rss_kb}kB".format(**usage)
        if usage
        else ""
    )
    return "\n".join(p for p in parts if p)


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return (
        text[:head]
        + f"\n...[{len(text) - limit} characters elided]...\n"
        + text[-(limit - head):]
    )
