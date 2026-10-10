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

from __future__ import annotations

import time
from typing import Any, Callable

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
    for row in store.in_flight():
        report["checked"] += 1
        try:
            executor = plane.executor_for(row.host_alias)
            outcome = await executor.status(row.handle())
        except (RemoteHostUnreachable, OSError):
            store.update_status(row.id, "unknown", last_seen=now())
            report["unknown"] += 1
            continue
        store.update_status(row.id, outcome.state, last_seen=now())
        if outcome.state in ("pending", "running"):
            report["still_in_flight"] += 1
        elif outcome.state == "unknown":
            report["unknown"] += 1
        else:
            report["terminal"] += 1
        if on_remote_status is not None and row.local_job_id:
            on_remote_status(row.local_job_id, row.host_alias, outcome.state)
    return report


__all__ = ["OnRemoteStatus", "refresh_in_flight"]
