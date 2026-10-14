"""Shared job submission and run projections, independent of HTTP mounts."""

from __future__ import annotations

from omicsclaw.tools.builtin.remote import valid_host

from .turn_submission import DesktopIngressError


def event_cursor(value):
    try:
        cursor = int(value)
        if cursor < 0:
            raise ValueError
    except (ValueError, TypeError):
        raise DesktopIngressError("invalid_last_event_id", status_code=422) from None
    return cursor


def job_event_page(jobs, job_id, after_seq):
    from .jobs_manager import TERMINAL_JOB_STATUSES

    cursor = event_cursor(after_seq)
    record = jobs.get_job(job_id)
    if record is None:
        raise DesktopIngressError("job_not_found", status_code=404)
    events = jobs.store.events_after(job_id, cursor)
    next_cursor = max([cursor, *(event[0] for event in events)])
    earliest = jobs.store.earliest_seq(job_id)
    latest = jobs.store.latest_seq(job_id)
    return {
        "events": events,
        "nextCursor": next_cursor,
        "gap": earliest > cursor + 1,
        "complete": record.status in TERMINAL_JOB_STATUSES and next_cursor >= latest,
        "job": record.as_payload(),
    }


async def create_job_from_document(jobs, document):
    kind = document.get("kind", "skill_run")
    if not isinstance(kind, str):
        raise DesktopIngressError("invalid_kind", status_code=422)
    skill = document.get("skill", "")
    if skill is not None and not isinstance(skill, str):
        raise DesktopIngressError("invalid_skill", status_code=422)
    session_id = document.get("session_id", "")
    if session_id is not None and not isinstance(session_id, str):
        raise DesktopIngressError("invalid_session_id", status_code=422)
    workspace = document.get("workspace", "")
    if workspace is not None and not isinstance(workspace, str):
        raise DesktopIngressError("invalid_workspace", status_code=422)
    runtime = document.get("runtime", "local")
    if runtime is not None and not isinstance(runtime, str):
        raise DesktopIngressError("invalid_runtime", status_code=422)
    runtime = runtime or "local"
    if runtime != "local" and not (
        runtime.startswith("remote:") and valid_host(runtime[7:])
    ):
        raise DesktopIngressError("invalid_runtime", status_code=422)
    inputs = document.get("inputs")
    request_id = document.get("source_request_id", "")
    lifetime = document.get("lifetime", "")
    if not isinstance(request_id, str):
        raise DesktopIngressError("invalid_job_request_id", status_code=422)
    if not isinstance(lifetime, str) or lifetime not in {"", "in_process", "external"}:
        raise DesktopIngressError("invalid_job_lifetime", status_code=422)
    # P4's code_run carries its payload at the top level
    # (``{kind, code, language?, inputs?{adata_path?}}``, the shape
    # the plan spells) rather than inside ``inputs``: the code is the
    # job, not an argument to a skill. Folded into ``inputs`` here so
    # the runner reads one bag; a skill_run body is untouched.
    if kind == "code_run":
        code = document.get("code")
        if not isinstance(code, str) or not code.strip():
            raise DesktopIngressError("code_required", status_code=422)
        bag = dict(inputs) if isinstance(inputs, dict) else {}
        bag["code"] = code
        # The kernel language rides at the top level with the
        # code ("python" | "r"); the runner validates the value.
        language = document.get("language", "python")
        if language is not None and not isinstance(language, str):
            raise DesktopIngressError("invalid_language", status_code=422)
        bag["language"] = language
        # C3's location rides with it ("local" |
        # "remote:<ssh alias>"), the same layer the language
        # chose: one kernel per (session, language, location).
        location = document.get("location", "local")
        if location is not None and not isinstance(location, str):
            raise DesktopIngressError("invalid_location", status_code=422)
        bag["location"] = location
        inputs = bag
    # P5's env_install folds the same way: ``{kind, env?,
    # packages?}`` at the top level, folded into ``inputs`` for
    # the runner. Shape-checked here; the name/package rules the
    # runner owns, like code_run's language and location.
    if kind == "env_install":
        env = document.get("env", "main")
        if env is not None and not isinstance(env, str):
            raise DesktopIngressError("invalid_env", status_code=422)
        extra = document.get("packages", [])
        if extra is None:
            extra = []
        if not isinstance(extra, list) or not all(
            isinstance(item, str) for item in extra
        ):
            raise DesktopIngressError("invalid_packages", status_code=422)
        bag = dict(inputs) if isinstance(inputs, dict) else {}
        bag["env"] = env
        bag["packages"] = extra
        inputs = bag
    record = await jobs.create_job(
        kind=kind,
        skill=skill or "",
        inputs=inputs,
        session_id=session_id or "",
        workspace=workspace or "",
        runtime=runtime,
        source_request_id=request_id,
        lifetime=lifetime,
    )
    return record


def run_payload(jobs, run):
    from dataclasses import asdict

    from omicsclaw.runs.projections import belongs_to_run

    objects = jobs.run_store.list()
    owned = [obj for obj in objects if belongs_to_run(obj, run.run_id)]
    artifacts = [obj for obj in owned if obj.__class__.__name__ == "Artifact"]
    artifact_ids = {obj.artifact_id for obj in artifacts}
    return {
        "run": asdict(run),
        "jobs": [asdict(obj) for obj in owned if obj.__class__.__name__ == "Job"],
        "kernels": [asdict(obj) for obj in owned if obj.__class__.__name__ == "Kernel"],
        "artifacts": [asdict(obj) for obj in artifacts],
        "activities": [
            asdict(obj) for obj in owned if obj.__class__.__name__ == "Activity"
        ],
        "verifications": [
            asdict(obj)
            for obj in objects
            if obj.__class__.__name__ == "Verification"
            and (obj.run_id == run.run_id or obj.artifact_id in artifact_ids)
        ],
    }
