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

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

__all__ = ["HARD_TOP_S", "IDLE_SOFT_TOP_S", "KernelReaper", "POLL_INTERVAL_S"]

_log = logging.getLogger(__name__)

IDLE_SOFT_TOP_S = 1800.0
"""30 min idle -> soft recycle (CS kernel_idle_timeout)."""

HARD_TOP_S = 21600.0
"""6 h wall clock since kernel birth -> recycle even when busy."""

POLL_INTERVAL_S = 30.0
"""Sweep period; injectable small for tests."""


class KernelReaper:
    """Sweeps the manager's sessions on a timer. Never raises out."""

    def __init__(
        self,
        manager: Any = None,
        *,
        idle_soft_s: float = IDLE_SOFT_TOP_S,
        hard_s: float = HARD_TOP_S,
        poll_s: float = POLL_INTERVAL_S,
        restart_after_cells: int = 200,
        restart_rss_kb: int = 6 * 1024 * 1024,
    ) -> None:
        self._manager = manager
        self.idle_soft_s = float(idle_soft_s)
        self.hard_s = float(hard_s)
        self.poll_s = max(0.05, float(poll_s))
        self.restart_after_cells = int(restart_after_cells)
        self.restart_rss_kb = int(restart_rss_kb)

    async def run(self) -> None:
        """The task body: sleep, sweep, repeat, forever."""
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001 - the reaper outlives any one sweep
                _log.exception("kernel reaper sweep failed")

    async def sweep_once(self) -> list[str]:
        """One pass. Answers the session ids it recycled.

        The protection set wins over the soft top but not the hard top:
        an executing session past six hours is exactly the runaway the
        hard top exists for. A *protected* session (a pending approval)
        past the hard top is still left alone this round — killing a
        session mid-approval orphans the approval — and logged, so the
        operator sees it instead of the silence OmicOS shipped.
        """
        manager = self._manager
        if manager is None:
            return []
        now = time.time()
        recycled: list[str] = []
        for state in list(manager.states.values()):
            kernel = state.kernel
            if kernel is None:
                continue
            age = now - (state.born_at or now)
            idle = now - state.last_activity
            protected = bool(state.protected)
            if state.executing and age <= self.hard_s:
                continue
            if age > self.hard_s:
                if protected:
                    _log.warning(
                        "session %s is %d s old (over the %d s hard top) but "
                        "protected (%s); not reaping this sweep",
                        state.session_id,
                        int(age),
                        int(self.hard_s),
                        ",".join(sorted(state.protected)),
                    )
                    continue
                reason = f"hard top {int(age)}s over {int(self.hard_s)}s"
            elif not state.executing and not protected and idle > self.idle_soft_s:
                reason = f"idle {int(idle)}s over soft top {int(self.idle_soft_s)}s"
            else:
                continue
            if state.lock.locked():
                # An execute took the lock between the checks above and
                # now; the next sweep reconsiders with fresh timestamps.
                continue
            async with state.lock:
                if state.kernel is kernel:
                    await manager._recycle_locked(
                        state, reason=reason, with_summary=True
                    )
                    recycled.append(state.session_id)
        return recycled
