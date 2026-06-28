"""One-click Ideate→Analyze endpoint (audit E-(2→3)).

`POST /thread/{id}/run-hypothesis` builds a handoff packet for a hypothesis with
the Analysis-Router-authoritative skill (the SAME (claim, dataset) the Ideate
route-preview showed) and runs it via the deterministic outbox executor — so the
result lands as a thread analysis without the user composing a chat message.
Augments the chat path (ADR 0023 §6), reusing build_packet + outbox.run_packet.
"""

from __future__ import annotations

import asyncio
import types

import pytest

# Import agent state first (state↔executors↔kg_tools cycle).
import omicsclaw.runtime.agent.state as _state  # noqa: F401
import omicsclaw.runtime.tools.kg_tools as kg_tools
from omicsclaw.surfaces.desktop import server


def _run(coro):
    return asyncio.run(coro)


def _setup(monkeypatch, *, run_result):
    """Wire the server globals + the build/run chain; return a capture dict."""
    cap: dict = {}

    monkeypatch.setattr(server, "_KG_AVAILABLE", True)
    monkeypatch.setattr(server, "_memory_client", object())
    monkeypatch.setattr(server, "_resolve_shared_kg_home", lambda: "/kg")

    def build_packet(cfg, slug, target_skill, notes):
        cap["build"] = {"slug": slug, "target_skill": target_skill}
        return types.SimpleNamespace(packet_id="pk-h1", target=types.SimpleNamespace(skill_name=target_skill, kind="skill"))

    fake_handoff = types.SimpleNamespace(
        config=types.SimpleNamespace(resolve=lambda home: f"cfg:{home}"),
        build_packet=build_packet,
        write_packet=lambda cfg, packet: cap.update(wrote=packet.packet_id),
    )
    monkeypatch.setattr(kg_tools, "_import_kg_handoff", lambda: fake_handoff)
    monkeypatch.setattr(kg_tools, "_router_skill_for_hypothesis", lambda slug, home, dataset_path="": "sc-de")

    async def fake_resolve(client, thread_id):
        cap["dataset_for"] = thread_id
        return "data/x.h5ad"

    monkeypatch.setattr("omicsclaw.memory.compat.resolve_thread_dataset_path", fake_resolve)

    async def fake_run_packet(memory_client, cfg, thread_id, packet_id, *, input_path=None, verdict="inconclusive"):
        cap["ran"] = {"packet_id": packet_id, "thread_id": thread_id, "input_path": input_path, "verdict": verdict}
        return run_result

    monkeypatch.setattr("omicsclaw.surfaces.desktop.outbox.run_packet", fake_run_packet)
    return cap


@pytest.mark.asyncio
async def test_run_hypothesis_builds_router_skill_packet_and_runs_it(monkeypatch):
    cap = _setup(monkeypatch, run_result={"status": "recorded", "skill": "sc-de", "output_dir": "/o/run1", "verdict": "inconclusive"})
    req = server.RunHypothesisRequest(hypothesis_slug="h1")
    out = await server.thread_run_hypothesis("t1", req)

    assert cap["build"]["target_skill"] == "sc-de"   # Router-authoritative skill fed to build_packet
    assert cap["wrote"] == "pk-h1"                    # packet written before run
    assert cap["ran"]["packet_id"] == "pk-h1"          # the built packet was run
    assert out["packet_id"] == "pk-h1"
    assert out["status"] == "recorded"
    assert out["thread_id"] == "t1"


@pytest.mark.asyncio
async def test_run_hypothesis_surfaces_executor_error_as_400(monkeypatch):
    _setup(monkeypatch, run_result={"status": "error", "error": "no input dataset for this thread"})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await server.thread_run_hypothesis("t1", server.RunHypothesisRequest(hypothesis_slug="h1"))
    assert ei.value.status_code == 400
    assert "no input dataset" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_run_hypothesis_rejects_unsafe_slug_before_kg_calls(monkeypatch):
    # codex must-fix: an attacker-influenced slug must be gated before build_packet
    # touches the filesystem (wiki/hypotheses/<slug>.md, handoff/outbox/<id>.json).
    cap = _setup(monkeypatch, run_result={"status": "recorded"})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await server.thread_run_hypothesis("t1", server.RunHypothesisRequest(hypothesis_slug="../../etc/passwd"))
    assert ei.value.status_code == 400
    assert "build" not in cap  # build_packet was NOT reached
    assert "ran" not in cap


def test_run_hypothesis_503_when_kg_unavailable(monkeypatch):
    monkeypatch.setattr(server, "_KG_AVAILABLE", False)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _run(server.thread_run_hypothesis("t1", server.RunHypothesisRequest(hypothesis_slug="h1")))
    assert ei.value.status_code == 503
