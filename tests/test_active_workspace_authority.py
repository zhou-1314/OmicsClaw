"""Contract tests for the Backend-lifetime Active Workspace authority."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from omicsclaw.control import RunObservationPage  # noqa: E402
from omicsclaw.control.run_runtime import RunRuntime  # noqa: E402
from omicsclaw.remote.app_integration import register_remote_routers  # noqa: E402
from omicsclaw.remote.auth import (  # noqa: E402
    capture_remote_bearer_authority,
    release_remote_bearer_authority,
)
from omicsclaw.remote.routers import artifacts as artifacts_module  # noqa: E402
from omicsclaw.remote.routers import datasets as datasets_module  # noqa: E402
from omicsclaw.remote.routers import env as env_module  # noqa: E402
from omicsclaw.remote.routers import jobs as jobs_module  # noqa: E402
from omicsclaw.remote.runtime_binding import (  # noqa: E402
    bind_remote_run_runtime,
    get_remote_run_runtime,
    get_remote_workspace,
    unbind_remote_run_runtime,
)
from omicsclaw.remote.schemas import Job  # noqa: E402
from omicsclaw.surfaces.desktop import server  # noqa: E402


AUTH_HEADERS = {"Authorization": "Bearer correct-token"}


@pytest.fixture(autouse=True)
def _isolated_remote_runtime_binding():
    authority = capture_remote_bearer_authority(
        server.app,
        {"OMICSCLAW_REMOTE_AUTH_TOKEN": "correct-token"},
    )
    unbind_remote_run_runtime()
    try:
        yield
    finally:
        unbind_remote_run_runtime()
        release_remote_bearer_authority(server.app, authority)


def _bind_workspace(workspace: Path) -> RunRuntime:
    # Binding validates the concrete Runtime type but does not need a started
    # scheduler for these pure Workspace-observation contracts.
    runtime = object.__new__(RunRuntime)
    bind_remote_run_runtime(runtime, workspace=workspace)
    return runtime


def _forbid(calls: list[str], label: str):
    def forbidden(*_args, **_kwargs):
        calls.append(label)
        raise AssertionError(f"request reached {label}")

    return forbidden


@pytest.mark.asyncio
async def test_lifespan_acquires_control_ownership_before_legacy_closure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events: list[str] = []

    class FailingControlRuntime:
        async def start(self) -> None:
            events.append("control_start")
            raise RuntimeError("control_lifetime_lock_unavailable")

        async def close(self) -> None:
            events.append("control_close")

    class ControlFactory:
        @staticmethod
        def for_local_surface(**_kwargs):
            return FailingControlRuntime()

    def forbidden_legacy_closure(_workspace: Path):
        events.append("legacy_closure")
        raise AssertionError("legacy state changed before Control ownership")

    import omicsclaw.runtime.agent.state as core

    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(workspace))
    monkeypatch.setattr(core, "init", lambda **_kwargs: None)
    monkeypatch.setattr(core, "LLM_PROVIDER_NAME", "test", raising=False)
    monkeypatch.setattr(core, "OMICSCLAW_MODEL", "test", raising=False)
    monkeypatch.setattr(server, "ControlRuntime", ControlFactory)
    monkeypatch.setattr(
        server,
        "terminalize_legacy_active_jobs_at_startup",
        forbidden_legacy_closure,
    )
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setattr(server, "_desktop_run_runtime", None, raising=False)
    monkeypatch.setattr(server, "_memory_client", None, raising=False)

    with pytest.raises(RuntimeError, match="control_lifetime_lock_unavailable"):
        async with server.lifespan(server.app):
            raise AssertionError("lifespan unexpectedly admitted traffic")

    assert events == ["control_start", "control_close"]


@pytest.mark.asyncio
async def test_real_lifespan_binds_every_workspace_backed_remote_adapter_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    later = tmp_path / "later"
    active.mkdir()
    later.mkdir()

    import omicsclaw.memory as memory_module
    import omicsclaw.runtime.agent.state as core

    def memory_disabled():
        raise RuntimeError("memory disabled for composition-root test")

    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(active))
    monkeypatch.setenv("OMICSCLAW_OUTPUT_ROOT", str(active / "output"))
    monkeypatch.setenv(
        "OMICSCLAW_CONTROL_STATE_ROOT",
        str(tmp_path / "control-state"),
    )
    monkeypatch.setattr(core, "init", lambda **_kwargs: None)
    monkeypatch.setattr(core, "LLM_PROVIDER_NAME", "test", raising=False)
    monkeypatch.setattr(core, "OMICSCLAW_MODEL", "test", raising=False)
    monkeypatch.setattr(memory_module, "get_engine_db", memory_disabled)
    monkeypatch.setattr(server, "_NOTEBOOK_AVAILABLE", False)
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setattr(server, "_desktop_run_runtime", None, raising=False)
    monkeypatch.setattr(server, "_memory_client", None, raising=False)

    async with server.lifespan(server.app):
        active_resolved = active.resolve()
        assert server._desktop_control_runtime is not None
        assert server._desktop_run_runtime is not None
        assert Path(server._desktop_control_runtime.workspace_id) == active_resolved
        assert get_remote_run_runtime() is server._desktop_run_runtime
        assert get_remote_workspace() == active_resolved

        monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(later.resolve()))

        datasets = await datasets_module.list_datasets()
        doctor = await env_module.env_doctor()
        jobs = await jobs_module.list_jobs(status=None, limit=10, cursor=None)
        artifacts = await artifacts_module.list_artifacts(
            job_id="legacy-missing",
            cursor=None,
            limit=10,
        )

        assert datasets.workspace == str(active_resolved)
        assert doctor.workspace_dir == str(active_resolved)
        assert jobs.jobs == []
        assert artifacts.artifacts == []
        assert get_remote_workspace() == active_resolved

    assert get_remote_run_runtime() is None
    assert get_remote_workspace() is None


def _symlink_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
@pytest.mark.parametrize(
    ("method", "request_kwargs"),
    (
        ("GET", {}),
        ("PUT", {"json": {"workspace": "relative/not-a-workspace"}}),
    ),
)
def test_workspace_auth_is_checked_before_workspace_validation_or_state_access(
    monkeypatch,
    tmp_path: Path,
    authorization: str | None,
    method: str,
    request_kwargs: dict[str, object],
) -> None:
    active = tmp_path / "active"
    active.mkdir()
    _bind_workspace(active)
    calls: list[str] = []
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(active))
    monkeypatch.setattr(
        server,
        "require_remote_workspace",
        _forbid(calls, "workspace"),
        raising=False,
    )
    monkeypatch.setattr(server, "_get_core", _forbid(calls, "core"))
    monkeypatch.setattr(
        server,
        "_get_omicsclaw_env_path",
        _forbid(calls, "env lookup"),
    )
    monkeypatch.setattr(server, "_update_env_file", _forbid(calls, "env write"))
    monkeypatch.setattr(server, "Path", _forbid(calls, "path validation"))
    before = dict(os.environ)
    headers = {} if authorization is None else {"Authorization": authorization}

    response = TestClient(server.app).request(
        method,
        "/workspace",
        headers=headers,
        **request_kwargs,
    )

    assert response.status_code == 401
    assert calls == []
    assert dict(os.environ) == before
    assert get_remote_workspace() == active.resolve()


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_put_workspace_malformed_json_is_rejected_before_body_or_state_access(
    monkeypatch,
    authorization: str | None,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setattr(
        server,
        "require_remote_workspace",
        _forbid(calls, "workspace"),
    )
    monkeypatch.setattr(server, "_get_core", _forbid(calls, "core"))
    monkeypatch.setattr(server, "Path", _forbid(calls, "path validation"))
    monkeypatch.setattr(
        server,
        "_get_omicsclaw_env_path",
        _forbid(calls, "env lookup"),
    )
    monkeypatch.setattr(server, "_update_env_file", _forbid(calls, "env write"))
    before = dict(os.environ)
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization

    response = TestClient(server.app).put(
        "/workspace",
        headers=headers,
        content=b"{malformed",
    )

    assert response.status_code == 401
    assert calls == []
    assert dict(os.environ) == before


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
@pytest.mark.parametrize(
    ("route", "params"),
    (
        ("/files/browse", {"path": "/must-not-be-inspected"}),
        ("/files/tree", {"path": "/must-not-be-inspected", "depth": 2}),
        ("/files/serve", {"path": "/must-not-be-inspected/secret.txt"}),
    ),
)
def test_file_routes_reject_unauthorized_requests_before_filesystem_access(
    monkeypatch,
    authorization: str | None,
    route: str,
    params: dict[str, object],
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setattr(server, "Path", _forbid(calls, "path access"))
    monkeypatch.setattr(server, "_get_core", _forbid(calls, "core"))
    monkeypatch.setattr(
        server,
        "require_remote_workspace",
        _forbid(calls, "workspace"),
    )
    monkeypatch.setattr(
        server,
        "_trusted_file_roots",
        _forbid(calls, "trusted roots"),
    )
    monkeypatch.setattr(
        server,
        "_resolve_trusted_file_path",
        _forbid(calls, "file resolution"),
    )
    monkeypatch.setattr(
        server,
        "_scan_file_tree",
        _forbid(calls, "tree scan"),
    )
    headers = {} if authorization is None else {"Authorization": authorization}

    response = TestClient(server.app).get(
        route,
        params=params,
        headers=headers,
    )

    assert response.status_code == 401
    assert calls == []


def test_put_active_workspace_is_idempotent_and_has_zero_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    active.mkdir()
    runtime = _bind_workspace(active)
    active_resolved = active.resolve()
    output_dir = active_resolved / "output"
    trusted_dirs = [active_resolved]
    fake_core = SimpleNamespace(
        TRUSTED_DATA_DIRS=trusted_dirs,
        OUTPUT_DIR=output_dir,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("SENTINEL=unchanged\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_DATA_DIRS", str(active_resolved))
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(active_resolved))
    monkeypatch.setenv("OMICSCLAW_OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr(server, "_update_env_file", _forbid(calls, "env write"))
    monkeypatch.setattr(server, "_get_omicsclaw_env_path", lambda: env_file)
    before_env = dict(os.environ)
    before_env_file = env_file.read_bytes()

    response = TestClient(server.app).put(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace": str(active_resolved)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert {
        key: payload[key]
        for key in (
            "ok",
            "workspace",
            "active_workspace",
            "requested_workspace",
            "restart_required",
        )
    } == {
        "ok": True,
        "workspace": str(active_resolved),
        "active_workspace": str(active_resolved),
        "requested_workspace": str(active_resolved),
        "restart_required": False,
    }
    assert calls == []
    assert dict(os.environ) == before_env
    assert env_file.read_bytes() == before_env_file
    assert fake_core.TRUSTED_DATA_DIRS is trusted_dirs
    assert fake_core.TRUSTED_DATA_DIRS == [active_resolved]
    assert fake_core.OUTPUT_DIR is output_dir
    assert get_remote_run_runtime() is runtime
    assert get_remote_workspace() == active_resolved
    assert not output_dir.exists()


def test_symlink_workspace_is_canonicalized_and_cannot_retarget_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    alias = tmp_path / "workspace-link"
    original.mkdir()
    replacement.mkdir()
    _symlink_directory(alias, original)
    runtime = _bind_workspace(alias)
    original_resolved = original.resolve()
    replacement_resolved = replacement.resolve()
    fake_core = SimpleNamespace(
        TRUSTED_DATA_DIRS=[original_resolved],
        OUTPUT_DIR=original_resolved / "output",
    )
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(alias))

    same_root = TestClient(server.app).put(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace": str(alias)},
    )

    assert same_root.status_code == 200
    assert same_root.json()["active_workspace"] == str(original_resolved)
    assert same_root.json()["requested_workspace"] == str(original_resolved)
    assert get_remote_workspace() == original_resolved

    alias.unlink()
    _symlink_directory(alias, replacement)

    observed = TestClient(server.app).get("/workspace", headers=AUTH_HEADERS)
    retarget = TestClient(server.app).put(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace": str(alias)},
    )

    assert observed.status_code == 200
    assert observed.json()["active_workspace"] == str(original_resolved)
    assert retarget.status_code == 409
    assert retarget.json()["requested_workspace"] == str(replacement_resolved)
    assert get_remote_run_runtime() is runtime
    assert get_remote_workspace() == original_resolved


def test_put_different_workspace_requires_restart_without_mutating_any_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    requested = tmp_path / "requested"
    active.mkdir()
    requested.mkdir()
    runtime = _bind_workspace(active)
    active_resolved = active.resolve()
    requested_resolved = requested.resolve()
    output_dir = active_resolved / "output"
    trusted_dirs = [active_resolved]
    fake_core = SimpleNamespace(
        TRUSTED_DATA_DIRS=trusted_dirs,
        OUTPUT_DIR=output_dir,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("SENTINEL=unchanged\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_DATA_DIRS", str(active_resolved))
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(active_resolved))
    monkeypatch.setenv("OMICSCLAW_OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr(server, "_update_env_file", _forbid(calls, "env write"))
    monkeypatch.setattr(server, "_get_omicsclaw_env_path", lambda: env_file)
    before_env = dict(os.environ)
    before_env_file = env_file.read_bytes()

    response = TestClient(server.app).put(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace": str(requested_resolved)},
    )

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": "workspace_change_requires_backend_restart",
        "workspace": str(active_resolved),
        "active_workspace": str(active_resolved),
        "requested_workspace": str(requested_resolved),
        "restart_required": True,
    }
    assert calls == []
    assert dict(os.environ) == before_env
    assert env_file.read_bytes() == before_env_file
    assert fake_core.TRUSTED_DATA_DIRS is trusted_dirs
    assert fake_core.TRUSTED_DATA_DIRS == [active_resolved]
    assert fake_core.OUTPUT_DIR is output_dir
    assert get_remote_run_runtime() is runtime
    assert get_remote_workspace() == active_resolved
    assert not output_dir.exists()
    assert not (requested_resolved / "output").exists()


def test_put_nonexistent_different_workspace_still_requires_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    requested = tmp_path / "not-created"
    active.mkdir()
    runtime = _bind_workspace(active)
    active_resolved = active.resolve()
    requested_resolved = requested.resolve()
    calls: list[str] = []
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setattr(server, "_get_core", _forbid(calls, "core"))
    monkeypatch.setattr(server, "_update_env_file", _forbid(calls, "env write"))
    before = dict(os.environ)

    response = TestClient(server.app).put(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace": str(requested)},
    )

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": "workspace_change_requires_backend_restart",
        "workspace": str(active_resolved),
        "active_workspace": str(active_resolved),
        "requested_workspace": str(requested_resolved),
        "restart_required": True,
    }
    assert calls == []
    assert dict(os.environ) == before
    assert not requested.exists()
    assert get_remote_run_runtime() is runtime
    assert get_remote_workspace() == active_resolved


def test_put_same_workspace_remains_idempotent_after_external_deletion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    active.mkdir()
    runtime = _bind_workspace(active)
    active_resolved = active.resolve()
    fake_core = SimpleNamespace(
        TRUSTED_DATA_DIRS=[active_resolved],
        OUTPUT_DIR=active_resolved / "output",
    )
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    before = dict(os.environ)
    active.rmdir()

    response = TestClient(server.app).put(
        "/workspace",
        headers=AUTH_HEADERS,
        json={"workspace": str(active_resolved)},
    )

    assert response.status_code == 200
    assert response.json()["active_workspace"] == str(active_resolved)
    assert response.json()["requested_workspace"] == str(active_resolved)
    assert response.json()["restart_required"] is False
    assert dict(os.environ) == before
    assert not active.exists()
    assert get_remote_run_runtime() is runtime
    assert get_remote_workspace() == active_resolved


def test_active_workspace_remains_frozen_when_environment_changes_later(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    later = tmp_path / "later"
    active.mkdir()
    later.mkdir()
    runtime = _bind_workspace(active)
    active_resolved = active.resolve()
    monkeypatch.setattr(
        server,
        "_core",
        SimpleNamespace(
            TRUSTED_DATA_DIRS=[active_resolved],
            OUTPUT_DIR=active_resolved / "output",
        ),
        raising=False,
    )
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(later.resolve()))

    response = TestClient(server.app).get("/workspace", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert {
        key: payload[key]
        for key in ("workspace", "active_workspace", "restart_required")
    } == {
        "workspace": str(active_resolved),
        "active_workspace": str(active_resolved),
        "restart_required": False,
    }
    assert get_remote_run_runtime() is runtime
    assert get_remote_workspace() == active_resolved
    assert os.environ["OMICSCLAW_WORKSPACE"] == str(later.resolve())


def test_all_remote_adapters_share_frozen_workspace_after_environment_drift(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active = (tmp_path / "active").resolve()
    later = (tmp_path / "later").resolve()
    active.mkdir()
    later.mkdir()
    _bind_workspace(active)

    class EmptyRuntime:
        lifecycle_ready = True

        def list_receipts(self, *, status=None, cursor=None, limit=50):
            del status, cursor, limit
            return RunObservationPage((), None)

    monkeypatch.setattr(
        jobs_module,
        "require_remote_run_runtime",
        lambda: EmptyRuntime(),
    )
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    # Simulate later mutable process drift.  No Adapter may follow it.
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(later))

    def seed_terminal_artifact(workspace: Path, filename: str) -> None:
        jobs_module._write_job(
            workspace,
            Job(
                job_id="legacy-terminal",
                skill="historical-skill",
                status="succeeded",
                workspace=str(workspace),
                inputs={},
                params={},
                created_at="2026-01-01T00:00:00+00:00",
            ),
        )
        artifact = jobs_module._artifact_root(workspace, "legacy-terminal") / filename
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(workspace.name, encoding="utf-8")

    seed_terminal_artifact(active, "active.txt")
    seed_terminal_artifact(later, "later.txt")
    later_before = {
        path.relative_to(later).as_posix(): path.read_bytes()
        for path in later.rglob("*")
        if path.is_file()
    }

    app = FastAPI()
    capture_remote_bearer_authority(
        app,
        {"OMICSCLAW_REMOTE_AUTH_TOKEN": "correct-token"},
    )
    register_remote_routers(app)
    client = TestClient(app)

    env_response = client.get("/env/doctor", headers=AUTH_HEADERS)
    datasets_response = client.get("/datasets", headers=AUTH_HEADERS)
    jobs_response = client.get("/jobs", headers=AUTH_HEADERS)
    artifacts_response = client.get(
        "/artifacts",
        headers=AUTH_HEADERS,
        params={"job_id": "legacy-terminal"},
    )
    chat_response = client.post(
        "/jobs",
        headers=AUTH_HEADERS,
        json={
            "skill": "chat",
            "session_id": "legacy-ui-only",
            "params": {"job_kind": "chat_stream"},
        },
    )
    session_response = client.post(
        "/sessions/legacy-ui-only/resume",
        headers=AUTH_HEADERS,
    )

    assert env_response.status_code == 200
    assert env_response.json()["workspace_dir"] == str(active)
    assert datasets_response.status_code == 200
    assert datasets_response.json()["workspace"] == str(active)
    assert jobs_response.status_code == 200
    assert {job["workspace"] for job in jobs_response.json()["jobs"]} == {str(active)}
    assert artifacts_response.status_code == 200
    assert {
        artifact["relative_path"] for artifact in artifacts_response.json()["artifacts"]
    } == {"active.txt"}
    assert chat_response.status_code == 409
    assert chat_response.json() == {"detail": "legacy_chat_job_submission_retired"}
    assert jobs_module._job_path(active, "legacy-terminal").is_file()
    assert not jobs_module._job_path(active, "legacy-ui-only").exists()
    assert session_response.json() == {
        "session_id": "legacy-ui-only",
        "resumed": False,
        "reason": "legacy_session_resume_retired",
        "active_job_ids": [],
    }
    later_after = {
        path.relative_to(later).as_posix(): path.read_bytes()
        for path in later.rglob("*")
        if path.is_file()
    }
    assert later_after == later_before


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_workspace_endpoint_is_unavailable_without_lifespan_binding(
    monkeypatch,
    tmp_path: Path,
    method: str,
) -> None:
    requested = tmp_path / "requested"
    requested.mkdir()
    calls: list[str] = []
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setattr(server, "_update_env_file", _forbid(calls, "env write"))
    kwargs = {"json": {"workspace": str(requested)}} if method == "PUT" else {}

    response = TestClient(server.app).request(
        method,
        "/workspace",
        headers=AUTH_HEADERS,
        **kwargs,
    )

    assert response.status_code == 503
    assert calls == []
    assert not (requested / "output").exists()


@pytest.mark.parametrize(
    "path",
    (
        "/env/doctor",
        "/datasets",
        "/jobs",
        "/artifacts?job_id=legacy-missing",
    ),
)
def test_remote_adapters_never_fall_back_to_environment_without_binding(
    monkeypatch,
    tmp_path: Path,
    path: str,
) -> None:
    environment_workspace = tmp_path / "environment-only"
    environment_workspace.mkdir()
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(environment_workspace))
    app = FastAPI()
    capture_remote_bearer_authority(
        app,
        {"OMICSCLAW_REMOTE_AUTH_TOKEN": "correct-token"},
    )
    register_remote_routers(app)

    response = TestClient(app).get(path, headers=AUTH_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == "remote_workspace_unavailable"
    assert not (environment_workspace / ".omicsclaw").exists()
