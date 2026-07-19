from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from omicsclaw.autoagent import _resolve_optimization_output_root, run_optimization
from omicsclaw.autoagent.api import optimizable_skills
from omicsclaw.control import ControlStateRepository


@pytest.fixture(autouse=True)
def _bound_autoagent_control_repository(tmp_path: Path):
    """Explicit unit-test seam for production's Desktop lifespan binding."""

    import omicsclaw.autoagent.api as api_module

    repository = ControlStateRepository(tmp_path / "autoagent-control")
    api_module._sessions.clear()
    api_module.bind_autoagent_repository(repository)
    try:
        yield repository
    finally:
        api_module._sessions.clear()
        api_module.unbind_autoagent_repository(repository)
        repository.close()


def test_save_config_request_accepts_only_a_safe_session_id() -> None:
    from pydantic import ValidationError

    import omicsclaw.autoagent.api as api_module

    session_id = "a" * 32
    request = api_module.SaveConfigRequest.model_validate(
        {"session_id": session_id}
    )
    assert request.model_dump() == {"session_id": session_id}

    for payload in (
        {"session_id": session_id, "skill": "renderer-choice"},
        {"session_id": "legacy-session_01"},
        {"session_id": "../session"},
        {"session_id": "session\nheader"},
    ):
        with pytest.raises(ValidationError):
            api_module.SaveConfigRequest.model_validate(payload)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_module.router)
    response = TestClient(app).post(
        "/autoagent/save-config",
        json={"session_id": session_id, "best_score": 999},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_optimize_start_request_rejects_unsafe_explicit_session_ids() -> None:
    from pydantic import ValidationError

    import omicsclaw.autoagent.api as api_module

    for session_id in (
        "../session",
        "session/name",
        "session\\name",
        "session\nheader",
        "legacy-session_01",
        "A" * 32,
        "a" * 31,
        "a" * 33,
    ):
        with pytest.raises(ValidationError):
            api_module.OptimizeRequest.model_validate(
                {
                    "session_id": session_id,
                    "skill": "sc-batch-integration",
                    "method": "harmony",
                }
            )

    canonical = api_module.OptimizeRequest.model_validate(
        {
            "session_id": "a" * 32,
            "skill": "sc-batch-integration",
            "method": "harmony",
        }
    )
    assert canonical.session_id == "a" * 32


def test_optimize_start_http_rejects_auto_promote_before_acceptance() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import omicsclaw.autoagent.api as api_module

    app = FastAPI()
    app.include_router(api_module.router)
    response = TestClient(app).post(
        "/autoagent/start",
        json={
            "session_id": "b" * 32,
            "skill": "sc-batch-integration",
            "method": "harmony",
            "auto_promote": True,
        },
    )

    assert response.status_code == 422
    assert "b" * 32 not in api_module._sessions
    with pytest.raises(KeyError):
        api_module._require_autoagent_repository().get_autoagent_session(
            "b" * 32
        )


def test_optimize_start_http_reports_persistent_capacity_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import omicsclaw.autoagent.api as api_module

    monkeypatch.setattr(api_module, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api_module,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api_module,
        "new_governed_worker_reference",
        lambda: (
            "linux-user-systemd-bwrap-v1",
            f"omicsclaw-run-{'c' * 24}.scope",
        ),
    )

    repository = api_module._require_autoagent_repository()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE autoagent_capacity "
            "SET session_count = 100000 WHERE singleton_id = 1"
        )
        connection.commit()

    app = FastAPI()
    app.include_router(api_module.router)
    response = TestClient(app).post(
        "/autoagent/start",
        json={
            "session_id": "c" * 32,
            "skill": "sc-batch-integration",
            "method": "harmony",
        },
    )

    assert response.status_code == 507
    assert response.json() == {
        "detail": "AutoAgent durable audit capacity is exhausted"
    }
    assert "c" * 32 not in api_module._sessions


def test_optimize_start_http_rejects_oversized_payload_before_authority() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import omicsclaw.autoagent.api as api_module

    app = FastAPI()
    app.include_router(api_module.router)
    response = TestClient(app).post(
        "/autoagent/start",
        json={
            "session_id": "d" * 32,
            "skill": "sc-batch-integration",
            "method": "harmony",
            "fixed_params": {"nested": {"value": "x" * 65_537}},
        },
    )

    assert response.status_code == 422
    assert "d" * 32 not in api_module._sessions
    with pytest.raises(KeyError):
        api_module._require_autoagent_repository().get_autoagent_session("d" * 32)


@pytest.mark.asyncio
async def test_optimize_start_generates_a_32_lower_hex_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api_module
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    class ImmediateFailureOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self):
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("error", error_code="harness_failed")

    monkeypatch.setattr(api_module, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api_module,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api_module,
        "new_governed_worker_reference",
        lambda: (
            "linux-user-systemd-bwrap-v1",
            f"omicsclaw-run-{'d' * 24}.scope",
        ),
    )
    monkeypatch.setattr(api_module, "GovernedAutoAgentWorker", ImmediateFailureOwner)
    api_module._sessions.clear()
    api_module._start_timestamps.clear()

    response = await api_module.optimize_start(
        api_module.OptimizeRequest(
            skill="sc-batch-integration",
            method="harmony",
        )
    )
    try:
        assert len(api_module._sessions) == 1
        session_id, runtime = next(iter(api_module._sessions.items()))
        assert re.fullmatch(r"[0-9a-f]{32}", session_id)
        assert runtime.session_id == session_id
        assert runtime.worker is not None
        await asyncio.wait_for(runtime.worker, timeout=10)
    finally:
        await response.body_iterator.aclose()
        api_module._sessions.clear()


@pytest.mark.asyncio
async def test_save_evolved_config_uses_start_and_result_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api_module
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    result = {
        "success": True,
        "mode": "harness_evolution",
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "improve batch mixing",
        "best_score": 0.875,
        "improvement_pct": 12.5,
        "patches_accepted": 1,
        "accepted_files": ["skills/singlecell/example.py"],
        "accepted_patch_commits": ["a" * 40],
        "output_dir": str(tmp_path / "output" / "run"),
        "promotion": {"status": "skipped"},
    }

    class SuccessfulOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self):
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("done", result=result)

    monkeypatch.setattr(api_module, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api_module,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api_module,
        "new_governed_worker_reference",
        lambda: (
            "linux-user-systemd-bwrap-v1",
            f"omicsclaw-run-{'1' * 24}.scope",
        ),
    )
    monkeypatch.setattr(api_module, "GovernedAutoAgentWorker", SuccessfulOwner)
    api_module._sessions.clear()
    api_module._start_timestamps.clear()

    response = await api_module.optimize_start(
        api_module.OptimizeRequest(
            session_id="1" * 32,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
            output_dir=result["output_dir"],
            evolution_goal="improve batch mixing",
        )
    )
    runtime = api_module._sessions["1" * 32]
    assert runtime.worker is not None
    await runtime.worker
    await asyncio.sleep(0)
    assert runtime.start_authority is not None
    with pytest.raises(FrozenInstanceError):
        runtime.start_authority.skill = "renderer-choice"

    try:
        saved = await api_module.save_evolved_config(
            api_module.SaveConfigRequest(session_id="1" * 32)
        )
        saved_again = await api_module.save_evolved_config(
            api_module.SaveConfigRequest(session_id="1" * 32)
        )
    finally:
        await response.body_iterator.aclose()
        api_module._sessions.clear()

    filename = api_module._evolved_config_filename(
        "sc-batch-integration",
        "harmony",
    )
    config_path = tmp_path / ".omicsclaw" / "evolved" / filename
    assert saved == {
        "success": True,
        "path": str(config_path),
        "relative_path": f".omicsclaw/evolved/{filename}",
    }
    assert saved_again == saved
    assert list(config_path.parent.iterdir()) == [config_path]
    assert config_path.stat().st_nlink == 1
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    evolved_at = payload.pop("evolved_at")
    assert evolved_at.endswith("+00:00")
    assert payload == {
        "skill": "sc-batch-integration",
        "method": "harmony",
        "best_score": 0.875,
        "improvement_pct": 12.5,
        "patches_accepted": 1,
        "accepted_files": ["skills/singlecell/example.py"],
        "accepted_patch_commits": ["a" * 40],
        "evolution_goal": "improve batch mixing",
    }


def test_evolved_config_filename_cannot_alias_distinct_valid_pairs() -> None:
    import omicsclaw.autoagent.api as api_module

    first = api_module._evolved_config_filename("a_b", "c")
    second = api_module._evolved_config_filename("a", "b_c")

    assert first != second
    assert re.fullmatch(r"evolved-[0-9a-f]{64}\.json", first)
    assert re.fullmatch(r"evolved-[0-9a-f]{64}\.json", second)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [None, False, 0, 1, "true", {}])
async def test_optimize_runtime_rejects_non_exact_success_before_done_or_promote(
    success: object,
) -> None:
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    result = {
        "skill": "sc-batch-integration",
        "method": "harmony",
        **({} if success is None else {"success": success}),
    }
    runtime = api_module.OptimizeSessionRuntime(
        session_id="2" * 32,
        loop=asyncio.get_running_loop(),
    )

    runtime.mark_done(result)

    status, retained_result, error = runtime.snapshot()
    assert status == "error"
    assert retained_result is None
    assert error
    api_module._sessions.clear()
    api_module._sessions[runtime.session_id] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.promote_session(runtime.session_id)
        assert rejected.value.status_code == 409
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
async def test_optimize_runtime_never_exposes_producer_done_before_result_validation(
) -> None:
    import omicsclaw.autoagent.api as api_module

    runtime = api_module.OptimizeSessionRuntime(
        session_id="3" * 32,
        loop=asyncio.get_running_loop(),
    )
    runtime.emit("done", {"success": True, "unvalidated": True})
    await asyncio.sleep(0)
    assert runtime.snapshot()[0] == "running"
    assert runtime.queue.empty()

    runtime.mark_done({"success": None, "error": "invalid final summary"})
    await asyncio.sleep(0)
    status, result, error = runtime.snapshot()
    assert status == "error"
    assert result is None
    assert error
    events = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait())
    assert [event["type"] for event in events] == ["error", "_finished"]


@pytest.mark.asyncio
async def test_save_evolved_config_rejects_unknown_and_nonterminal_sessions(
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    api_module._sessions.clear()
    with pytest.raises(HTTPException) as unknown:
        await api_module.save_evolved_config(
            api_module.SaveConfigRequest(session_id="4" * 32)
        )
    assert unknown.value.status_code == 404

    runtime = api_module.OptimizeSessionRuntime(
        session_id="5" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(tmp_path / "output"),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
        ),
    )
    api_module._sessions["5" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as nonterminal:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="5" * 32)
            )
        assert nonterminal.value.status_code == 409
        assert "done" in str(nonterminal.value.detail)
        assert not (tmp_path / ".omicsclaw").exists()
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_authority", "mismatched_result"])
async def test_save_evolved_config_rejects_restarted_or_mismatched_state(
    tmp_path: Path,
    failure: str,
) -> None:
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    authority = api_module.OptimizeStartAuthority(
        cwd=str(tmp_path),
        output_dir=str(tmp_path / "output"),
        skill="sc-batch-integration",
        method="harmony",
        evolution_goal="goal",
    )
    runtime = api_module.OptimizeSessionRuntime(
        session_id="6" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=None if failure == "missing_authority" else authority,
    )
    runtime.status = "done"
    runtime.result = {
        "success": True,
        "skill": "wrong-skill" if failure == "mismatched_result" else authority.skill,
        "method": authority.method,
        "evolution_goal": authority.evolution_goal,
        "output_dir": authority.output_dir,
        "best_score": 1.0,
        "improvement_pct": 1.0,
        "patches_accepted": 0,
        "accepted_files": [],
        "accepted_patch_commits": [],
    }
    api_module._sessions.clear()
    api_module._sessions["6" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="6" * 32)
            )
        assert rejected.value.status_code == 409
        assert not (tmp_path / ".omicsclaw").exists()
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
async def test_save_evolved_config_rejects_internally_inconsistent_result(
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    runtime = api_module.OptimizeSessionRuntime(
        session_id="7" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(tmp_path / "output"),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="goal",
        ),
    )
    runtime.status = "done"
    runtime.result = {
        "success": True,
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "goal",
        "output_dir": str(tmp_path / "output"),
        "best_score": 1.0,
        "improvement_pct": 2.0,
        "patches_accepted": 2,
        "accepted_files": ["skills/example.py"],
        "accepted_patch_commits": ["a" * 40],
    }
    api_module._sessions.clear()
    api_module._sessions["7" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="7" * 32)
            )
        assert rejected.value.status_code == 409
        assert not (tmp_path / ".omicsclaw").exists()
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_kind", ["symlink", "hardlink"])
async def test_save_evolved_config_does_not_follow_aliased_destinations(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    config_dir = tmp_path / ".omicsclaw" / "evolved"
    config_dir.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text("do-not-replace\n", encoding="utf-8")
    destination = config_dir / api_module._evolved_config_filename(
        "sc-batch-integration",
        "harmony",
    )
    if destination_kind == "symlink":
        destination.symlink_to(external)
    else:
        destination.hardlink_to(external)

    runtime = api_module.OptimizeSessionRuntime(
        session_id="8" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(tmp_path / "output"),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="goal",
        ),
    )
    runtime.status = "done"
    runtime.result = {
        "success": True,
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "goal",
        "output_dir": str(tmp_path / "output"),
        "best_score": 1.0,
        "improvement_pct": 0.0,
        "patches_accepted": 0,
        "accepted_files": [],
        "accepted_patch_commits": [],
    }
    api_module._sessions.clear()
    api_module._sessions["8" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="8" * 32)
            )
        assert rejected.value.status_code == 409
        assert external.read_text(encoding="utf-8") == "do-not-replace\n"
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
async def test_save_evolved_config_fails_closed_when_evolved_directory_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checked directory must not be replaceable before the owned write."""
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    config_dir = tmp_path / ".omicsclaw" / "evolved"
    config_dir.mkdir(parents=True)
    displaced_dir = tmp_path / "displaced-evolved"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    filename = api_module._evolved_config_filename(
        "sc-batch-integration",
        "harmony",
    )
    real_open = os.open
    raced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        path_text = os.fspath(path)
        if (
            not raced
            and Path(path_text).name.startswith(f".{filename}.")
            and Path(path_text).name.endswith(".tmp")
        ):
            raced = True
            config_dir.rename(displaced_dir)
            config_dir.symlink_to(external_dir, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    runtime = api_module.OptimizeSessionRuntime(
        session_id="9" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(tmp_path / "output"),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="goal",
        ),
    )
    runtime.status = "done"
    runtime.result = {
        "success": True,
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "goal",
        "output_dir": str(tmp_path / "output"),
        "best_score": 1.0,
        "improvement_pct": 0.0,
        "patches_accepted": 0,
        "accepted_files": [],
        "accepted_patch_commits": [],
    }
    api_module._sessions.clear()
    api_module._sessions["9" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="9" * 32)
            )
        assert rejected.value.status_code == 409
        assert raced is True
        assert not (external_dir / filename).exists()
        assert not (displaced_dir / filename).exists()
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
async def test_save_evolved_config_restores_previous_value_after_directory_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed authority recheck must not destroy the last good projection."""
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    config_dir = tmp_path / ".omicsclaw" / "evolved"
    config_dir.mkdir(parents=True)
    displaced_dir = tmp_path / "displaced-evolved"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    filename = api_module._evolved_config_filename(
        "sc-batch-integration",
        "harmony",
    )
    previous = config_dir / filename
    previous.write_text("previous-good\n", encoding="utf-8")
    real_open = os.open
    raced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        path_text = os.fspath(path)
        if (
            not raced
            and Path(path_text).name.startswith(f".{filename}.")
            and Path(path_text).name.endswith(".tmp")
        ):
            raced = True
            config_dir.rename(displaced_dir)
            config_dir.symlink_to(external_dir, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    runtime = api_module.OptimizeSessionRuntime(
        session_id="a" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(tmp_path / "output"),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="goal",
        ),
    )
    runtime.status = "done"
    runtime.result = {
        "success": True,
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "goal",
        "output_dir": str(tmp_path / "output"),
        "best_score": 1.0,
        "improvement_pct": 0.0,
        "patches_accepted": 0,
        "accepted_files": [],
        "accepted_patch_commits": [],
    }
    api_module._sessions.clear()
    api_module._sessions["a" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="a" * 32)
            )
        assert rejected.value.status_code == 409
        assert raced is True
        assert (displaced_dir / filename).read_text(
            encoding="utf-8"
        ) == "previous-good\n"
        assert not (external_dir / filename).exists()
    finally:
        api_module._sessions.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_authority", ["cwd_alias", "unsafe_skill", "unsafe_method"]
)
async def test_save_evolved_config_rejects_unsafe_bound_paths_and_identifiers(
    tmp_path: Path,
    unsafe_authority: str,
) -> None:
    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api_module

    real_cwd = tmp_path / "real"
    real_cwd.mkdir()
    bound_cwd = real_cwd
    skill = "sc-batch-integration"
    method = "harmony"
    if unsafe_authority == "cwd_alias":
        bound_cwd = tmp_path / "workspace-alias"
        bound_cwd.symlink_to(real_cwd, target_is_directory=True)
    elif unsafe_authority == "unsafe_skill":
        skill = "../outside"
    else:
        method = "method/../../outside"

    runtime = api_module.OptimizeSessionRuntime(
        session_id="b" * 32,
        loop=asyncio.get_running_loop(),
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(bound_cwd),
            output_dir=str(real_cwd / "output"),
            skill=skill,
            method=method,
            evolution_goal="goal",
        ),
    )
    runtime.status = "done"
    runtime.result = {
        "success": True,
        "skill": skill,
        "method": method,
        "evolution_goal": "goal",
        "output_dir": str(real_cwd / "output"),
        "best_score": 1.0,
        "improvement_pct": 0.0,
        "patches_accepted": 0,
        "accepted_files": [],
        "accepted_patch_commits": [],
    }
    api_module._sessions.clear()
    api_module._sessions["b" * 32] = runtime
    try:
        with pytest.raises(HTTPException) as rejected:
            await api_module.save_evolved_config(
                api_module.SaveConfigRequest(session_id="b" * 32)
            )
        assert rejected.value.status_code == 409
        assert not (real_cwd / ".omicsclaw").exists()
    finally:
        api_module._sessions.clear()


def test_branch_status_git_probe_scrubs_backend_control_credentials(monkeypatch):
    import subprocess

    import omicsclaw.autoagent as autoagent_module
    import omicsclaw.autoagent.api as api_module

    observed: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(stdout="feature/safe\n")

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD", "3")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(autoagent_module, "_check_protected_branch", lambda _root: None)

    result = asyncio.run(api_module.branch_status())

    assert result["branch"] == "feature/safe"
    child_env = observed["env"]
    assert isinstance(child_env, dict)
    assert "OMICSCLAW_REMOTE_AUTH_TOKEN" not in child_env
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in child_env
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in child_env


def test_optimize_skills_catalog_is_canonical_and_launchable():
    result = asyncio.run(optimizable_skills())

    names = {item["skill"] for item in result["skills"]}
    assert "sc-batch-integration" in names
    assert "sc-integrate" not in names
    assert "spatial-integrate" in names
    assert "spatial-integration" not in names
    assert "sc-clustering" in names
    assert "sc-cell-annotation" in names

    for item in result["skills"]:
        assert item["skill"] == item["canonical_skill"]
        assert item["methods"]
        assert all(method["params"] for method in item["methods"])

    batch_skill = next(
        item for item in result["skills"] if item["skill"] == "sc-batch-integration"
    )
    harmony = next(
        method for method in batch_skill["methods"] if method["name"] == "harmony"
    )
    assert harmony["params"] == ["harmony_theta", "integration_pcs"]
    assert harmony["fixed_params"] == [
        {
            "name": "batch_key",
            "type": "string",
            "required": False,
            "default": "batch",
            "cli_flag": "--batch-key",
        }
    ]

    spatial_deconv = next(
        item for item in result["skills"] if item["skill"] == "spatial-deconv"
    )
    flashdeconv = next(
        method
        for method in spatial_deconv["methods"]
        if method["name"] == "flashdeconv"
    )
    fixed_names = {param["name"] for param in flashdeconv["fixed_params"]}
    assert {"reference", "cell_type_key"} <= fixed_names


def test_resolve_optimization_output_root_defaults_under_workspace_output(tmp_path):
    output_root = _resolve_optimization_output_root(
        "sc-batch-integration",
        "harmony",
        cwd=str(tmp_path),
    )

    assert output_root.parent == tmp_path / "output"
    assert output_root.name.startswith("optimize_sc-batch-integration_harmony_")


def test_default_optimization_output_roots_are_collision_resistant(tmp_path):
    first = _resolve_optimization_output_root(
        "sc-batch-integration",
        "harmony",
        cwd=str(tmp_path),
    )
    second = _resolve_optimization_output_root(
        "sc-batch-integration",
        "harmony",
        cwd=str(tmp_path),
    )

    assert first != second


def test_resolve_optimization_output_root_resolves_relative_output_dir_against_workspace(
    tmp_path,
):
    output_root = _resolve_optimization_output_root(
        "sc-batch-integration",
        "harmony",
        cwd=str(tmp_path),
        output_dir="custom-output/run-001",
    )

    assert output_root == Path(tmp_path) / "custom-output" / "run-001"


def test_resolve_optimization_output_root_preserves_and_rejects_raw_alias(
    tmp_path,
):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        _resolve_optimization_output_root(
            "sc-batch-integration",
            "harmony",
            output_dir=str(alias / "session"),
        )


def test_run_optimization_rejects_relative_input_path_without_cwd():
    result = run_optimization(
        skill_name="sc-batch-integration",
        method="harmony",
        input_path="data/demo.h5ad",
        max_trials=1,
    )

    assert result["success"] is False
    assert "Relative input_path requires cwd" in result["error"]


def test_run_optimization_rejects_preexisting_explicit_output_root(
    monkeypatch,
    tmp_path,
):
    output_root = tmp_path / "existing-run"
    output_root.mkdir()
    stale = output_root / "trial_0000" / "result.json"
    stale.parent.mkdir()
    stale.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.OptimizationLoop.run",
        lambda *_args, **_kwargs: pytest.fail(
            "a nonempty session root must fail before optimization starts"
        ),
    )

    result = run_optimization(
        skill_name="sc-batch-integration",
        method="harmony",
        output_dir=str(output_root),
        demo=True,
        max_trials=1,
    )

    assert result["success"] is False
    assert "already exists" in result["error"].lower()
    assert stale.read_text(encoding="utf-8") == "{}"


def test_run_optimization_rejects_missing_required_fixed_params_before_trials():
    result = run_optimization(
        skill_name="sc-batch-integration",
        method="scanvi",
        demo=True,
        max_trials=1,
        fixed_params={"batch_key": "sample_id"},
    )

    assert result["success"] is False
    assert "Missing required fixed parameters" in result["error"]
    assert "labels_key" in result["error"]


def test_manual_promotion_uses_durable_workspace_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent as autoagent_package
    import omicsclaw.autoagent.api as api_module
    import omicsclaw.autoagent.harness_workspace as workspace_module
    from omicsclaw.autoagent.harness_workspace import (
        AcceptedPatchRecord,
        PromotionResult,
    )

    output_root = tmp_path / "run"
    sandbox_repo = output_root / "sandbox_repo"
    sandbox_repo.mkdir(parents=True)
    durable_record = AcceptedPatchRecord(
        iteration=1,
        commit_hash="a" * 40,
        parent_commit="b" * 40,
        artifact_path=str(output_root / "accepted.patch"),
        manifest_path=str(output_root / "accepted.json"),
    )
    observed: dict[str, object] = {}
    events: list[str] = []

    class FakeWorkspace:
        def __init__(self, _source_root: Path, candidate_root: Path) -> None:
            assert candidate_root == output_root
            self.repo_root = sandbox_repo
            self._created = False

        def open_existing(self) -> None:
            events.append("open")
            observed["opened"] = True

        def durable_accepted_head_record(self) -> AcceptedPatchRecord:
            events.append("durable")
            observed["loaded"] = True
            return durable_record

        def promote_accepted_state(
            self,
            *,
            accepted_patch: AcceptedPatchRecord,
        ) -> PromotionResult:
            events.append("promote")
            observed["record"] = accepted_patch
            return PromotionResult(
                status="applied",
                promoted_files=["skills/test/derived.py"],
            )

    result_payload = {
        "success": True,
        "output_dir": str(output_root),
        "promotion": {"status": "skipped"},
        # These compatibility fields are deliberately wrong. The endpoint must
        # load both file authority and the record from durable sandbox state.
        "accepted_files": ["skills/test/caller-controlled.py"],
        "accepted_patches": [{"commit_hash": "caller-controlled"}],
    }
    runtime = SimpleNamespace(
        session_id="c" * 32,
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(output_root),
            skill="skill",
            method="method",
            evolution_goal="",
        ),
        snapshot=lambda: ("done", result_payload, None),
        finished_at=0.0,
    )

    monkeypatch.setattr(workspace_module, "HarnessWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        autoagent_package,
        "_check_protected_branch",
        lambda _root: None,
    )
    monkeypatch.setitem(api_module._sessions, "c" * 32, runtime)

    response = asyncio.run(api_module.promote_session("c" * 32))

    assert events == ["open", "durable", "promote"]
    assert observed == {
        "opened": True,
        "loaded": True,
        "record": durable_record,
    }
    assert response["status"] == "applied"
    assert response["promoted_files"] == ["skills/test/derived.py"]


@pytest.mark.parametrize("failure_stage", ["open", "durable"])
def test_manual_promotion_stops_before_promote_when_durable_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    import omicsclaw.autoagent as autoagent_package
    import omicsclaw.autoagent.api as api_module
    import omicsclaw.autoagent.harness_workspace as workspace_module

    output_root = tmp_path / "run"
    sandbox_repo = output_root / "sandbox_repo"
    sandbox_repo.mkdir(parents=True)
    events: list[str] = []

    class FailingWorkspace:
        def __init__(self, _source_root: Path, candidate_root: Path) -> None:
            assert candidate_root == output_root
            self.repo_root = sandbox_repo

        def open_existing(self) -> None:
            events.append("open")
            if failure_stage == "open":
                raise ValueError("injected open authority failure")

        def durable_accepted_head_record(self) -> object:
            events.append("durable")
            if failure_stage == "durable":
                raise ValueError("injected durable authority failure")
            return object()

        def promote_accepted_state(self, *, accepted_patch: object) -> object:
            events.append("promote")
            raise AssertionError("promotion must not run after authority failure")

    session_id = "d" * 32 if failure_stage == "open" else "e" * 32
    runtime = SimpleNamespace(
        session_id=session_id,
        start_authority=api_module.OptimizeStartAuthority(
            cwd=str(tmp_path),
            output_dir=str(output_root),
            skill="skill",
            method="method",
            evolution_goal="",
        ),
        snapshot=lambda: (
            "done",
            {
                "success": True,
                "output_dir": str(output_root),
                "promotion": {"status": "skipped"},
            },
            None,
        ),
        finished_at=0.0,
    )
    monkeypatch.setattr(workspace_module, "HarnessWorkspace", FailingWorkspace)
    monkeypatch.setattr(
        autoagent_package,
        "_check_protected_branch",
        lambda _root: None,
    )
    monkeypatch.setitem(api_module._sessions, session_id, runtime)

    with pytest.raises(Exception) as caught:
        asyncio.run(api_module.promote_session(session_id))

    assert getattr(caught.value, "status_code", None) == 409
    assert events == (["open"] if failure_stage == "open" else ["open", "durable"])
