from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from omicsclaw.remote import auth as remote_auth  # noqa: E402
from omicsclaw.surfaces.desktop import server  # noqa: E402


_LOCAL_EVOLUTION_TOKEN = "a" * 64
_ROTATED_LOCAL_EVOLUTION_TOKEN = "b" * 64


class _Proposal:
    def __init__(self, status: str = "pending") -> None:
        self.status = status

    def to_dict(self):
        return {"proposal_id": "proposal-1", "status": self.status}


class _Governance:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def snapshot(self):
        self.calls.append(("snapshot",))
        return {"proposals": [], "health": []}

    def refresh(self):
        self.calls.append(("refresh",))
        return [_Proposal()]

    def experience_page(self, cursor="", limit=50, state=""):
        self.calls.append(("experience_page", cursor, limit, state))
        if cursor == "BAD":
            raise ValueError("invalid cursor: 'BAD'")
        return {
            "skills": [{"skill_revision": {"skill_id": "sc-de"}}],
            "next_cursor": None,
        }

    def experience_view(self, skill_id):
        self.calls.append(("experience_view", skill_id))
        if skill_id != "sc-de":
            return None
        return {"skill_revision": {"skill_id": skill_id}, "validation_state": "current"}

    def propose_deprecation(
        self,
        *,
        target_skill: str,
        replacement_skill: str,
        proposer: str,
        reason: str,
        support_event_ids: list[str],
    ):
        self.calls.append(
            (
                "propose_deprecation",
                target_skill,
                replacement_skill,
                proposer,
                reason,
                support_event_ids,
            )
        )
        return _Proposal()

    def propose_gotcha(
        self,
        *,
        target_skill: str,
        proposer: str,
        reason: str,
        support_event_ids: list[str],
        entry: dict,
    ):
        self.calls.append(
            (
                "propose_gotcha",
                target_skill,
                proposer,
                reason,
                support_event_ids,
                entry,
            )
        )
        return _Proposal()

    def approve(self, proposal_id: str, *, approver: str, reason: str = ""):
        self.calls.append(("approve", proposal_id, approver, reason))
        return SimpleNamespace(
            proposal_id=proposal_id,
            status="approved",
            approved_by=approver,
            before_hash="sha256:before",
            after_hash="sha256:after",
        )

    def reject(self, proposal_id: str, *, approver: str, reason: str):
        self.calls.append(("reject", proposal_id, approver, reason))
        return _Proposal("rejected")

    def reconcile(self, *, operator: str, reason: str):
        self.calls.append(("reconcile", operator, reason))
        return {
            "status": "rolled_back",
            "proposal_id": "proposal-1",
            "action": "restored_interrupted_approval",
        }


_EVOLUTION_ROUTE_CASES = (
    ("GET", "/skill-evolution", None),
    ("POST", "/skill-evolution/refresh", None),
    (
        "POST",
        "/skill-evolution/proposals/deprecation",
        {
            "target_skill": "legacy-skill",
            "replacement_skill": "replacement-skill",
            "proposer": "maintainer",
            "reason": "maintained replacement",
            "support_event_ids": ["defect-a", "defect-b", "defect-c"],
        },
    ),
    (
        "POST",
        "/skill-evolution/proposals/gotcha",
        {
            "target_skill": "evolution-test",
            "proposer": "maintainer",
            "reason": "repeated conditional failure",
            "support_event_ids": ["defect-a", "defect-b", "defect-c"],
            "entry": {
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the matrix remains sparse.",
                "guidance": "Densify only the bounded slice.",
                "anchors": ["evolution_test.py:1"],
            },
        },
    ),
    (
        "POST",
        "/skill-evolution/reconcile",
        {"operator": "operator", "reason": "backend restarted"},
    ),
    (
        "POST",
        "/skill-evolution/proposal-1/approve",
        {"approver": "human", "reason": "reviewed"},
    ),
    (
        "POST",
        "/skill-evolution/proposal-1/reject",
        {"approver": "human", "reason": "insufficient evidence"},
    ),
)


@pytest.fixture(autouse=True)
def _restore_skill_evolution_authority_state():
    """Keep the process-global FastAPI app state isolated between tests."""

    state = server.app.state
    attributes = (
        remote_auth.AUTHORITY_STATE_ATTR,
        server._SKILL_EVOLUTION_AUTH_STATE_ATTR,
    )
    sentinel = object()
    previous = {attribute: getattr(state, attribute, sentinel) for attribute in attributes}
    for attribute in attributes:
        if previous[attribute] is not sentinel:
            delattr(state, attribute)
    yield
    for attribute in attributes:
        if hasattr(state, attribute):
            delattr(state, attribute)
        if previous[attribute] is not sentinel:
            setattr(state, attribute, previous[attribute])


def _configure_skill_evolution_authority(
    monkeypatch,
    *,
    remote_token: str | None = None,
    dedicated_token: str | None = None,
):
    monkeypatch.delenv("OMICSCLAW_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", raising=False)
    if remote_token is not None:
        monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", remote_token)
    if dedicated_token is not None:
        monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", dedicated_token)
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    return server._capture_skill_evolution_bearer_authority(server.app, os.environ)


@pytest.mark.parametrize(("method", "path", "payload"), _EVOLUTION_ROUTE_CASES)
@pytest.mark.parametrize(
    ("dedicated_token", "headers", "expected_status", "expected_detail"),
    [
        pytest.param(
            None,
            {},
            503,
            "skill evolution bearer token is not configured",
            id="token-unset",
        ),
        pytest.param(
            "   ",
            {"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
            503,
            "skill evolution bearer token is not configured",
            id="token-blank",
        ),
        pytest.param(
            _LOCAL_EVOLUTION_TOKEN,
            {},
            401,
            "missing bearer token",
            id="header-missing",
        ),
        pytest.param(
            _LOCAL_EVOLUTION_TOKEN,
            {"Authorization": "Bearer wrong-token"},
            401,
            "invalid bearer token",
            id="token-wrong",
        ),
        pytest.param(
            _LOCAL_EVOLUTION_TOKEN,
            {"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
            200,
            None,
            id="token-correct",
        ),
    ],
)
def test_all_skill_evolution_routes_fail_closed_without_exact_bearer_token(
    monkeypatch,
    method,
    path,
    payload,
    dedicated_token,
    headers,
    expected_status,
    expected_detail,
):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=dedicated_token,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    request_kwargs = {"headers": headers}
    if payload is not None:
        request_kwargs["json"] = payload
    response = client.request(method, path, **request_kwargs)

    assert response.status_code == expected_status
    if expected_detail is not None:
        assert response.json()["detail"] == expected_detail
        assert governance.calls == []


def test_skill_evolution_strict_auth_does_not_change_remote_local_default(
    monkeypatch,
):
    monkeypatch.delenv("OMICSCLAW_REMOTE_AUTH_TOKEN", raising=False)
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    assert client.post("/connections/test").status_code == 200


def test_skill_evolution_routes_accept_the_dedicated_local_auth_token(
    monkeypatch,
):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.get(
        "/skill-evolution",
        headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json() == {"proposals": [], "health": []}


def _evolution_client(monkeypatch):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch, dedicated_token=_LOCAL_EVOLUTION_TOKEN
    )
    client = TestClient(server.app, raise_server_exceptions=False)
    return client, governance


_EVOLUTION_AUTH = {"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"}


def test_skill_evolution_skills_list_route_paginates(monkeypatch):
    client, governance = _evolution_client(monkeypatch)
    response = client.get(
        "/skill-evolution/skills?limit=10&state=current", headers=_EVOLUTION_AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["skills"][0]["skill_revision"]["skill_id"] == "sc-de"
    assert body["next_cursor"] is None
    assert ("experience_page", "", 10, "current") in governance.calls


def test_skill_evolution_skill_detail_route_and_404(monkeypatch):
    client, _ = _evolution_client(monkeypatch)
    ok = client.get("/skill-evolution/skills/sc-de", headers=_EVOLUTION_AUTH)
    assert ok.status_code == 200
    assert ok.json()["skill_revision"]["skill_id"] == "sc-de"

    missing = client.get("/skill-evolution/skills/nope", headers=_EVOLUTION_AUTH)
    assert missing.status_code == 404


def test_skill_evolution_skills_bad_cursor_is_422(monkeypatch):
    client, _ = _evolution_client(monkeypatch)
    response = client.get(
        "/skill-evolution/skills?cursor=BAD", headers=_EVOLUTION_AUTH
    )
    assert response.status_code == 422


def test_skill_evolution_routes_use_only_the_frozen_dedicated_authority(
    monkeypatch,
):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        remote_token="remote-token",
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    assert (
        client.get(
            "/skill-evolution",
            headers={"Authorization": "Bearer remote-token"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/skill-evolution",
            headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
        ).status_code
        == 200
    )

    monkeypatch.setenv(
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        _ROTATED_LOCAL_EVOLUTION_TOKEN,
    )
    assert (
        client.get(
            "/skill-evolution",
            headers={"Authorization": f"Bearer {_ROTATED_LOCAL_EVOLUTION_TOKEN}"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/skill-evolution",
            headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
        ).status_code
        == 200
    )


def test_skill_evolution_routes_do_not_lazily_read_environment_authority(
    monkeypatch,
):
    _configure_skill_evolution_authority(
        monkeypatch,
        remote_token="late-token",
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.get(
        "/skill-evolution",
        headers={"Authorization": "Bearer late-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "skill evolution bearer token is not configured"
    )


def test_skill_evolution_authority_freezes_and_scrubs_the_dedicated_token():
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": _LOCAL_EVOLUTION_TOKEN,
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    authority = server._SkillEvolutionBearerAuthority.capture(environ)

    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environ
    assert environ["OMICSCLAW_REMOTE_AUTH_TOKEN"] == "remote-token"
    assert authority.source == "dedicated_environment"
    assert authority.matches(_LOCAL_EVOLUTION_TOKEN) is True
    assert authority.matches("remote-token") is False
    assert _LOCAL_EVOLUTION_TOKEN not in repr(authority)
    environ["OMICSCLAW_SKILL_EVOLUTION_TOKEN"] = _ROTATED_LOCAL_EVOLUTION_TOKEN
    assert authority.matches(_ROTATED_LOCAL_EVOLUTION_TOKEN) is False


@pytest.mark.parametrize(
    "invalid_token",
    [
        "short",
        "A" * 64,
        "g" * 64,
        "a" * 63,
        "a" * 65,
        f" {_LOCAL_EVOLUTION_TOKEN} ",
    ],
)
def test_skill_evolution_authority_rejects_invalid_dedicated_environment_token(
    invalid_token,
):
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": invalid_token,
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    with pytest.raises(RuntimeError, match="invalid Skill Evolution credential"):
        server._SkillEvolutionBearerAuthority.capture(environ)

    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environ


def test_skill_evolution_blank_dedicated_environment_stays_unconfigured():
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": " \t ",
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    authority = server._SkillEvolutionBearerAuthority.capture(environ)

    assert authority.source == "unconfigured"
    assert authority.configured is False
    assert authority.matches("remote-token") is False
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environ


def test_skill_evolution_remote_environment_never_configures_authority():
    environ = {"OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token-a"}

    authority = server._SkillEvolutionBearerAuthority.capture(environ)
    environ["OMICSCLAW_REMOTE_AUTH_TOKEN"] = "remote-token-b"

    assert authority.source == "unconfigured"
    assert authority.configured is False
    assert authority.matches("remote-token-a") is False
    assert authority.matches("remote-token-b") is False


def test_skill_evolution_authority_prefers_one_shot_pipe_over_initial_env():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"0123456789abcdef" * 4 + b"\n")
    finally:
        os.close(write_fd)
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD": str(read_fd),
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": "stale-environment-token",
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    try:
        authority = server._SkillEvolutionBearerAuthority.capture(environ)

        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in environ
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environ
        assert authority.source == "dedicated_pipe"
        assert authority.matches("0123456789abcdef" * 4) is True
        assert authority.matches("stale-environment-token") is False
        assert authority.matches("remote-token") is False
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"a" * 63,
        b"a" * 65,
        b"A" * 64,
        b"a" * 64 + b"unexpected-suffix",
    ],
)
def test_skill_evolution_pipe_authority_rejects_malformed_payload_without_fallback(
    payload,
):
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
    finally:
        os.close(write_fd)
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD": str(read_fd),
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": "must-not-fallback",
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "must-not-fallback",
    }

    try:
        with pytest.raises(RuntimeError, match="credential"):
            server._SkillEvolutionBearerAuthority.capture(environ)
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


@pytest.mark.parametrize("descriptor", ["not-a-number", "-1", "0", "1", "2"])
def test_skill_evolution_pipe_authority_rejects_invalid_descriptor_without_fallback(
    descriptor,
):
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD": descriptor,
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": _LOCAL_EVOLUTION_TOKEN,
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    with pytest.raises(RuntimeError, match="credential descriptor"):
        server._SkillEvolutionBearerAuthority.capture(environ)

    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in environ
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environ


def test_skill_evolution_pipe_authority_rejects_closed_descriptor_without_fallback():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD": str(read_fd),
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": _LOCAL_EVOLUTION_TOKEN,
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    with pytest.raises(RuntimeError, match="unable to read Skill Evolution credential"):
        server._SkillEvolutionBearerAuthority.capture(environ)


def test_skill_evolution_pipe_read_times_out_without_fallback(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        server,
        "_SKILL_EVOLUTION_TOKEN_READ_TIMEOUT_SECONDS",
        0.03,
        raising=False,
    )
    environ = {
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD": str(read_fd),
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN": _LOCAL_EVOLUTION_TOKEN,
        "OMICSCLAW_REMOTE_AUTH_TOKEN": "remote-token",
    }

    def _close_writer() -> None:
        try:
            os.close(write_fd)
        except OSError:
            pass

    delayed_close = threading.Timer(0.5, _close_writer)
    delayed_close.start()
    try:
        with pytest.raises(RuntimeError, match="credential read timed out"):
            server._SkillEvolutionBearerAuthority.capture(environ)
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in environ
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environ
    finally:
        delayed_close.cancel()
        _close_writer()
        delayed_close.join(timeout=1)
        try:
            os.close(read_fd)
        except OSError:
            pass


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux /proc only")
def test_pipe_delivered_authority_never_enters_initial_process_environment():
    marker = "0123456789abcdef" * 4
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, marker.encode("ascii") + b"\n")
    finally:
        os.close(write_fd)
    env = os.environ.copy()
    env.pop("OMICSCLAW_SKILL_EVOLUTION_TOKEN", None)
    env["OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD"] = str(read_fd)
    code = f"""
import os
from pathlib import Path
from omicsclaw.surfaces.desktop import server

authority = server._SkillEvolutionBearerAuthority.capture(os.environ)
initial_environment = Path('/proc/self/environ').read_bytes()
assert {marker.encode('ascii')!r} not in initial_environment
assert authority.matches({marker!r})
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            pass_fds=(read_fd,),
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.close(read_fd)

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("model", "payload", "extra_key", "extra_value"),
    [
        (
            server.EvolutionApprovalRequest,
            {"approver": "human", "reason": "reviewed"},
            "target_path",
            "/tmp/forbidden",
        ),
        (
            server.EvolutionRejectionRequest,
            {"approver": "human", "reason": "insufficient evidence"},
            "source_hash",
            "sha256:caller-controlled",
        ),
        (
            server.EvolutionReconciliationRequest,
            {"operator": "operator", "reason": "backend restarted"},
            "patch",
            "caller-controlled patch",
        ),
        (
            server.EvolutionDeprecationProposalRequest,
            {
                "target_skill": "legacy-skill",
                "replacement_skill": "replacement-skill",
                "proposer": "maintainer",
                "reason": "maintained replacement",
                "support_event_ids": ["defect-a"],
            },
            "validator",
            "caller-controlled-validator",
        ),
        (
            server.EvolutionGotchaEntryRequest,
            {
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the matrix remains sparse.",
                "guidance": "Densify only the bounded slice.",
                "anchors": ["evolution_test.py:1"],
            },
            "patch",
            "caller-controlled patch",
        ),
        (
            server.EvolutionGotchaProposalRequest,
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs when the matrix remains sparse.",
                    "guidance": "Densify only the bounded slice.",
                    "anchors": ["evolution_test.py:1"],
                },
            },
            "target_path",
            "/tmp/forbidden",
        ),
    ],
)
def test_all_evolution_request_models_forbid_extra_control_fields(
    model,
    payload,
    extra_key,
    extra_value,
):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, extra_key: extra_value})


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/skill-evolution/proposal-1/approve",
            {
                "approver": "human",
                "reason": "reviewed",
                "target_path": "/tmp/forbidden",
            },
        ),
        (
            "/skill-evolution/proposal-1/reject",
            {
                "approver": "human",
                "reason": "insufficient evidence",
                "source_hash": "sha256:caller-controlled",
            },
        ),
        (
            "/skill-evolution/reconcile",
            {
                "operator": "operator",
                "reason": "backend restarted",
                "patch": "caller-controlled patch",
            },
        ),
        (
            "/skill-evolution/proposals/deprecation",
            {
                "target_skill": "legacy-skill",
                "replacement_skill": "replacement-skill",
                "proposer": "maintainer",
                "reason": "maintained replacement",
                "support_event_ids": ["defect-a"],
                "validator": "caller-controlled-validator",
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs when the matrix remains sparse.",
                    "guidance": "Densify only the bounded slice.",
                    "anchors": ["evolution_test.py:1"],
                },
                "target_path": "/tmp/forbidden",
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs when the matrix remains sparse.",
                    "guidance": "Densify only the bounded slice.",
                    "anchors": ["evolution_test.py:1"],
                    "patch": "caller-controlled patch",
                },
            },
        ),
    ],
)
def test_evolution_http_models_forbid_extra_control_fields(
    monkeypatch,
    path,
    payload,
):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        path,
        headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
        json=payload,
    )

    assert response.status_code == 422
    assert governance.calls == []


def test_skill_evolution_routes_expose_snapshot_refresh_and_human_decisions(monkeypatch):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"}

    assert client.get("/skill-evolution", headers=headers).status_code == 200
    refreshed = client.post("/skill-evolution/refresh", headers=headers)
    proposed = client.post(
        "/skill-evolution/proposals/deprecation",
        headers=headers,
        json={
            "target_skill": "legacy-skill",
            "replacement_skill": "replacement-skill",
            "proposer": "maintainer",
            "reason": "maintained replacement",
            "support_event_ids": ["defect-a", "defect-b", "defect-c"],
        },
    )
    gotcha = client.post(
        "/skill-evolution/proposals/gotcha",
        headers=headers,
        json={
            "target_skill": "evolution-test",
            "proposer": "maintainer",
            "reason": "repeated conditional failure",
            "support_event_ids": ["defect-a", "defect-b", "defect-c"],
            "entry": {
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the matrix remains sparse.",
                "guidance": "Densify only the bounded slice.",
                "anchors": ["evolution_test.py:1"],
            },
        },
    )
    approved = client.post(
        "/skill-evolution/proposal-1/approve",
        headers=headers,
        json={"approver": "human", "reason": "reviewed"},
    )
    rejected = client.post(
        "/skill-evolution/proposal-2/reject",
        headers=headers,
        json={"approver": "human", "reason": "insufficient evidence"},
    )
    reconciled = client.post(
        "/skill-evolution/reconcile",
        headers=headers,
        json={"operator": "operator", "reason": "backend restarted"},
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["created"][0]["proposal_id"] == "proposal-1"
    assert proposed.status_code == 200
    assert proposed.json()["proposal_id"] == "proposal-1"
    assert gotcha.status_code == 200
    assert gotcha.json()["proposal_id"] == "proposal-1"
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert reconciled.status_code == 200
    assert reconciled.json()["action"] == "restored_interrupted_approval"
    assert ("approve", "proposal-1", "human", "reviewed") in governance.calls
    assert (
        "propose_deprecation",
        "legacy-skill",
        "replacement-skill",
        "maintainer",
        "maintained replacement",
        ["defect-a", "defect-b", "defect-c"],
    ) in governance.calls
    assert (
        "propose_gotcha",
        "evolution-test",
        "maintainer",
        "repeated conditional failure",
        ["defect-a", "defect-b", "defect-c"],
        {
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the matrix remains sparse.",
            "guidance": "Densify only the bounded slice.",
            "anchors": ["evolution_test.py:1"],
        },
    ) in governance.calls
    assert (
        "reject",
        "proposal-2",
        "human",
        "insufficient evidence",
    ) in governance.calls
    assert ("reconcile", "operator", "backend restarted") in governance.calls


def test_http_gotcha_tracer_reaches_canonical_skill_and_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import json

    from omicsclaw.common.report import validate_result_envelope
    from omicsclaw.runtime.context.layers import load_skill_context
    from omicsclaw.skill.evolution import default_skill_health_ledger
    from omicsclaw.skill.evolution_governance import (
        SharedRunnerEvolutionExecutionAdapter,
    )
    import omicsclaw.skill.registry as registry_module
    from omicsclaw.skill.runner import run_skill
    from omicsclaw.skill.schema import load_skill_yaml
    from omicsclaw.skill.skill_md import render_skill_md

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "spatial" / "http-gotcha"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "http_gotcha.py"
    script_source = """from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true")
parser.add_argument("--input")
parser.add_argument("--output", required=True)
args = parser.parse_args()

output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)
is_defect = bool(args.input and Path(args.input).name.startswith("defect-"))
payload = {
    "skill": "http-gotcha",
    "version": "1.0.0",
    "completed_at": "2026-07-16T00:00:00+00:00",
    "input_checksum": "",
    "summary": {"method": "http-tracer"},
    "data": {"mode": "demo" if args.demo else "ordinary"},
    "status": "failed" if is_defect else "ok",
}
(output / "result.json").write_text(json.dumps(payload), encoding="utf-8")
if is_defect:
    raise RuntimeError("dense-only branch rejects sparse input")
"""
    script.write_text(script_source, encoding="utf-8")
    defect_line = next(
        number
        for number, line in enumerate(script_source.splitlines(), start=1)
        if "raise RuntimeError" in line
    )
    trace_anchor = f"{script.name}:{defect_line}"
    manifest = skill_dir / "skill.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "id": "http-gotcha",
                "name": "http-gotcha",
                "domain": "spatial",
                "version": "1.0.0",
                "type": "leaf",
                "summary": {
                    "load_when": "testing the real HTTP Gotcha governance path",
                    "skip_when": [
                        {
                            "condition": "not testing Gotcha governance",
                            "use": "another fixture",
                        }
                    ],
                    "trigger_keywords": ["http gotcha"],
                },
                "interface": {"outputs": {"files": ["result.json"]}},
                "runtime": {"entry": script.name},
                "lifecycle": {"status": "mvp"},
                "validation": {"level": "smoke-only"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        render_skill_md(
            load_skill_yaml(manifest),
            "## When to use\n\nUse for tests.\n\n"
            "## Flow\n\n1. Run the entry.\n\n"
            "## Gotchas\n\n- _None yet — append as failure modes are reported._\n\n"
            "## Key CLI\n\n`python http_gotcha.py --demo`\n\n"
            "## See also\n\n- None.\n",
        ),
        encoding="utf-8",
    )
    manifest_before = manifest.read_bytes()
    catalog = skills_root / "catalog.json"
    dag = skills_root / "skill_dag.json"
    catalog.write_bytes(b"catalog-sentinel\n")
    dag.write_bytes(b"dag-sentinel\n")

    ledger_path = tmp_path / "events.jsonl"
    proposals_path = tmp_path / "proposals.jsonl"
    monkeypatch.setenv("OMICSCLAW_SKILL_HEALTH_LEDGER", str(ledger_path))
    monkeypatch.setenv("OMICSCLAW_EVOLUTION_PROPOSALS", str(proposals_path))
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )

    # The production runner and governance both resolve the process singleton.
    # Track its original state through monkeypatch so even an assertion failure
    # restores the repository registry after this alternate-root tracer.
    monkeypatch.setattr(registry_module, "SKILLS_DIR", skills_root)
    monkeypatch.setattr(
        registry_module.registry,
        "_state",
        registry_module.registry._state,
    )
    registry_module.registry.reload(skills_root)
    assert registry_module.registry.skills["http-gotcha"]["gotcha_details"] == ()

    governance = server._skill_evolution_governance()
    assert governance.skills_root == skills_root.resolve()
    assert isinstance(
        governance.execution_adapter,
        SharedRunnerEvolutionExecutionAdapter,
    )

    run_results = []
    for label in ("defect-a", "defect-b", "defect-c", "conditional-success"):
        input_path = tmp_path / f"{label}.txt"
        input_path.write_text(label, encoding="utf-8")
        output_dir = tmp_path / f"output-{label}"
        result = run_skill(
            "http-gotcha",
            input_path=str(input_path),
            output_dir=str(output_dir),
        )
        run_results.append(result)
        payload = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
        assert validate_result_envelope(payload) == []

    assert [result.error_kind for result in run_results[:3]] == [
        "script_defect",
        "script_defect",
        "script_defect",
    ]
    assert all(not result.success for result in run_results[:3])
    assert run_results[3].success is True

    ledger = default_skill_health_ledger()
    events_before_approval = ledger.events()
    defects = [
        event
        for event in events_before_approval
        if event.evidence_kind == "ordinary" and event.outcome == "failed"
    ]
    counterexamples = [
        event
        for event in events_before_approval
        if event.evidence_kind == "ordinary" and event.outcome == "succeeded"
    ]
    assert len(defects) == 3
    assert len(counterexamples) == 1
    assert len({event.execution_fingerprint for event in defects}) == 3
    assert {
        event.skill_hash for event in events_before_approval
    } == {defects[0].skill_hash}
    assert {
        event.source_hash for event in events_before_approval
    } == {defects[0].source_hash}
    assert all(
        f"trace:{trace_anchor}" in event.evidence_refs
        for event in defects
    )

    client = TestClient(server.app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"}

    refreshed = client.post("/skill-evolution/refresh", headers=headers)
    assert refreshed.status_code == 200
    candidate = next(
        proposal
        for proposal in refreshed.json()["created"]
        if proposal["kind"] == "gotcha_evidence"
    )
    assert candidate["kind"] == "gotcha_evidence"
    assert candidate["status"] == "draft"
    assert skill_md.read_text(encoding="utf-8").count("_None yet") == 1

    proposed = client.post(
        "/skill-evolution/proposals/gotcha",
        headers=headers,
        json={
            "target_skill": "http-gotcha",
            "proposer": "maintainer",
            "reason": "reviewed exact-source conditional failures",
            "support_event_ids": [event.event_id for event in defects],
            "entry": {
                "lead": "Sparse input reaches a dense-only branch",
                "condition": "This occurs when the selected matrix remains sparse.",
                "guidance": "Densify only the bounded slice before this branch.",
                "anchors": [trace_anchor],
            },
        },
    )
    assert proposed.status_code == 200
    proposal = proposed.json()
    assert proposal["proposal_id"] != candidate["proposal_id"]
    assert proposal["proposed_change"]["source_candidate_id"] == candidate[
        "proposal_id"
    ]

    approved = client.post(
        f"/skill-evolution/{proposal['proposal_id']}/approve",
        headers=headers,
        json={"approver": "reviewer", "reason": "evidence and wording verified"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    events_after_approval = ledger.events()
    demo_events = [
        event
        for event in events_after_approval
        if event.evidence_kind == "demo" and event.outcome == "succeeded"
    ]
    assert len(events_after_approval) == len(events_before_approval) + 1
    assert len(demo_events) == 1
    assert manifest.read_bytes() == manifest_before
    assert catalog.read_bytes() == b"catalog-sentinel\n"
    assert dag.read_bytes() == b"dag-sentinel\n"

    rendered = skill_md.read_text(encoding="utf-8")
    assert "_None yet" not in rendered
    assert rendered.count("**Sparse input reaches a dense-only branch.**") == 1
    detail = (
        "**Sparse input reaches a dense-only branch.** "
        "This occurs when the selected matrix remains sparse. "
        "Densify only the bounded slice before this branch. "
        f"Evidence: `{trace_anchor}`."
    )
    assert registry_module.registry.skills["http-gotcha"]["gotcha_details"] == (
        detail,
    )
    assert detail in load_skill_context(skill="http-gotcha")

    snapshot = client.get("/skill-evolution", headers=headers)
    assert snapshot.status_code == 200
    by_id = {
        item["proposal_id"]: item for item in snapshot.json()["proposals"]
    }
    audited = by_id[proposal["proposal_id"]]
    assert audited["proposed_by"] == "maintainer"
    assert audited["proposal_reason"] == "reviewed exact-source conditional failures"
    assert audited["approved_by"] == "reviewer"
    assert audited["approval_reason"] == "evidence and wording verified"
    assert audited["before_hash"] == proposal["target_content_hash"]
    assert audited["after_hash"] == approved.json()["after_hash"]
    refreshed_again = client.post("/skill-evolution/refresh", headers=headers)
    assert refreshed_again.status_code == 200
    assert all(
        proposal["kind"] != "gotcha_evidence"
        for proposal in refreshed_again.json()["created"]
    )


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/skill-evolution/proposal-1/approve",
            {"approver": "   ", "reason": "reviewed"},
        ),
        (
            "/skill-evolution/proposal-1/reject",
            {"approver": "human", "reason": "   "},
        ),
        (
            "/skill-evolution/reconcile",
            {"operator": "   ", "reason": "backend restarted"},
        ),
        (
            "/skill-evolution/proposals/deprecation",
            {
                "target_skill": "legacy-skill",
                "replacement_skill": "replacement-skill",
                "proposer": "   ",
                "reason": "maintained replacement",
                "support_event_ids": ["defect-a"],
            },
        ),
        (
            "/skill-evolution/proposals/deprecation",
            {
                "target_skill": "   ",
                "replacement_skill": "replacement-skill",
                "proposer": "maintainer",
                "reason": "maintained replacement",
                "support_event_ids": ["defect-a"],
            },
        ),
        (
            "/skill-evolution/proposals/deprecation",
            {
                "target_skill": "legacy-skill",
                "replacement_skill": "replacement-skill",
                "proposer": "maintainer",
                "reason": "maintained replacement",
                "support_event_ids": ["   "],
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "**arbitrary markdown**",
                    "condition": "This occurs for sparse input.",
                    "guidance": "Use a bounded dense slice.",
                    "anchors": ["evolution_test.py:1"],
                },
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs for sparse input.",
                    "guidance": "Use a bounded dense slice.",
                    "anchors": ["../../outside.py:1"],
                },
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "/home/alice/patient_A.h5ad triggers this branch.",
                    "guidance": "Use a generalized remediation.",
                    "anchors": ["evolution_test.py:1"],
                },
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs for sparse input.",
                    "guidance": "token=private-value must be configured.",
                    "anchors": ["evolution_test.py:1"],
                },
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs for sparse input.",
                    "guidance": "Use a bounded\u2028dense slice.",
                    "anchors": ["evolution_test.py:1"],
                },
            },
        ),
        (
            "/skill-evolution/proposals/gotcha",
            {
                "target_skill": "evolution-test",
                "proposer": "maintainer",
                "reason": "repeated conditional failure",
                "support_event_ids": ["defect-a"],
                "entry": {
                    "lead": "Dense-only branch rejects sparse input",
                    "condition": "This occurs for sparse input.",
                    "guidance": "Use a bounded dense slice.",
                    "anchors": ['result.json["success"]'],
                },
            },
        ),
    ],
)
def test_skill_evolution_routes_reject_blank_semantic_fields_as_422(
    monkeypatch,
    path,
    payload,
):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        path,
        headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
        json=payload,
    )

    assert response.status_code == 422
    assert governance.calls == []


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "See ,/home/alice/patient.h5ad.",
        "See path:/home/alice/patient.h5ad.",
        "See //server/share/patient.h5ad.",
        "See ;C:/Users/Alice/patient.h5ad.",
        "client_secret=super-secret-value must be set.",
        "AWS_SECRET_ACCESS_KEY=super-secret-value must be set.",
        "authorization: Bearer-super-secret-value must be set.",
        "API key = super-secret-value must be set.",
        "ＡＰＩ＿ＫＥＹ＝super-secret-value must be set.",
        "API‐key=super-secret-value must be set.",
        "API·key=super-secret-value must be set.",
        "SECRET_KEY=super-secret-value must be set.",
        "secret-key: super-secret-value must be set.",
        "ＤＪＡＮＧＯ＿ＳＥＣＲＥＴ＿ＫＥＹ＝super-secret-value must be set.",
        "Open _https://private.example/patient before running.",
        "Use _private emphasis_ here.",
    ],
)
def test_skill_evolution_gotcha_route_rejects_privacy_bypasses_as_422(
    monkeypatch,
    unsafe_text,
):
    governance = _Governance()
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        "/skill-evolution/proposals/gotcha",
        headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
        json={
            "target_skill": "evolution-test",
            "proposer": "maintainer",
            "reason": "repeated conditional failure",
            "support_event_ids": ["defect-a"],
            "entry": {
                "lead": "Dense-only branch rejects sparse input",
                "condition": unsafe_text,
                "guidance": "Use a bounded dense slice.",
                "anchors": ["evolution_test.py:1"],
            },
        },
    )

    assert response.status_code == 422
    assert governance.calls == []


def test_skill_evolution_rejection_recovery_conflict_returns_409(monkeypatch):
    from omicsclaw.skill.evolution_governance import EvolutionRevalidationError

    governance = _Governance()

    def reject(_proposal_id: str, *, approver: str, reason: str):
        del approver, reason
        raise EvolutionRevalidationError(
            "an interrupted approval requires reconciliation"
        )

    governance.reject = reject  # type: ignore[method-assign]
    monkeypatch.setattr(server, "_skill_evolution_governance", lambda: governance)
    _configure_skill_evolution_authority(
        monkeypatch,
        dedicated_token=_LOCAL_EVOLUTION_TOKEN,
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        "/skill-evolution/proposal-1/reject",
        headers={"Authorization": f"Bearer {_LOCAL_EVOLUTION_TOKEN}"},
        json={"approver": "human", "reason": "reviewed"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "an interrupted approval requires reconciliation"
    )
