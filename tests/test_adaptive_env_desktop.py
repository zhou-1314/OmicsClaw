"""Backend surfacing of adaptive env provisioning to the desktop app.

Covers: JobOutcome/Job runtime_source propagation, the /env overlay + adaptive-mode
endpoints, and the resolver status-callback wiring.
"""

from __future__ import annotations

import asyncio
import json
import sys

from omicsclaw.execution.executors.base import JobContext, JobOutcome
from omicsclaw.remote.routers import env as env_router
from omicsclaw.remote.schemas import (
    AdaptiveModeUpdateRequest,
    Job,
    OverlayCleanRequest,
)
from omicsclaw.skill.execution import env_resolver
from omicsclaw.skill.result import build_skill_run_result


# --------------------------------------------------------------------------- #
# provenance: SkillRunResult.runtime_source -> JobOutcome -> Job              #
# --------------------------------------------------------------------------- #


def test_job_outcome_carries_runtime_source_default_base():
    assert JobOutcome(exit_code=0).runtime_source == "base"
    assert JobOutcome(exit_code=0, runtime_source="venv:abc").runtime_source == "venv:abc"


def test_job_schema_has_runtime_source_field():
    j = Job(job_id="1", skill="x", status="succeeded", workspace="/w", inputs={}, params={},
            created_at="t", runtime_source="venv:abc")
    assert j.runtime_source == "venv:abc"
    assert "runtime_source" in j.model_dump()


def test_job_schema_runtime_source_optional_for_legacy_jobs():
    # Old job.json without the field deserializes cleanly to None (no migration).
    j = Job.model_validate({"job_id": "1", "skill": "x", "status": "succeeded",
                            "workspace": "/w", "inputs": {}, "params": {}, "created_at": "t"})
    assert j.runtime_source is None


def test_default_executor_propagates_runtime_source(monkeypatch, tmp_path):
    from omicsclaw.execution.executors.default import SkillRunnerExecutor
    from omicsclaw.skill import runner as skill_runner

    async def _fake_arun(skill, **kwargs):
        return build_skill_run_result(skill=skill, success=True, exit_code=0,
                                      output_dir=str(tmp_path), runtime_source="venv:deadbeef")

    monkeypatch.setattr(skill_runner, "arun_skill", _fake_arun)
    ctx = JobContext(job_id="1", workspace=tmp_path, skill="spatial-cnv", inputs={"demo": True},
                     params={}, artifact_root=tmp_path / "art", stdout_log=tmp_path / "out.log")
    outcome = asyncio.run(SkillRunnerExecutor().run(ctx))
    assert outcome.runtime_source == "venv:deadbeef"


# --------------------------------------------------------------------------- #
# /env adaptive-mode endpoints                                                #
# --------------------------------------------------------------------------- #


def test_get_adaptive_mode_reflects_env(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "probe")
    monkeypatch.delenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", raising=False)
    resp = asyncio.run(env_router.get_adaptive_mode())
    assert resp.mode == "probe" and resp.kill_switch is False


def test_kill_switch_surfaced(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", "1")
    resp = asyncio.run(env_router.get_adaptive_mode())
    assert resp.mode == "off" and resp.kill_switch is True


def test_put_adaptive_mode_sets_env(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", raising=False)
    resp = asyncio.run(env_router.set_adaptive_mode(AdaptiveModeUpdateRequest(mode="off")))
    assert resp.mode == "off"
    assert env_resolver.adaptive_env_mode() == "off"  # took effect immediately


# --------------------------------------------------------------------------- #
# /env overlay list + clean endpoints                                         #
# --------------------------------------------------------------------------- #


def _make_overlay(root, key, specs):
    keydir = root / key
    (keydir / ".venv" / "bin").mkdir(parents=True)
    (keydir / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (keydir / ".meta.json").write_text(json.dumps({"pip_specs": specs, "base_python": "/p"}))


def test_env_overlays_endpoint_lists(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_overlay(tmp_path / "envs", "a" * 16, ["infercnvpy>=0.4.0"])
    resp = asyncio.run(env_router.env_overlays())
    assert resp.total == 1 and resp.overlays[0].key == "a" * 16
    assert resp.overlays[0].pip_specs == ["infercnvpy>=0.4.0"]
    assert resp.env_root.endswith("envs")


def test_env_clean_endpoint_all_and_by_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_overlay(tmp_path / "envs", "a" * 16, ["x"])
    _make_overlay(tmp_path / "envs", "b" * 16, ["y"])
    # remove one by key
    r1 = asyncio.run(env_router.env_clean(OverlayCleanRequest(key="a" * 16)))
    assert r1.removed == 1 and r1.key == "a" * 16
    # clean the rest
    r2 = asyncio.run(env_router.env_clean(OverlayCleanRequest()))
    assert r2.removed == 1


# --------------------------------------------------------------------------- #
# status callback through the resolver                                        #
# --------------------------------------------------------------------------- #


def test_status_callback_fires_during_on_mode_provisioning(monkeypatch, tmp_path):
    import contextlib

    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.delenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", raising=False)
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["infercnvpy"])
    monkeypatch.setattr(env_resolver, "_probe_missing",
                        lambda py, names, env, **k: ["infercnvpy"] if str(py) == sys.executable else [])

    import omicsclaw.skill.execution.venv_provision as vp
    monkeypatch.setattr(vp, "key_dir", lambda bp, s: tmp_path / "k")
    monkeypatch.setattr(vp, "venv_dir", lambda bp, s: tmp_path / "k" / ".venv")
    monkeypatch.setattr(vp, "fingerprint", lambda bp, s: "fp")

    @contextlib.contextmanager
    def _lock(kr, **k):
        yield True

    monkeypatch.setattr(vp, "venv_lock", _lock)
    monkeypatch.setattr(vp, "venv_looks_valid", lambda v: False)
    monkeypatch.setattr(vp, "fingerprint_matches", lambda v, fp: False)
    monkeypatch.setattr(vp, "ensure_overlay_venv", lambda v, bp, **k: True)
    monkeypatch.setattr(vp, "install_into_venv", lambda v, s, **k: True)
    monkeypatch.setattr(vp, "write_fingerprint", lambda v, fp: None)
    monkeypatch.setattr(vp, "write_meta", lambda *a, **k: None)
    monkeypatch.setattr(vp, "overlay_env", lambda v, p: {"VIRTUAL_ENV": str(v), "PATH": "x"})
    monkeypatch.setattr(vp, "venv_python", lambda v: v / "bin" / "python")

    msgs: list[str] = []
    rt = env_resolver.resolve_skill_runtime(
        {"requires": ["infercnvpy"], "alias": "spatial-cnv"},
        base_python=sys.executable, base_env={"PATH": "/usr/bin"}, status_cb=msgs.append,
    )
    assert rt.source.startswith("venv:")
    joined = " | ".join(msgs)
    assert "Preparing environment" in joined
    assert "Installing" in joined and "infercnvpy" in joined
    assert "Environment ready" in joined
