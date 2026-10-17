"""Reconcile remote jobs after restart or on request.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .jobs import has_identity_proof, verified_state
from .ssh import RemoteHostUnreachable

Report = dict[str, int]
OnRemoteStatus = Callable[[str, str, str], None]
"""``(local_job_id, host_alias, state)`` — the bridge's hook into the
local jobs table, called only for rows that carry a ``local_job_id``."""


async def refresh_in_flight(
    plane: Any,
    *,
    on_remote_status: OnRemoteStatus | None = None,
    now: Callable[[], float] = time.time,
) -> Report:
    """Re-ask every in-flight remote job what it is doing.

    :param plane: Anything exposing ``job_store()`` and
        ``executor_for(alias)``.
    :param on_remote_status: Optional callback for jobs the P1 plane
        started, so their local row moves with the remote answer.
    :returns: Counts by outcome: ``checked``, ``still_in_flight``,
        ``terminal``, ``unknown``.

    Never raises for a host problem; a store problem propagates, because
    a database that cannot be read is a backend that cannot start, and
    hiding it here would only move the failure somewhere less legible.
    """
    store = plane.job_store()
    report: Report = {"checked": 0, "still_in_flight": 0, "terminal": 0, "unknown": 0}
    for row in store.in_flight(include_unknown=True):
        report["checked"] += 1
        try:
            if not has_identity_proof(row.handle()):
                store.update_status(row.id, "unknown", last_seen=now())
                report["unknown"] += 1
                if on_remote_status is not None and row.local_job_id:
                    on_remote_status(row.local_job_id, row.host_alias, "unknown")
                continue
            executor = plane.executor_for(row.host_alias)
            outcome = await executor.status(row.handle())
        except (RemoteHostUnreachable, OSError):
            store.update_status(row.id, "unknown", last_seen=now())
            report["unknown"] += 1
            if on_remote_status is not None and row.local_job_id:
                on_remote_status(row.local_job_id, row.host_alias, "unknown")
            continue
        state = verified_state(row.handle(), outcome)
        store.update_status(row.id, state, last_seen=now())
        if state in ("pending", "running"):
            report["still_in_flight"] += 1
        elif state == "unknown":
            report["unknown"] += 1
        else:
            report["terminal"] += 1
        if on_remote_status is not None and row.local_job_id:
            on_remote_status(row.local_job_id, row.host_alias, state)
    return report


__all__ = ["OnRemoteStatus", "refresh_in_flight"]
