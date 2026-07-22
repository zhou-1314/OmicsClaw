"""Tests for Bench Phase 3.3b — parse_literature permission gate + thread-scoped
dataset registration.

Covers: the ASK gate fires for parse_literature (download is a proposal, ADR 0021)
while fetch_geo_metadata (the metadata reader, download defaults False) stays
ungated; thread_id reaches the executor via context_params (the silent-un-scoping
guard); and the literature download-result → DatasetMemory registration skips the
metadata sidecar, skips failed GSEs, and stamps the active thread_id (empty =
legacy un-scoped).

Note: fetch_geo_metadata's own download=True branch is an ungated pre-existing
bypass tracked for follow-up gating — this test only asserts the tool is not
force-gated (so the metadata path stays friction-free), not that it cannot download.
"""

from __future__ import annotations

import json

import pytest


def _bot_specs_by_name():
    from omicsclaw.runtime.context.layers import ContextAssemblyRequest  # noqa: F401
    from omicsclaw.runtime.tools.builders.agent import (
        BotToolContext,
        build_bot_tool_specs,
    )

    specs = build_bot_tool_specs(
        BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
    )
    return {s.name: s for s in specs}


# ---- ToolSpec gate + context wiring ----------------------------------------


def test_parse_literature_is_ask_gated_and_thread_aware():
    from omicsclaw.runtime.tools.spec import APPROVAL_MODE_ASK

    spec = _bot_specs_by_name()["parse_literature"]
    assert spec.approval_mode == APPROVAL_MODE_ASK
    assert "thread_id" in spec.context_params
    assert "session_id" in spec.context_params


def test_parse_literature_requires_approval_but_metadata_reader_does_not():
    from omicsclaw.runtime.policy.policy import (
        TOOL_POLICY_REQUIRE_APPROVAL,
        evaluate_tool_policy,
    )

    specs = _bot_specs_by_name()
    # Default (untrusted, no prior approval) state → the ASK gate fires.
    pl = evaluate_tool_policy("parse_literature", specs["parse_literature"])
    assert pl is not None and pl.action == TOOL_POLICY_REQUIRE_APPROVAL

    # fetch_geo_metadata stays ungated (the metadata reader, download defaults False).
    fg = evaluate_tool_policy("fetch_geo_metadata", specs["fetch_geo_metadata"])
    assert fg is not None and fg.action != TOOL_POLICY_REQUIRE_APPROVAL


def test_thread_id_reaches_executor_via_context_params():
    # Risk #1 guard: if context_params omits thread_id, every literature dataset
    # is silently captured un-scoped. Assert the declarative wiring delivers it.
    from omicsclaw.runtime.tools.executor import build_executor_kwargs

    spec = _bot_specs_by_name()["parse_literature"]
    kwargs = build_executor_kwargs(
        spec, {"session_id": "s1", "thread_id": "t-glioma", "chat_id": 7}
    )
    assert kwargs.get("thread_id") == "t-glioma"
    assert kwargs.get("session_id") == "s1"


# ---- dataset registration from the literature result.json ------------------


@pytest.mark.asyncio
async def test_register_literature_datasets_skips_metadata_and_scopes_thread(
    tmp_path, monkeypatch
):
    import omicsclaw.runtime.agent.state  # noqa: F401 — load state first (prod order)
    import omicsclaw.runtime.tools.builders.agent_executors as ae

    out_dir = tmp_path / "lit"
    out_dir.mkdir()
    # Mirror the literature skill's real layout (skills/literature/literature_parse.py):
    # downloads land under `<out_dir>/data/<gse_id>/...`, i.e. contained within the
    # Run's own output tree. `_register_literature_datasets` now enforces that
    # containment via `is_scientific_output_file` (ADR 0065), so fixture files must
    # actually exist on disk under `out_dir` — not merely be referenced by path.
    gse1_dir = out_dir / "data" / "GSE1"
    gse1_dir.mkdir(parents=True)
    (gse1_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (gse1_dir / "matrix.h5ad").write_bytes(b"")
    result = {
        "data": {
            "metadata": {"technology": "10x"},
            "download_results": [
                {
                    "gse_id": "GSE1",
                    "status": "success",
                    "files": [
                        str(gse1_dir / "metadata.json"),
                        str(gse1_dir / "matrix.h5ad"),
                    ],
                },
                {  # failed GSE → skipped entirely
                    "gse_id": "GSE2",
                    "status": "failed",
                    "files": [str(tmp_path / "GSE2" / "x.h5ad")],
                },
            ],
        }
    }
    (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    captured: list = []

    async def fake_capture(session_id, input_path, data_type="", thread_id=""):
        captured.append((input_path, data_type, thread_id))

    monkeypatch.setattr(ae, "_auto_capture_dataset", fake_capture)
    await ae._register_literature_datasets(out_dir, "s1", "t-glioma")

    # metadata.json skipped; failed GSE2 skipped; only GSE1/matrix.h5ad captured.
    assert len(captured) == 1
    path, dtype, tid = captured[0]
    assert path.endswith("matrix.h5ad")
    assert dtype == "10x"
    assert tid == "t-glioma"


@pytest.mark.asyncio
async def test_register_literature_datasets_legacy_unscoped_when_thread_empty(
    tmp_path, monkeypatch
):
    # thread_id="" forwards through to _auto_capture_dataset as "" → the dataset
    # lands un-scoped (legacy dataset://<basename>), backward compatible.
    import omicsclaw.runtime.agent.state  # noqa: F401
    import omicsclaw.runtime.tools.builders.agent_executors as ae

    out_dir = tmp_path / "lit"
    out_dir.mkdir()
    # Fixture file must exist under out_dir — see containment note in the sibling
    # test above (ADR 0065 / is_scientific_output_file).
    gse1_dir = out_dir / "data" / "GSE1"
    gse1_dir.mkdir(parents=True)
    (gse1_dir / "matrix.h5ad").write_bytes(b"")
    result = {
        "data": {
            "metadata": {"technology": "10x"},
            "download_results": [
                {"gse_id": "GSE1", "status": "success",
                 "files": [str(gse1_dir / "matrix.h5ad")]},
            ],
        }
    }
    (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    captured: list = []

    async def fake_capture(session_id, input_path, data_type="", thread_id=""):
        captured.append(thread_id)

    monkeypatch.setattr(ae, "_auto_capture_dataset", fake_capture)
    await ae._register_literature_datasets(out_dir, "s1", "")
    assert captured == [""]  # un-scoped


@pytest.mark.asyncio
async def test_register_literature_datasets_soft_fails_without_result_json(tmp_path):
    import omicsclaw.runtime.agent.state  # noqa: F401
    import omicsclaw.runtime.tools.builders.agent_executors as ae

    # No result.json present → returns quietly, never raises.
    await ae._register_literature_datasets(tmp_path, "s1", "t-glioma")


# ---- ADR 0070: execute_parse_literature claims a fresh output dir ------------
#
# execute_parse_literature spawns the `literature` leaf Skill directly (it does
# NOT go through the shared `_prepare_skill_run` runner), so it must claim a
# fresh, exclusively-owned output directory itself. These two tests guard the
# ADR-0070 gate on this agent-tool Surface: fail closed on a non-fresh target,
# and claim before spawn on a fresh one.


class _FrozenClock:
    """Freezes ``datetime.now()`` so ``literature-parse_<ts>`` is deterministic."""

    @staticmethod
    def now(tz=None):
        import datetime as _dt

        return _dt.datetime(2026, 7, 22, 12, 0, 0)


def _stub_literature_skill(ae, monkeypatch, tmp_path):
    """Point the executor at an isolated OUTPUT_DIR + a dummy literature script."""
    monkeypatch.setattr(ae, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(ae, "OMICSCLAW_DIR", tmp_path)
    monkeypatch.setattr(ae, "datetime", _FrozenClock)
    lit = tmp_path / "skills" / "literature"
    lit.mkdir(parents=True)
    (lit / "literature_parse.py").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_parse_literature_fails_closed_on_non_fresh_output_dir(
    tmp_path, monkeypatch
):
    # Regression guard for ADR 0070 R9: a prior run's result.json must NOT be
    # inherited by a new execution, and the child must never spawn against a
    # dirty directory.
    import omicsclaw.runtime.agent.state  # noqa: F401
    import omicsclaw.runtime.tools.builders.agent_executors as ae

    _stub_literature_skill(ae, monkeypatch, tmp_path)

    # Pre-seed the EXACT directory this call will compute with stale evidence.
    stale_dir = tmp_path / "out" / "literature-parse_20260722_120000"
    stale_dir.mkdir(parents=True)
    (stale_dir / "result.json").write_text('{"status": "ok"}', encoding="utf-8")

    spawn_calls: list = []

    async def _spy_spawn(*cmd, **kw):
        spawn_calls.append(cmd)
        raise AssertionError("must not spawn against a non-fresh output dir")

    monkeypatch.setattr(ae.asyncio, "create_subprocess_exec", _spy_spawn)

    result = await ae.execute_parse_literature(
        {"input_value": "10.1234/x"}, session_id=None, thread_id=""
    )

    assert spawn_calls == []  # never spawned
    assert result.startswith("Error:")
    assert "fresh output directory" in result  # the claim's fail-closed message
    # Existing user evidence is never deleted or rewritten (ADR 0070).
    assert json.loads((stale_dir / "result.json").read_text()) == {"status": "ok"}


@pytest.mark.asyncio
async def test_parse_literature_claims_fresh_output_dir_before_spawn(
    tmp_path, monkeypatch
):
    # On a fresh target the executor claims the directory (durable marker present)
    # and the child is spawned INTO the claimed dir — proving claim-before-spawn.
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    import omicsclaw.runtime.agent.state  # noqa: F401
    import omicsclaw.runtime.tools.builders.agent_executors as ae

    _stub_literature_skill(ae, monkeypatch, tmp_path)
    # Keep the test hermetic: the KG ingest is a background best-effort spawn.
    monkeypatch.setattr(ae, "_spawn_literature_kg_ingest", lambda *a, **k: None)

    from pathlib import Path

    seen: dict = {}

    class _FakeProc:
        returncode = 0

        def __init__(self, out_dir):
            self._out_dir = out_dir

        async def communicate(self):
            # The claim marker must already exist when the child runs.
            assert (self._out_dir / OUTPUT_CLAIM_FILENAME).exists()
            (self._out_dir / "report.md").write_text("# literature ok\n", encoding="utf-8")
            return (b"done", b"")

    async def _fake_spawn(*cmd, **kw):
        args = list(cmd)
        out = Path(args[args.index("--output") + 1])
        seen["out"] = out
        return _FakeProc(out)

    monkeypatch.setattr(ae.asyncio, "create_subprocess_exec", _fake_spawn)

    result = await ae.execute_parse_literature(
        {"input_value": "10.1234/x", "auto_download": False},
        session_id=None,
        thread_id="",
    )

    assert "literature ok" in result
    claimed = seen["out"]
    assert claimed.name == "literature-parse_20260722_120000"
    assert (claimed / OUTPUT_CLAIM_FILENAME).exists()  # durable claim survived
