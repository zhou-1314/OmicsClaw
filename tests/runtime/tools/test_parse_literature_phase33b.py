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
