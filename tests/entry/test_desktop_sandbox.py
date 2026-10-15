"""Desktop sandbox integration tests: per-domain network approval, snapshot, and REST routes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omicsclaw.engine import AgentEngine
from omicsclaw.entry import (
    AppConfig,
    SandboxMode,
    SkillsIndex,
    assembly,
    attach_sessions,
    open_app,
)
from omicsclaw.entry.desktop import (
    DesktopInteractions,
    answer_permission,
    create_desktop_app,
    open_chat_stream,
)
from omicsclaw.entry.desktop.doctor import build_doctor_report
from omicsclaw.entry.sandbox import (
    EGRESS_GRANTS_FILENAME,
    EGRESS_GRANTS_HYPHEN_FILENAME,
    RequestNetworkAccessTool,
    load_persisted_egress_grants,
    open_sandbox,
)
from omicsclaw.sandbox import BackendName, EgressMode, Level, ProbeReport, SandboxConfig
from omicsclaw.schema import Message, Role, ToolCall
from tests.entry.test_sandbox import _FakeSandboxManager, fake_sandbox  # noqa: F401
from tests.entry.test_turn import _Scripted

WAIT_S = 5.0
KEY_1 = "a" * 32


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path.resolve()


def _config(workspace: Path, **overrides: Any) -> AppConfig:
    values: dict[str, Any] = {"workspace": workspace, "skills_index": SkillsIndex.OFF}
    values.update(overrides)
    return AppConfig(**values)


def _decode_frame(frame: str) -> tuple[str, Any]:
    lines = frame[:-2].split("\n")
    if lines[0].startswith("id: "):
        lines = lines[1:]
    (line,) = lines
    payload = json.loads(line[len("data: ") :])
    data = payload["data"]
    if payload["type"] in {
        "tool_use",
        "tool_result",
        "permission_request",
        "result",
        "status",
        "event_omitted",
    }:
        data = json.loads(data)
    return payload["type"], data


def test_request_network_access_policy_requires_ask_every_time():
    assert RequestNetworkAccessTool.policy.ask_every_time is True
    assert RequestNetworkAccessTool.policy.prompts_for_itself is True


def test_allow_session_on_domain_a_does_not_auto_approve_domain_b_and_does_not_persist(
    workspace: Path,
    fake_sandbox,  # noqa: F811
    monkeypatch,
):
    _state, instances = fake_sandbox
    provider = _Scripted(
        Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(
                    id="net0",
                    name="request_network_access",
                    arguments=json.dumps(
                        {
                            "domain": "api.domain-once.org",
                            "reason": "fetch cohort once",
                            "scope": "always",
                        }
                    ),
                ),
            ),
        ),
        Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(
                    id="net1",
                    name="request_network_access",
                    arguments=json.dumps(
                        {
                            "domain": "api.domain-a.org",
                            "reason": "fetch cohort A",
                        }
                    ),
                ),
            ),
        ),
        Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(
                    id="net2",
                    name="request_network_access",
                    arguments=json.dumps(
                        {
                            "domain": "api.domain-b.org",
                            "reason": "fetch cohort B",
                        }
                    ),
                ),
            ),
        ),
        Message(role=Role.ASSISTANT, content="all three domains processed"),
    )
    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: provider)
    config = _config(workspace, sandbox=SandboxMode.AUTO)

    async def scenario():
        app = await open_app(config)
        app = dataclasses.replace(
            app,
            engine=AgentEngine(provider, app.registry, config.engine_config()),
        )
        app = attach_sessions(app, abandon_grace_s=None)
        interactions = DesktopInteractions(app)
        cards: list[dict[str, Any]] = []
        answers: list[dict[str, Any]] = []
        try:
            body = {
                "ingress_schema_version": 3,
                "content": "query two biobanks",
                "session_id": "s1",
                "source_request_id": KEY_1,
            }
            stream = await open_chat_stream(
                app, body, keepalive_s=None, interactions=interactions
            )
            frames: list[tuple[str, Any]] = []
            async with stream.body as frames_iter:
                async for raw in frames_iter:
                    kind, data = _decode_frame(raw)
                    frames.append((kind, data))
                    if kind == "permission_request":
                        cards.append(data)
                        # Card 1: once, Card 2: session, Card 3: always
                        scopes = ("once", "session", "always")
                        chosen_scope = scopes[len(cards) - 1]
                        ans = await answer_permission(
                            app,
                            interactions,
                            {
                                "request_id": data["request_id"],
                                "decision": {"behavior": "allow", "scope": chosen_scope},
                            },
                        )
                        answers.append(ans)
            return cards, answers, frames, app.sandbox.snapshot()
        finally:
            await app.aclose()

    cards, answers, _frames, snap = asyncio.run(asyncio.wait_for(scenario(), WAIT_S))

    # All three domains must have triggered an approval card!
    assert len(cards) == 3
    assert "api.domain-once.org" in cards[0]["arguments"]
    assert "api.domain-a.org" in cards[1]["arguments"]
    assert "api.domain-b.org" in cards[2]["arguments"]

    # First decision was once-scoped (even though model requested scope="always"): memory only
    assert answers[0]["ok"] is True
    assert answers[0]["scope"] == "once"
    assert "api.domain-once.org" in instances[0].granted_domains

    # Second decision was session-scoped: granted in memory, NOT persisted on disk
    assert answers[1]["ok"] is True
    assert answers[1]["scope"] == "session"
    assert "api.domain-a.org" in instances[0].granted_domains

    # Third decision was always-scoped: granted in memory AND persisted on disk
    assert answers[2]["ok"] is True
    assert answers[2]["scope"] == "always"
    assert "api.domain-b.org" in instances[0].granted_domains

    grants_hyphen = workspace / ".omicsclaw" / EGRESS_GRANTS_HYPHEN_FILENAME
    grants_underscore = workspace / ".omicsclaw" / EGRESS_GRANTS_FILENAME
    assert grants_hyphen.is_file()
    assert grants_underscore.is_file()
    persisted = load_persisted_egress_grants(grants_underscore)
    assert "api.domain-b.org" in persisted
    assert "api.domain-once.org" not in persisted
    assert "api.domain-a.org" not in persisted
    assert "api.domain-b.org" in snap["persisted_domains"]
    assert "api.domain-once.org" not in snap["persisted_domains"]
    assert "api.domain-a.org" not in snap["persisted_domains"]


def test_sandbox_rest_routes_and_doctor_report(
    workspace: Path,
    fake_sandbox,  # noqa: F811
):
    config = _config(workspace, sandbox=SandboxMode.AUTO, sandbox_auto_approve=True)

    async def build():
        app = attach_sessions(await open_app(config), abandon_grace_s=None)
        return app

    app = asyncio.run(build())
    try:
        api = create_desktop_app(app)
        client = TestClient(api)

        # 1. GET /api/sandbox/status
        res = client.get("/api/sandbox/status")
        assert res.status_code == 200
        status = res.json()
        assert status["active"] is True
        assert status["backend"] == "bwrap"
        assert status["level"] == "isolated"
        assert status["auto_approve_active"] is True
        assert "bash" in status["coverage"]
        assert isinstance(status["allowed_domains"], list)
        assert isinstance(status["persisted_domains"], list)
        assert isinstance(status["recent_blocked"], list)

        # 2. POST /api/sandbox/allow-domain (session + persist)
        allow_res = client.post(
            "/api/sandbox/allow-domain",
            json={"domain": "api.ensembl-mirror.org", "persist": True},
        )
        assert allow_res.status_code == 200
        updated = allow_res.json()
        assert "api.ensembl-mirror.org" in updated["allowed_domains"]
        assert "api.ensembl-mirror.org" in updated["persisted_domains"]

        # 3. POST /api/sandbox/allow-domain rejects denylisted domain with 400
        deny_res = client.post(
            "/api/sandbox/allow-domain",
            json={"domain": "pastebin.com", "persist": False},
        )
        assert deny_res.status_code == 400
        assert "denylist" in deny_res.json()["detail"].lower()

        # 4. Doctor report includes structured sandbox snapshot
        doc = build_doctor_report(app)
        assert "sandbox" in doc
        assert "api.ensembl-mirror.org" in doc["sandbox"]["allowed_domains"]
        assert "api.ensembl-mirror.org" in doc["sandbox"]["persisted_domains"]

        # 5. DELETE /api/sandbox/allow-domain revokes from memory and persisted file
        del_res = client.request(
            "DELETE",
            "/api/sandbox/allow-domain",
            json={"domain": "api.ensembl-mirror.org"},
        )
        assert del_res.status_code == 200
        after_del = del_res.json()
        assert "api.ensembl-mirror.org" not in after_del["allowed_domains"]
        assert "api.ensembl-mirror.org" not in after_del["persisted_domains"]
    finally:
        asyncio.run(app.aclose())
