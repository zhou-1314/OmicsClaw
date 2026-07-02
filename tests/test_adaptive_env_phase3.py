"""Phase 3 of adaptive env provisioning: provenance, overlay management, whitelist."""

from __future__ import annotations

import json

from omicsclaw.autoagent.constants import SUBPROCESS_ENV_WHITELIST
from omicsclaw.skill.execution import venv_provision as vp
from omicsclaw.skill.result import build_skill_run_result, coerce_skill_run_result


# --------------------------------------------------------------------------- #
# provenance: runtime_source threads through the result model                  #
# --------------------------------------------------------------------------- #


def test_skill_run_result_carries_runtime_source_default_base():
    r = build_skill_run_result(skill="x", success=True, exit_code=0, output_dir=None)
    assert r.runtime_source == "base"


def test_build_result_records_venv_source():
    r = build_skill_run_result(
        skill="spatial-cnv", success=True, exit_code=0, output_dir="/tmp/x",
        runtime_source="venv:abc123",
    )
    assert r.runtime_source == "venv:abc123"


def test_legacy_dict_shape_is_unchanged_by_provenance():
    """Provenance must NOT leak into to_legacy_dict (stable backward-compat shape)."""
    r = build_skill_run_result(skill="x", success=True, exit_code=0,
                               output_dir=None, runtime_source="venv:abc")
    assert "runtime_source" not in r.to_legacy_dict()


def test_coerce_reads_runtime_source_from_rich_mapping():
    back = coerce_skill_run_result(
        {"skill": "x", "success": True, "exit_code": 0, "runtime_source": "venv:deadbeef"}
    )
    assert back.runtime_source == "venv:deadbeef"


def test_coerce_defaults_runtime_source_when_absent():
    # Legacy result dicts without the field still coerce cleanly.
    back = coerce_skill_run_result({"skill": "x", "success": True, "exit_code": 0})
    assert back.runtime_source == "base"


# --------------------------------------------------------------------------- #
# AutoAgent whitelist forwards adaptive-env controls                          #
# --------------------------------------------------------------------------- #


def test_autoagent_whitelist_forwards_adaptive_controls():
    for var in (
        "OMICSCLAW_ADAPTIVE_ENV",
        "OMICSCLAW_SKIP_ADAPTIVE_ENV",
        "OMICSCLAW_ENV_DIR",
        "VIRTUAL_ENV",
        "PATH",
    ):
        assert var in SUBPROCESS_ENV_WHITELIST


# --------------------------------------------------------------------------- #
# overlay cache management (oc env overlays|clean)                            #
# --------------------------------------------------------------------------- #


_KEY_A = "a" * 16  # valid 16-hex content keys
_KEY_B = "b" * 16


def _make_fake_overlay(root, key, specs):
    keydir = root / key
    venv = keydir / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n")
    (keydir / ".meta.json").write_text(json.dumps({"pip_specs": specs, "base_python": "/p"}))
    return keydir


def test_list_overlays_reports_inventory(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_fake_overlay(tmp_path / "envs", _KEY_A, ["infercnvpy>=0.4.0"])
    _make_fake_overlay(tmp_path / "envs", _KEY_B, ["gseapy"])
    overlays = vp.list_overlays()
    keys = {o["key"] for o in overlays}
    assert keys == {_KEY_A, _KEY_B}
    assert all(o["valid"] for o in overlays)
    by_key = {o["key"]: o for o in overlays}
    assert by_key[_KEY_A]["pip_specs"] == ["infercnvpy>=0.4.0"]


def test_list_overlays_ignores_non_overlay_dirs(monkeypatch, tmp_path):
    """list_overlays only reports 16-hex marked dirs, not arbitrary children."""
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_fake_overlay(tmp_path / "envs", _KEY_A, ["x"])
    (tmp_path / "envs" / "my_project").mkdir()  # not 16-hex
    (tmp_path / "envs" / "my_project" / ".venv").mkdir()  # even with a .venv
    assert {o["key"] for o in vp.list_overlays()} == {_KEY_A}


def test_list_overlays_empty_when_no_root(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "nope"))
    assert vp.list_overlays() == []


def test_remove_overlay_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_fake_overlay(tmp_path / "envs", _KEY_A, ["x"])
    assert vp.remove_overlay(_KEY_A) is True
    assert not (tmp_path / "envs" / _KEY_A).exists()
    # path traversal / non-16-hex / missing are all refused
    assert vp.remove_overlay("../../etc") is False
    assert vp.remove_overlay("my_project") is False  # not 16-hex
    assert vp.remove_overlay(_KEY_B) is False  # missing


def test_clean_all_removes_overlays(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_fake_overlay(tmp_path / "envs", _KEY_A, ["x"])
    _make_fake_overlay(tmp_path / "envs", _KEY_B, ["y"])
    assert vp.clean_all() == 2
    assert vp.list_overlays() == []


def test_clean_all_spares_non_overlay_dirs(monkeypatch, tmp_path):
    """A misconfigured OMICSCLAW_ENV_DIR (e.g. $HOME) must not nuke unrelated dirs —
    even ones that themselves contain a .venv — because they are not 16-hex keys."""
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    _make_fake_overlay(tmp_path / "envs", _KEY_A, ["x"])
    proj = tmp_path / "envs" / "my_project"  # a real user project, not 16-hex
    (proj / ".venv" / "bin").mkdir(parents=True)
    (proj / "notes.txt").write_text("keep me")
    assert vp.clean_all() == 1  # only the overlay
    assert (proj / "notes.txt").exists()
    assert (proj / ".venv").exists()


# --------------------------------------------------------------------------- #
# provenance error path + end-to-end threading                                #
# --------------------------------------------------------------------------- #


def test_err_preserves_runtime_source():
    """A venv-backed spawn failure must report the overlay source, not 'base'."""
    from omicsclaw.skill.runner import _err

    r = _err("spatial-cnv", "spawn boom", duration=0.1, runtime_source="venv:abc123")
    assert r.success is False
    assert r.runtime_source == "venv:abc123"


def test_run_skill_threads_runtime_source_end_to_end(monkeypatch, tmp_path):
    """Full _prepare_skill_run -> _finalize_skill_run path records provenance."""
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "off")  # deterministic: base interpreter
    from omicsclaw.skill.runner import run_skill

    result = run_skill("bulkrna-de", demo=True, output_dir=str(tmp_path / "out"))
    assert result.success, result.stderr
    assert result.runtime_source == "base"


def test_run_skill_survives_resolver_exception(monkeypatch, tmp_path):
    """The runner's belt-and-suspenders guard: a resolver blow-up degrades to base,
    the skill still runs (non-fatal guarantee, default-on)."""
    import omicsclaw.skill.runner as runner_mod

    def _boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(runner_mod, "resolve_skill_runtime", _boom)
    result = runner_mod.run_skill("bulkrna-de", demo=True, output_dir=str(tmp_path / "out"))
    assert result.success, result.stderr
    assert result.runtime_source == "base"
