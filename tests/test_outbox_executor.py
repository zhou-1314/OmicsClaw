"""Deterministic outbox executor — idea→analysis→verdict closure (audit E).

Tests the executor's orchestration in isolation: the KG boundary
(``_load_packet`` / ``_record_packet_result``) and the skill runner
(``arun_skill``) are stubbed, so these run without OmicsClaw-KG installed
(it's only on sys.path inside the live desktop process). The real
``record_result`` behavior is covered in the OmicsClaw-KG test suite.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import omicsclaw.surfaces.desktop.outbox as outbox


def _run(coro):
    return asyncio.run(coro)


def _packet(*, kind="omicsclaw_skill", skill_name="spatial-domains", recommended=None):
    return types.SimpleNamespace(
        packet_id="hof-x-h1",
        target=types.SimpleNamespace(kind=kind, skill_name=skill_name),
        recommended_skills=recommended or [],
        hypothesis=types.SimpleNamespace(slug="h1"),
        experiment_slug=None,
        step_id=None,
        question="does X drive Y?",
    )


def _ok_result(output_dir, files):
    return types.SimpleNamespace(
        success=True, exit_code=0, output_dir=str(output_dir), files=files, stderr=""
    )


# --- pure helpers -----------------------------------------------------------


def test_resolve_skill_name_prefers_target_then_recommended():
    assert outbox._resolve_skill_name(_packet(skill_name="A")) == "A"
    assert (
        outbox._resolve_skill_name(_packet(kind="file_drop", skill_name=None, recommended=["B"]))
        == "B"
    )
    assert (
        outbox._resolve_skill_name(_packet(kind="file_drop", skill_name=None, recommended=[]))
        is None
    )


def test_flatten_summary_uses_result_json_then_artifact_count():
    r = types.SimpleNamespace(files=["a.png"])
    s = outbox._flatten_summary("sk", r, {"summary": {"n_clusters": 7, "n_cells": 100}})
    assert s.startswith("sk:") and "n_clusters=7" in s
    assert "1 artifact" in outbox._flatten_summary("sk", r, None)


# --- orchestration ----------------------------------------------------------


def test_run_packet_records_on_success(monkeypatch, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "result.json").write_text(
        json.dumps({"skill": "spatial-domains", "summary": {"n_clusters": 7}})
    )
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def fake_arun(skill, **kw):
        assert skill == "spatial-domains"
        assert kw["input_path"].endswith("x.h5ad")
        return _ok_result(out, [str(out / "figures" / "umap.png")])

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    monkeypatch.setattr(outbox, "validate_input_path", lambda p, allow_dir=False: Path(p))

    captured = {}

    def fake_record(cfg, pid, *, verdict, summary, artifact_paths):
        captured.update(verdict=verdict, summary=summary, artifacts=artifact_paths, pid=pid)
        return {"status": "recorded", "hypothesis_slug": "h1", "suggested_verdict": verdict}

    monkeypatch.setattr(outbox, "_record_packet_result", fake_record)

    res = _run(
        outbox.run_packet(
            object(), object(), "t1", "hof-x-h1", input_path="/data/x.h5ad", verdict="validated"
        )
    )
    assert res["status"] == "recorded"
    assert captured["pid"] == "hof-x-h1"
    assert captured["verdict"] == "validated"
    assert "n_clusters=7" in captured["summary"]
    assert captured["artifacts"] == [str(out)]  # the run dir is the artifact reference


def test_run_packet_run_failure_does_not_record(monkeypatch):
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def fake_arun(skill, **kw):
        return types.SimpleNamespace(
            success=False, exit_code=1, output_dir=None, files=[], stderr="kernel died"
        )

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    monkeypatch.setattr(outbox, "validate_input_path", lambda p, allow_dir=False: Path(p))

    recorded = {"called": False}
    monkeypatch.setattr(
        outbox, "_record_packet_result", lambda *a, **k: recorded.__setitem__("called", True)
    )

    res = _run(outbox.run_packet(object(), object(), "t1", "p1", input_path="/data/x.h5ad"))
    assert res["status"] == "run_failed"
    assert recorded["called"] is False  # a failed run is not recorded as evidence


def test_run_packet_unresolved_skill_errors(monkeypatch):
    monkeypatch.setattr(
        outbox, "_load_packet", lambda cfg, pid: _packet(kind="file_drop", skill_name=None)
    )

    async def fake_arun(skill, **kw):
        raise AssertionError("must not run a packet with no skill")

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    res = _run(outbox.run_packet(object(), object(), "t1", "p1", input_path="/data/x.h5ad"))
    assert res["status"] == "error" and "skill" in res["error"]


def test_run_packet_no_input_errors(monkeypatch):
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def fake_arun(skill, **kw):
        raise AssertionError("must not run without an input")

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    res = _run(outbox.run_packet(None, object(), "", "p1", input_path=None))
    assert res["status"] == "error" and "input" in res["error"]


def test_run_packet_rejects_refined_verdict():
    res = _run(
        outbox.run_packet(object(), object(), "t1", "p1", input_path="/data/x.h5ad", verdict="refined")
    )
    assert res["status"] == "error" and "refined" in res["error"].lower()


def test_run_packet_rejects_unsafe_packet_id(monkeypatch):
    def must_not_load(*a, **k):
        raise AssertionError("an unsafe packet_id must be rejected before touching the FS")

    monkeypatch.setattr(outbox, "_load_packet", must_not_load)
    res = _run(
        outbox.run_packet(object(), object(), "t1", "../../etc/passwd", input_path="/data/x.h5ad")
    )
    assert res["status"] == "error" and "packet_id" in res["error"]


def test_run_packet_skill_exception_is_run_failed(monkeypatch):
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def boom_arun(skill, **kw):
        raise RuntimeError("kernel exploded")

    monkeypatch.setattr(outbox, "arun_skill", boom_arun)
    monkeypatch.setattr(outbox, "validate_input_path", lambda p, allow_dir=False: Path(p))
    recorded = {"called": False}
    monkeypatch.setattr(
        outbox, "_record_packet_result", lambda *a, **k: recorded.__setitem__("called", True)
    )

    res = _run(outbox.run_packet(object(), object(), "t1", "p1", input_path="/data/x.h5ad"))
    assert res["status"] == "run_failed" and "kernel exploded" in res["error"]
    assert recorded["called"] is False


def test_run_packet_resolves_thread_dataset_when_no_input(monkeypatch, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "result.json").write_text(json.dumps({"summary": {}}))
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def fake_resolve(mc, tid):
        assert tid == "t1"
        return "/data/thread_ds.h5ad"

    monkeypatch.setattr(outbox, "_resolve_thread_dataset_path", fake_resolve)
    monkeypatch.setattr(outbox, "validate_input_path", lambda p, allow_dir=False: Path(p))

    seen = {}

    async def fake_arun(skill, **kw):
        seen["input"] = kw["input_path"]
        return _ok_result(out, [])

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    monkeypatch.setattr(outbox, "_record_packet_result", lambda *a, **k: {"status": "recorded"})

    res = _run(outbox.run_packet(object(), object(), "t1", "p1", verdict="inconclusive"))
    assert res["status"] == "recorded"
    assert seen["input"].endswith("thread_ds.h5ad")  # resolved from thread memory


def test_run_packet_captures_thread_analysis(monkeypatch, tmp_path):
    """E-(2→3) / codex must-fix: a successful run is also recorded as a thread-scoped
    AnalysisMemory so it surfaces in the Analyze panel (analysis://<thread_id>/*)."""
    out = tmp_path / "run"
    out.mkdir()
    (out / "result.json").write_text(json.dumps({"skill": "spatial-domains", "data": {"method": "leiden"}, "summary": {}}))
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def fake_arun(skill, **kw):
        return _ok_result(out, [])

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    monkeypatch.setattr(outbox, "validate_input_path", lambda p, allow_dir=False: Path(p))
    monkeypatch.setattr(outbox, "_record_packet_result", lambda *a, **k: {"status": "recorded"})

    remembered = {}

    class FakeClient:
        async def remember(self, uri, content, disclosure=""):
            remembered["uri"] = uri
            remembered["content"] = content

    res = _run(outbox.run_packet(FakeClient(), object(), "t1", "hof-x-h1", input_path="/data/x.h5ad"))
    assert res["status"] == "recorded"
    assert remembered["uri"].startswith("analysis://t1/spatial-domains/")
    payload = json.loads(remembered["content"])
    assert payload["thread_id"] == "t1"
    assert payload["skill"] == "spatial-domains"
    assert payload["method"] == "leiden"
    assert payload["status"] == "completed"
    assert payload["output_path"] == str(out)


def test_run_packet_skips_thread_analysis_without_thread(monkeypatch, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    monkeypatch.setattr(outbox, "_load_packet", lambda cfg, pid: _packet())

    async def fake_arun(skill, **kw):
        return _ok_result(out, [])

    monkeypatch.setattr(outbox, "arun_skill", fake_arun)
    monkeypatch.setattr(outbox, "validate_input_path", lambda p, allow_dir=False: Path(p))
    monkeypatch.setattr(outbox, "_record_packet_result", lambda *a, **k: {"status": "recorded"})

    calls = {"n": 0}

    class FakeClient:
        async def remember(self, *a, **k):
            calls["n"] += 1

    # No thread_id → no thread-scoped analysis capture (input_path supplied explicitly).
    res = _run(outbox.run_packet(FakeClient(), object(), "", "hof-x-h1", input_path="/data/x.h5ad"))
    assert res["status"] == "recorded"
    assert calls["n"] == 0
