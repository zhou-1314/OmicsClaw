"""Conversation -> Run linkage for the desktop output 看板 "本对话" scope.

The producing chat session is recorded server-side (a sidecar next to
``result.json``) so ``/outputs/latest`` can attribute each Run to its
conversation without the frontend reverse-engineering it from media paths.
Stamping trusts only producer signals (output_dir / media) and a fresh,
finalized Run leaf, so a tool that merely *references* an older run can't
re-stamp it.
"""
import asyncio
import json
import os
import time
from pathlib import Path

import omicsclaw.surfaces.desktop.server as server


def _make_run(output_dir: Path, name: str, *, with_figure: bool = True) -> Path:
    run_dir = output_dir / name
    (run_dir / "figures").mkdir(parents=True, exist_ok=True)
    if with_figure:
        (run_dir / "figures" / "umap.png").write_bytes(b"\x89PNG\r\n")
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "skill": name.split("__")[0],
                "completed_at": "2026-06-01T12:00:00+00:00",
                "summary": {"n_clusters": 7},
            }
        )
    )
    return run_dir


def _age_run(run_dir: Path, seconds: float) -> None:
    """Backdate the completion marker so the freshness gate treats it as old."""
    old = time.time() - seconds
    os.utime(run_dir / "result.json", (old, old))


def _use_output_dir(monkeypatch, output_dir: Path) -> None:
    class _FakeCore:
        OUTPUT_DIR = output_dir

    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "_core", _FakeCore)


def test_media_tool_result_stamps_session_and_outputs_returns_it(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    run_dir = _make_run(output_dir, "spatial-domains__20260601_120000__abc123ff")
    _use_output_dir(monkeypatch, output_dir)

    media = [{"type": "image", "mimeType": "image/png", "localPath": str(run_dir / "figures" / "umap.png")}]
    server._stamp_session_for_run("sess-XYZ", {"summary": "done"}, media)

    sidecar = run_dir / server._SESSION_SIDECAR_NAME
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text())["session_id"] == "sess-XYZ"

    result = asyncio.run(server.outputs_latest(limit=10))
    runs = result["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == "spatial-domains__20260601_120000__abc123ff"
    assert runs[0]["session_id"] == "sess-XYZ"


def test_text_only_result_with_output_dir_key_links(monkeypatch, tmp_path):
    # A text skill (no figures) that reports its output_dir still links.
    output_dir = tmp_path / "output"
    run_dir = _make_run(output_dir, "literature__20260601_130000__def456aa", with_figure=False)
    _use_output_dir(monkeypatch, output_dir)

    server._stamp_session_for_run(
        "sess-LIT", {"output_dir": str(run_dir), "summary": {"gse_count": 1}}, []
    )
    result = asyncio.run(server.outputs_latest(limit=10))
    assert result["runs"][0]["session_id"] == "sess-LIT"


def test_python_repr_string_result_with_output_dir_links(monkeypatch, tmp_path):
    # The desktop SSE path str()-ifies dict results; a Python-repr string with
    # an output_dir must still resolve (ast.literal_eval fallback).
    output_dir = tmp_path / "output"
    run_dir = _make_run(output_dir, "literature__20260601_140000__aaa11122", with_figure=False)
    _use_output_dir(monkeypatch, output_dir)

    repr_payload = repr({"output_dir": str(run_dir), "summary": {"gse_count": 1}})
    assert repr_payload.startswith("{'")  # genuinely the single-quoted repr form
    server._stamp_session_for_run("sess-REPR", repr_payload, [])
    result = asyncio.run(server.outputs_latest(limit=10))
    assert result["runs"][0]["session_id"] == "sess-REPR"


def test_referencing_an_old_run_does_not_restamp_it(monkeypatch, tmp_path):
    # A read/list tool whose media points at a PREVIOUS run must not overwrite
    # that run's link with the current session (freshness gate).
    output_dir = tmp_path / "output"
    old_run = _make_run(output_dir, "spatial-domains__20260520_090000__old00001")
    _age_run(old_run, 3600)  # finalized an hour ago — not this turn
    _use_output_dir(monkeypatch, output_dir)

    media = [{"type": "image", "mimeType": "image/png", "localPath": str(old_run / "figures" / "umap.png")}]
    server._stamp_session_for_run("sess-OTHER", {"note": "viewing an old figure"}, media)
    assert not (old_run / server._SESSION_SIDECAR_NAME).exists()


def test_generic_path_key_is_not_trusted(monkeypatch, tmp_path):
    # Only producer fields (output_dir/output_directory/run_dir) are trusted —
    # a generic ``path`` key pointing into a run dir must NOT stamp it.
    output_dir = tmp_path / "output"
    run_dir = _make_run(output_dir, "spatial-domains__20260601_120000__abc123ff")
    _use_output_dir(monkeypatch, output_dir)

    server._stamp_session_for_run("sess-X", {"path": str(run_dir / "result.json")}, [])
    assert not (run_dir / server._SESSION_SIDECAR_NAME).exists()


def test_fresh_run_in_non_producer_field_is_not_stamped(monkeypatch, tmp_path):
    # A non-producing tool result that merely MENTIONS a FRESH run's figure in a
    # generic field — and carries no producer (pending) media — must not stamp
    # it. The freshness gate is not the only guard: provenance is. (on_tool_result
    # passes only pending_media, never the generically-extracted media, here [].)
    output_dir = tmp_path / "output"
    fresh = _make_run(output_dir, "spatial-domains__20260601_120000__fresh001")
    _use_output_dir(monkeypatch, output_dir)

    server._stamp_session_for_run(
        "sess-OTHER",
        {"viewed_image": str(fresh / "figures" / "umap.png"), "note": "looked at a chart"},
        [],
    )
    assert not (fresh / server._SESSION_SIDECAR_NAME).exists()


def test_non_run_tool_result_writes_no_sidecar(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    _make_run(output_dir, "spatial-domains__20260601_120000__abc123ff")
    _use_output_dir(monkeypatch, output_dir)

    server._stamp_session_for_run("sess-X", "plain text output, no paths", [])
    result = asyncio.run(server.outputs_latest(limit=10))
    assert "session_id" not in result["runs"][0]


def test_outputs_without_sidecar_carry_no_session(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    _make_run(output_dir, "spatial-domains__20260601_120000__abc123ff")
    _use_output_dir(monkeypatch, output_dir)

    result = asyncio.run(server.outputs_latest(limit=10))
    assert "session_id" not in result["runs"][0]
