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

from __future__ import annotations

import asyncio
import logging
import re
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
    "normalize_location",
]

_log = logging.getLogger(__name__)

CANCEL_GRACE_S = 10.0
"""Tier 2 of the cancel ladder: how long an interrupt is given to land."""

_LOCATION_SHAPE: re.Pattern[str] = re.compile(r"\Aremote:[A-Za-z0-9._-]+\Z")


def normalize_location(location: str) -> str:
    """``"local"`` or ``"remote:<alias>"``, or refused.

    The shape check only — one ``remote:`` prefix and one closed-alphabet
    word, no leading dash — because this package cannot import the
    remote layer's validator (the dependency points the other way). The
    runtime, the tool faces and the ingress route all re-check the alias
    with the real validator; this is the first lock, not the only one.
    """
    text = "local" if location is None else str(location)
    if text == "local":
        return text
    if _LOCATION_SHAPE.match(text) and not text[7:].startswith("-"):
        return text
    raise ValueError(
        f"unsupported kernel location {location!r}: expected 'local' or"
        " 'remote:<ssh alias>' (one word of letters, digits, dots,"
        " dashes, underscores)"
    )


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

_RETRY_DISCLOSURE = (
    "note: the kernel died mid-cell before this result; the cell was "
    "re-run on a fresh kernel and may have partially executed once — "
    "side effects (files written, external calls) may have happened twice"
)


@dataclass(slots=True)
class SessionState:
    """The manager-side record of one session's kernel."""

    session_id: str
    language: str = "python"
    location: str = "local"
    """``local`` or ``remote:<alias>`` — where this kernel's namespace
    lives. Part of the state key, so the same session may hold one
    kernel per (language, location) combination."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_guard: asyncio.Lock = field(default_factory=asyncio.Lock)
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
    current_execution_token: str = ""
    last_error: str = ""
    target_workdir: str = ""
    """The immutable remote target work directory bound to this state.

    A remote session may be resumed after the kernel dies, but it must keep
    the work directory captured by the job snapshot.  An empty value means
    this state predates the snapshot seam and has not been bound yet.
    """
    resetting: bool = False
    """Set while a cancel is killing this kernel (tier 3): the execute
    in flight must report its death instead of retrying the cell, or a
    cancelled cell would silently re-run on the replacement kernel."""

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "language": self.language,
            "location": self.location,
            "target_workdir": self.target_workdir or None,
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
        remote_runtime: Any = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.limits = limits or OutputLimits()
        self.cell_timeout_s = float(cell_timeout_s)
        self.python = python
        self.remote_runtime = remote_runtime
        """The remote half, or ``None`` (the default — remote kernel
        locations then raise at the door). Anything with an async
        ``start(session_id, *, host, workspace, limits, on_figure,
        language) -> kernel``; the shipped one is
        :class:`omicsclaw.remote.kernel.RemoteKernelRuntime`. Typed as
        ``Any`` because this package sits below the remote layer and
        imports nothing from it."""
        self.handles = HandleRegistry()
        self.states: dict[tuple[str, str, str], SessionState] = {}
        self._on_figure = on_figure
        self._reaper = reaper or KernelReaper(self)
        self._reaper_task: asyncio.Task[None] | None = None

    # ---- session bookkeeping ----

    def state_for(
        self,
        session_id: str,
        *,
        language: str = "python",
        location: str = "local",
    ) -> SessionState:
        """The state of one session's *language* kernel *at a location*.
        A session may hold a python kernel and an R kernel at once, and
        a local kernel beside a remote one — separate namespaces,
        separate lifecycles, the same session id."""
        key = (session_id, language, normalize_location(location))
        state = self.states.get(key)
        if state is None:
            state = SessionState(
                session_id=session_id, language=language, location=key[2]
            )
            self.states[key] = state
        return state

    def snapshot(self) -> list[dict[str, Any]]:
        return [state.snapshot() for state in self.states.values()]

    def protect(
        self,
        session_id: str,
        reason: str,
        *,
        language: str = "python",
        location: str = "local",
    ) -> None:
        """Add *session_id*'s kernel to the reaper's protection set (e.g.
        a pending approval). Idempotent per reason."""
        self.state_for(session_id, language=language, location=location).protected.add(
            reason
        )

    def unprotect(
        self,
        session_id: str,
        reason: str,
        *,
        language: str = "python",
        location: str = "local",
    ) -> None:
        state = self.states.get((session_id, language, normalize_location(location)))
        if state is not None:
            state.protected.discard(reason)

    # ---- execution ----

    async def execute(
        self,
        session_id: str,
        code: str,
        *,
        origin: str = "agent",
        language: str = "python",
        location: str = "local",
        callbacks: KernelCallbacks | None = None,
        timeout_s: float | None = None,
        adata_in: str | None = None,
        adata_binding: str = "adata",
        job_id: str = "",
        target_workdir: str = "",
        retry_on_death: bool = True,
        cancelled: Callable[[], bool] | None = None,
        execution_token: str = "",
    ) -> CellResult:
        """Run *code* on *session_id*'s kernel, starting one if needed.

        *origin* records who asked (``"agent"`` everywhere this round; the
        ``origin == "user"`` screenshot semantics are a documented TODO).
        *language* picks the session's kernel for that language —
        ``"python"`` (the ``python`` tool and the default) or ``"r"``
        (the installed IRkernel spec; needs ``r_available()``). The two
        kernels of one session are separate namespaces. *location* picks
        where that namespace lives — ``"local"`` (the whole of P4) or
        ``"remote:<ssh alias>"`` (C3; needs a *remote_runtime*, python
        only, and no ``adata_in`` — the h5ad bridge reads a file on this
        machine into the kernel, so the file must sit where the kernel
        runs). *adata_in* is an optional host h5ad path run through
        :meth:`HandleRegistry.sync_in` under the session lock, with the
        resident object additionally bound to *adata_binding* in the cell's
        namespace — the job face's input contract, python only.
        """
        if language not in ("python", "r"):
            raise ValueError(f"unsupported kernel language: {language!r}")
        location = normalize_location(location)
        if location != "local":
            if self.remote_runtime is None:
                raise ValueError(
                    "remote kernel locations are not enabled on this"
                    " deployment (no remote kernel runtime bound)"
                )
            if language != "python":
                raise ValueError(
                    "remote kernels are python-only this round; R over"
                    " IRkernel rides the language seam when it lands"
                )
        if adata_in and language != "python":
            raise ValueError("the adata h5ad bridge is python-only")
        if adata_in and location != "local":
            raise ValueError(
                "the adata h5ad bridge needs the kernel beside the file;"
                " run the cell locally or move the data to the host"
            )
        self._ensure_reaper()
        state = self.state_for(session_id, language=language, location=location)
        async with state.lock:
            if cancelled is not None and cancelled():
                return CellResult(status="interrupted")
            if location != "local":
                candidate_workdir = str(target_workdir or "").strip()
                if state.target_workdir:
                    if not candidate_workdir:
                        raise ValueError(
                            "remote kernel target snapshot is missing its workdir"
                        )
                    if candidate_workdir != state.target_workdir:
                        raise ValueError(
                            "remote kernel target workdir differs from the "
                            "session snapshot"
                        )
                elif state.kernel is not None and candidate_workdir:
                    raise ValueError(
                        "remote kernel target workdir cannot change after "
                        "the session kernel has started"
                    )
                elif candidate_workdir:
                    state.target_workdir = candidate_workdir
            async with state.cancel_guard:
                state.executing = True
                state.protected.add("executing")
                state.current_job = str(job_id)
                state.current_execution_token = execution_token
            try:
                if state.restart_needed and state.kernel is not None:
                    await self._recycle_locked(
                        state, reason=state.restart_needed, with_summary=True
                    )
                attempt = 0
                mid_cell_retry = False
                while True:
                    attempt += 1
                    notice = ""
                    if state.kernel is not None and not await asyncio.to_thread(
                        state.kernel.is_alive
                    ):
                        # The kernel died between cells (killed externally,
                        # or it crashed after the last cell): announce, then
                        # cold-start beneath this same call.
                        await self._park_with_obituary(
                            state,
                            summary=None,
                            reason="the previous kernel died before this cell",
                        )
                    if state.kernel is None:
                        await self._start_locked(
                            state, target_workdir=state.target_workdir
                        )
                    if state.obituary_pending:
                        notice = state.obituary
                        state.obituary_pending = False
                    prefix = ""
                    sync_info: dict[str, Any] | None = None
                    if adata_in:
                        sync_info, prefix = await asyncio.to_thread(
                            self._sync_input, state, adata_in, adata_binding
                        )

                    def execute_cell(source=prefix + code):
                        # Cancellation during startup or a lock wait must not
                        # become a new command after the caller has stopped.
                        if cancelled is not None and cancelled():
                            return CellResult(status="interrupted")
                        return state.kernel.execute(
                            source,
                            timeout_s=float(timeout_s or self.cell_timeout_s),
                            callbacks=callbacks,
                        )

                    if cancelled is not None and cancelled():
                        return CellResult(status="interrupted")
                    result = await asyncio.to_thread(execute_cell)
                    if (
                        result.status == "dead"
                        and retry_on_death
                        and attempt == 1
                        and not state.resetting
                    ):
                        # A kernel that dies *mid-cell* is not the cell's
                        # fault: park it, and retry the cell once on a
                        # cold-started kernel that carries the notice — the
                        # caller asked for work, and the work has not had a
                        # chance to run. A second death is reported as-is,
                        # and a death caused by a cancel's tier-3 kill is
                        # never retried (the caller asked for the opposite).
                        # The retry is disclosed below: a cell that already
                        # had side effects may have run them twice.
                        await self._park_with_obituary(
                            state,
                            summary=None,
                            reason="the previous kernel died mid-cell",
                        )
                        mid_cell_retry = True
                        continue
                    notices = []
                    if notice:
                        notices.append(notice)
                    if mid_cell_retry:
                        notices.append(_RETRY_DISCLOSURE)
                    result.notice = "\n".join(notices)
                    if notices:
                        # The plan puts the handover notice at the *top* of
                        # the next cell's stderr — the first thing a model
                        # or a person reads — with the retry disclosure
                        # second, never in front of it.
                        result.stderr = "\n".join(notices) + "\n" + result.stderr
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
                        await self._park_with_obituary(
                            state,
                            summary=None,
                            reason="the previous kernel was reset after a cell timeout",
                        )
                    return result
            finally:
                async with state.cancel_guard:
                    state.executing = False
                    state.protected.discard("executing")
                    state.current_job = ""
                    state.current_execution_token = ""
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
            _log.info(
                "session %s marked for kernel restart: %s",
                state.session_id,
                state.restart_needed,
            )

    async def _start_locked(
        self, state: SessionState, *, target_workdir: str = ""
    ) -> None:
        def _on_figure(path: Path, mime: str) -> tuple[str, str]:
            artifact_id = ""
            if self._on_figure is not None:
                try:
                    artifact_id = str(
                        self._on_figure(path, mime, state.session_id, state.current_job)
                        or ""
                    )
                except Exception:  # noqa: BLE001 - registration is never a cell fault
                    _log.exception("figure registration failed: %s", path)
            return artifact_id, "figure"

        if state.location != "local":
            # The remote half owns everything a far-away process needs —
            # the environment, the launch, the tunnel, the attach — and
            # raises on a host that refuses; a refusal here is a failed
            # cell, never a quiet fallback to a local kernel.
            start_kwargs = {
                "host": state.location[len("remote:") :],
                "workspace": self.workspace,
                "limits": self.limits,
                "on_figure": _on_figure,
                "language": state.language,
            }
            if target_workdir:
                start_kwargs["target_workdir"] = target_workdir
            kernel = await self.remote_runtime.start(state.session_id, **start_kwargs)
        else:
            kernel = SessionKernel(
                state.session_id,
                workspace=self.workspace,
                python=self.python,
                limits=self.limits,
                on_figure=_on_figure,
                language=state.language,
            )
            await asyncio.to_thread(kernel.start)
        state.kernel = kernel
        state.born_at = time.time()
        state.cells = 0
        state.peak_rss_kb = None
        state.restart_needed = ""

    # ---- the cancel ladder ----

    async def cancel(
        self,
        session_id: str,
        *,
        job_id: str | None = None,
        language: str | None = None,
        location: str | None = None,
        execution_token: str | None = None,
    ) -> str:
        """Finish interruption and cleanup before propagating caller cancellation."""
        return await _finish_before_cancelling(
            self._cancel(
                session_id,
                job_id=job_id,
                language=language,
                location=location,
                execution_token=execution_token,
            )
        )

    async def _cancel(
        self,
        session_id: str,
        *,
        job_id: str | None = None,
        language: str | None = None,
        location: str | None = None,
        execution_token: str | None = None,
    ) -> str:
        """Interrupt what *session_id* is running. Answers the tier reached.

        ``"interrupted"`` — tier 1/2: the executing task observed the
        interrupt within the grace. ``"reset"`` — tier 3: the kernel was
        killed and its handles dropped; the next execute cold-starts with
        a handover notice. ``"dead"``/``"idle"`` — there was nothing to
        interrupt, or nothing answered even the tier-4 ``pass`` probe.
        """
        # A session may hold one kernel per language; cancel reaches
        # whichever exists (state_for is deliberately not used — it would
        # conjure an empty python state for an R-only session).
        live = [
            s
            for s in self.states.values()
            if s.session_id == session_id
            and s.kernel is not None
            and (job_id is None or s.current_job == job_id)
            and (language is None or s.language == language)
            and (location is None or s.location == location)
            and (
                execution_token is None or s.current_execution_token == execution_token
            )
        ]
        if not live:
            return "idle"
        live.sort(key=lambda s: not s.executing)  # an executing kernel first
        state = live[0]
        kernel = state.kernel
        scoped = job_id is not None or execution_token is not None

        def owns_execution():
            return (
                state.kernel is kernel
                and (job_id is None or state.current_job == job_id)
                and (
                    execution_token is None
                    or state.current_execution_token == execution_token
                )
            )

        if not scoped:
            await asyncio.to_thread(kernel.interrupt)
        else:
            # Keep ownership fixed until the signal is sent. The old execution
            # cannot release its session lock and admit another cell meanwhile.
            async with state.cancel_guard:
                if not owns_execution():
                    return "idle"
                await asyncio.to_thread(kernel.interrupt)
        deadline = time.monotonic() + CANCEL_GRACE_S
        while state.executing and time.monotonic() < deadline:
            if scoped and not owns_execution():
                return "interrupted"
            await asyncio.sleep(0.1)
        if scoped and not owns_execution():
            return "interrupted"
        if not state.executing:
            if not await asyncio.to_thread(kernel.is_alive):
                return "dead"
            probe = await self._probe(state)
            return "interrupted" if probe else "dead"
        # Tier 3: the interrupt did not land inside the grace. Kill the
        # process *before* taking the session lock: kill is safe from any
        # thread, and it is what makes the executing poll loop detect the
        # death and let go of the lock promptly — a SIGINT-immune C
        # extension would otherwise hold the lock (and the UI's cancel
        # button) hostage until the cell timeout. The ``resetting`` flag
        # keeps the in-flight execute from retrying the cell on the
        # replacement kernel.
        async with state.cancel_guard:
            if scoped and not owns_execution():
                return "interrupted"
            state.resetting = True
            await asyncio.to_thread(kernel.kill)
        async with state.lock:
            # The lock is free only once the in-flight execute has
            # returned, which is exactly when the flag has done its job.
            state.resetting = False
            if state.kernel is kernel:
                await self._park_with_obituary(
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
            outcome = await asyncio.to_thread(kernel.execute, "pass\n", timeout_s=15.0)
        except Exception:  # noqa: BLE001 - a probe that raises is a dead kernel
            return False
        return outcome.status == "ok"

    # ---- recycling and death ----

    async def _park_with_obituary(
        self, state: SessionState, *, summary: str | None, reason: str
    ) -> None:
        """Kill (if anything is left) and leave an obituary for the next cell.

        The kill runs on a worker thread: a local kill is an instant
        ``os.killpg``, but a remote kill is an ssh round trip, and this
        method is reached from the event loop (execute, the cancel
        ladder, the reaper) — the one place a blocking subprocess must
        never run."""
        kernel = state.kernel
        if kernel is not None:
            try:
                await asyncio.to_thread(kernel.kill)
            finally:
                state.kernel = None
        dropped = self.handles.drop_session(state.session_id)
        handle_note = ", ".join(dropped) if dropped else "no registered adata handles"
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
        """Planned recycle: summary cell first, then the kill. The summary
        cell is python — an R kernel gets the honest no-summary notice
        instead of a cell it cannot run."""
        summary: str | None = None
        kernel = state.kernel
        if (
            with_summary
            and state.language == "python"
            and kernel is not None
            and await asyncio.to_thread(kernel.is_alive)
        ):
            try:
                outcome = await asyncio.to_thread(
                    kernel.execute, SUMMARY_CELL_CODE, timeout_s=60.0
                )
                summary = outcome.stdout.strip()[:4000] or None
            except Exception:  # noqa: BLE001 - the summary is best-effort
                _log.exception("namespace summary failed for %s", state.session_id)
        await self._park_with_obituary(state, summary=summary, reason=reason)
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


async def _finish_before_cancelling(operation):
    """Keep a cancellation operation alive until its thread work and cleanup finish."""
    task = asyncio.create_task(operation)
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


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
        + text[-(limit - head) :]
    )
