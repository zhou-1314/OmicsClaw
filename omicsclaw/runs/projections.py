"""Read existing session, plan and kernel facts into the Run model."""

import hashlib
from datetime import UTC, datetime

from .models import Kernel, Run


def plan_snapshot(plans, session_id):
    if plans is None or not session_id:
        return "", [], ""
    items = plans.for_session(session_id).read()
    steps = [
        {"step_id": item.id, "title": item.content, "status": str(item.status)}
        for item in items
    ]
    active = next((item.id for item in items if str(item.status) == "in_progress"), "")
    return ("session-plan:" + session_id if steps else ""), steps, active


def ensure_session_run(store, session_id, *, plans=None):
    if not session_id:
        return None
    identity = "legacy:" + session_id
    run = store.load(identity)
    if run is None:
        store.save(
            Run(
                run_id=identity,
                session_id=session_id,
                status="unknown",
                origin="legacy",
            ),
            if_absent=True,
        )
        run = store.load(identity)
    plan_id, steps, active = plan_snapshot(plans, session_id)
    run.plan_id = plan_id
    run.plan_steps = steps
    run.step_id = active
    store.save(run)
    return run


def observe_kernel(state):
    kernel = state.kernel
    if kernel is None or not kernel.is_alive():
        return None
    pid = getattr(kernel, "pid", None)
    started = float(getattr(kernel, "started_at", 0) or state.born_at)
    if pid is None or not started:
        return None
    location = state.location
    host = (
        location.removeprefix("remote:") if location.startswith("remote:") else "local"
    )
    identity = f"{host}:{state.session_id}:{state.language}:{pid}:{started}"
    reachable = True
    if location.startswith("remote:"):
        client = getattr(kernel, "_kc", None)
        heartbeat = getattr(client, "hb_channel", None)
        reachable = bool(heartbeat is not None and heartbeat.is_beating())
    return Kernel(
        kernel_id="kernel:" + hashlib.sha256(identity.encode()).hexdigest()[:32],
        session_id=state.session_id,
        language=state.language,
        host_id=host,
        status=("busy" if state.executing else "ready") if reachable else "unknown",
        pid=int(pid),
        started_at=started,
        environment=str(getattr(kernel, "python", "")),
        last_heartbeat=datetime.now(UTC).isoformat() if reachable else None,
    )


def bind_kernel(store, kernel, run_id, job_id=""):
    previous = store.load(kernel.kernel_id)
    if previous and kernel.last_heartbeat is None:
        kernel.last_heartbeat = previous.last_heartbeat
    kernel.run_id = previous.run_id if previous else run_id
    kernel.run_ids = sorted(set((previous.run_ids if previous else []) + [run_id]))
    store.save(kernel)
    for job in store.list("job"):
        if job.run_id == run_id and (not job_id or job.execution_ref == job_id):
            job.kernel_id = kernel.kernel_id
            store.save(job)
    return kernel


def belongs_to_run(obj, run_id):
    return getattr(obj, "run_id", None) == run_id or run_id in getattr(
        obj, "run_ids", ()
    )
