"""Desktop visibility + 本对话 linkage for autonomous runs (audit A-2 / A-3).

A-2: the autonomous runner wrote only ``completion_report.json`` /
``result_summary.md``; the desktop ``/outputs`` reader keys on ``result.json``,
so every finished autonomous run was mis-reported running→failed and the
readable summary was never collected as a key file.

A-3: the verification-storm fix made the autonomous tool return a compact text
digest with no machine-readable producer field and no ``pending_media``, so the
desktop could neither inline the run's figures nor stamp the producing session
(the run never appeared under 本对话). These tests pin the restored linkage —
without re-bloating the LLM-facing digest (it stays the executor's return
value; media + a run-dir anchor travel through the ``pending_media``
side-channel that ``on_tool_result`` already consumes).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import omicsclaw.surfaces.desktop.server as server
from omicsclaw.runtime.agent import state as core
from omicsclaw.runtime.tools.builders.agent_executors import _register_autonomous_media


def _make_autonomous_run(
    output_dir: Path,
    name: str = "autonomous-code__20260601_120000__run00001",
    *,
    with_figure: bool = True,
) -> Path:
    run = output_dir / name
    (run / "figures").mkdir(parents=True, exist_ok=True)
    if with_figure:
        (run / "figures" / "plot.png").write_bytes(b"\x89PNG\r\n")
    (run / "result_summary.md").write_text("# Autonomous Code Runner Summary\n", encoding="utf-8")
    (run / "completion_report.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    (run / "result.json").write_text(
        json.dumps(
            {
                "skill": "autonomous-code",
                "status": "completed",
                "completed_at": "2026-06-01T12:00:00+00:00",
                "summary": "done",
            }
        ),
        encoding="utf-8",
    )
    return run


def _use_output_dir(monkeypatch, output_dir: Path) -> None:
    class _FakeCore:
        OUTPUT_DIR = output_dir

    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "_core", _FakeCore)


# --- A-2: key files ---------------------------------------------------------


def test_collect_key_files_includes_summary_and_completion_report(tmp_path: Path):
    run = _make_autonomous_run(tmp_path / "output")
    names = {f["name"] for f in server._collect_key_files(run)}
    assert "result.json" in names
    assert "result_summary.md" in names
    assert "completion_report.json" in names


# --- A-3: pending_media registration ---------------------------------------


def test_register_autonomous_media_without_request_summarizes_not_inlines(tmp_path: Path):
    """Default (no return_media): figures are NOT queued for inline display.
    The run queues a collapsed output_summary that counts the figures and
    anchors the run dir — so the desktop shows a 'view outputs' entry, not
    auto-dumped plots."""
    run = _make_autonomous_run(tmp_path / "output", with_figure=True)
    core.pending_media.pop("sessFIG", None)
    items = _register_autonomous_media("sessFIG", str(run))

    assert all(it.get("type") != "photo" for it in items), "figures must NOT auto-inline"
    summary = next(it for it in items if it.get("type") == "output_summary")
    assert summary["figures"] >= 1
    assert summary["run_dir"] == str(run)
    queued = core.pending_media.get("sessFIG") or []
    assert any(it.get("type") == "output_summary" for it in queued)
    assert all(it.get("type") != "photo" for it in queued)
    core.pending_media.pop("sessFIG", None)


def test_register_autonomous_media_with_return_media_queues_figure_cards(tmp_path: Path):
    """When the user explicitly asked (return_media), the matching figures ride
    through as cards exactly like the skill executor's path."""
    run = _make_autonomous_run(
        tmp_path / "output", name="autonomous-code__20260601_140000__run00003", with_figure=True
    )
    core.pending_media.pop("sessREQ", None)
    items = _register_autonomous_media("sessREQ", str(run), return_media="all")
    fig_names = {Path(i["path"]).name for i in items if i.get("path")}
    assert "plot.png" in fig_names
    core.pending_media.pop("sessREQ", None)


def test_register_autonomous_media_textonly_still_anchors(tmp_path: Path):
    """A text-only autonomous run (no figures) must still register a run-dir
    anchor so the producing session can be stamped (本对话). The anchor is the
    output_summary's run_dir (zero counts)."""
    run = _make_autonomous_run(tmp_path / "output", name="autonomous-code__20260601_130000__run00002", with_figure=False)
    core.pending_media.pop("sessTXT", None)
    items = _register_autonomous_media("sessTXT", str(run))
    assert items, "text-only run must register a run-dir anchor for 本对话 linkage"
    summary = next(it for it in items if it.get("type") == "output_summary")
    assert summary["run_dir"] == str(run)
    core.pending_media.pop("sessTXT", None)


def test_autonomous_run_stamps_session_via_registered_media(monkeypatch, tmp_path: Path):
    """End-to-end: a non-JSON digest (display_output) still links to its
    session because the registered pending_media carries a run-dir path."""
    output_dir = tmp_path / "output"
    run = _make_autonomous_run(output_dir, with_figure=True)
    _use_output_dir(monkeypatch, output_dir)

    media = [{"type": "image", "path": str(run / "figures" / "plot.png")}]
    server._stamp_session_for_run("sessE2E", "Autonomous analysis completed (run ...). ## Answer ...", media)

    assert (run / server._SESSION_SIDECAR_NAME).is_file()
    result = asyncio.run(server.outputs_latest(limit=10))
    assert result["runs"][0]["session_id"] == "sessE2E"
    assert result["runs"][0]["status"] == "completed"


def test_stamp_session_via_output_summary_anchor(monkeypatch, tmp_path: Path):
    """A figure-suppressed run carries no media file path — only the collapsed
    output_summary's ``runDir`` anchors it. Session stamping (本对话) must still
    resolve the run from that anchor."""
    output_dir = tmp_path / "output"
    run = _make_autonomous_run(output_dir, name="autonomous-code__20260601_150000__run00004", with_figure=True)
    _use_output_dir(monkeypatch, output_dir)

    media = [{"type": "output_summary", "figures": 1, "tables": 0, "notebooks": 0, "runDir": str(run)}]
    server._stamp_session_for_run("sessSUM", "Autonomous analysis completed (run ...).", media)

    assert (run / server._SESSION_SIDECAR_NAME).is_file()
    assert server._read_session_sidecar(run) == "sessSUM"
