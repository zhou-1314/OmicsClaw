"""Adaptive env provisioning: the runtime resolver (probe + on-branch).

Verifies mode gating (default is ON), the subprocess importability probe, the
on-mode overlay provisioning orchestration, and — critically — that the EXPLICIT
``off``/``skip`` path is byte-for-byte behavior-neutral (base interpreter, empty
overlay, no probe spawned).
"""

from __future__ import annotations

import contextlib
import os
import sys

import pytest

import omicsclaw.skill.execution.venv_provision as vpmod
from omicsclaw.skill.execution import env_resolver
from omicsclaw.skill.execution.env_resolver import SkillRuntime, resolve_skill_runtime


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_ADAPTIVE_ENV", raising=False)
    monkeypatch.delenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", raising=False)


# --------------------------------------------------------------------------- #
# mode resolution                                                             #
# --------------------------------------------------------------------------- #


def test_mode_defaults_on(monkeypatch):
    # Phase 2 ships default-on (user-confirmed).
    assert env_resolver._mode() == "on"


@pytest.mark.parametrize("value,expected", [
    ("probe", "probe"),
    ("on", "on"),
    ("1", "on"),
    ("true", "on"),
    ("off", "off"),
    ("0", "off"),
    ("false", "off"),
    ("", "on"),       # unset/empty -> default on
    ("anything", "on"),
])
def test_mode_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", value)
    assert env_resolver._mode() == expected


def test_skip_override_forces_off(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", "1")
    assert env_resolver._mode() == "off"


def test_skip_kill_switch_yields_skip_provenance(monkeypatch):
    """Kill-switch returns base interpreter but a distinct source='skip'."""
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", "1")
    monkeypatch.setattr(env_resolver, "_probe_missing",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("skip must not probe")))
    rt = resolve_skill_runtime({"requires": ["scanpy"], "alias": "x"}, base_python=sys.executable, base_env={})
    assert rt.source == "skip" and rt.python == sys.executable and rt.env_overlay == {}


# --------------------------------------------------------------------------- #
# default-off behavior neutrality                                            #
# --------------------------------------------------------------------------- #


def test_explicit_off_returns_base_and_never_probes(monkeypatch):
    """OMICSCLAW_ADAPTIVE_ENV=off is the byte-for-byte legacy path: no probe."""
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "off")

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("probe must not run when adaptive env is off")

    monkeypatch.setattr(env_resolver, "_probe_missing", _boom)
    rt = resolve_skill_runtime(
        {"requires": ["scanpy", "numpy"], "alias": "x"},
        base_python=sys.executable,
        base_env={},
    )
    assert rt == SkillRuntime(python=sys.executable, source="base")
    assert rt.env_overlay == {}


def test_no_requires_returns_base_without_probe(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "probe")
    monkeypatch.setattr(
        env_resolver, "_probe_missing",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe empty requires")),
    )
    rt = resolve_skill_runtime({"requires": [], "alias": "x"}, base_python=sys.executable, base_env={})
    assert rt.source == "base"


# --------------------------------------------------------------------------- #
# real subprocess probe                                                       #
# --------------------------------------------------------------------------- #


def test_probe_missing_real_subprocess_detects_present_and_absent():
    missing = env_resolver._probe_missing(
        sys.executable,
        ["os", "sys", "json", "definitely_not_a_real_module_xyz"],
        env=dict(__import__("os").environ),
    )
    assert missing == ["definitely_not_a_real_module_xyz"]


def test_probe_missing_empty_list_short_circuits():
    assert env_resolver._probe_missing(sys.executable, [], env={}) == []


def test_resolver_passes_cwd_to_probe(monkeypatch):
    """cwd (script dir) must reach the probe so it matches the real run."""
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "probe")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["numpy"])
    seen = {}

    def _fake_probe(python, names, env, *, cwd=None, **k):
        seen["cwd"] = cwd
        return []

    monkeypatch.setattr(env_resolver, "_probe_missing", _fake_probe)
    resolve_skill_runtime({"requires": ["numpy"], "alias": "x"},
                          base_python=sys.executable, base_env={}, cwd="/some/skill/dir")
    assert seen["cwd"] == "/some/skill/dir"


# --------------------------------------------------------------------------- #
# probe mode: logs + reports, but stays in base env (Phase 1)                 #
# --------------------------------------------------------------------------- #


def test_probe_mode_reports_missing_but_runs_base(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "probe")
    # scanpy present, infercnvpy missing, torch (conda) missing.
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages",
                        lambda info: ["scanpy", "infercnvpy", "torch"])
    monkeypatch.setattr(env_resolver, "_probe_missing", lambda py, names, env, **k: ["infercnvpy", "torch"])

    rt = resolve_skill_runtime({"requires": ["scanpy", "infercnvpy", "torch"], "alias": "spatial-cnv"},
                               base_python=sys.executable, base_env={})
    # Phase 1: still base interpreter, empty overlay — observation only.
    assert rt.python == sys.executable
    assert rt.env_overlay == {}
    assert rt.source == "probe"
    assert "missing:infercnvpy" in rt.notes and "missing:torch" in rt.notes


def test_probe_mode_all_present_returns_base(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "probe")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["numpy"])
    monkeypatch.setattr(env_resolver, "_probe_missing", lambda *a, **k: [])
    rt = resolve_skill_runtime({"requires": ["numpy"], "alias": "x"}, base_python=sys.executable, base_env={})
    assert rt.source == "base" and rt.env_overlay == {}


def test_probe_inconclusive_falls_back_to_base(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "probe")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["numpy"])
    monkeypatch.setattr(env_resolver, "_probe_missing", lambda *a, **k: None)  # inconclusive
    rt = resolve_skill_runtime({"requires": ["numpy"], "alias": "x"}, base_python=sys.executable, base_env={})
    assert rt.source == "base"


# --------------------------------------------------------------------------- #
# Phase 2: on-mode provisioning orchestration (venv_provision mocked)          #
# --------------------------------------------------------------------------- #


def _stub_vp(monkeypatch, tmp_path, *, valid=False, fp_match=False,
            ensure=True, install=True, base_missing=None, venv_missing=None):
    """Patch venv_provision for deterministic on-mode flow tests."""
    venv = tmp_path / "key" / ".venv"
    monkeypatch.setattr(vpmod, "key_dir", lambda bp, specs: tmp_path / "key")
    monkeypatch.setattr(vpmod, "venv_dir", lambda bp, specs: venv)
    monkeypatch.setattr(vpmod, "fingerprint", lambda bp, specs: "fp")

    @contextlib.contextmanager
    def _lock(key_root, **k):
        yield True

    monkeypatch.setattr(vpmod, "venv_lock", _lock)
    monkeypatch.setattr(vpmod, "venv_looks_valid", lambda v: valid)
    monkeypatch.setattr(vpmod, "fingerprint_matches", lambda v, fp: fp_match)
    monkeypatch.setattr(vpmod, "ensure_overlay_venv", lambda v, bp, **k: ensure)
    calls = {"installed": None}
    monkeypatch.setattr(vpmod, "install_into_venv",
                        lambda v, specs, **k: (calls.__setitem__("installed", specs), install)[1])
    monkeypatch.setattr(vpmod, "write_fingerprint", lambda v, fp: None)
    monkeypatch.setattr(vpmod, "write_meta", lambda *a, **k: None)
    monkeypatch.setattr(vpmod, "overlay_env",
                        lambda v, p: {"VIRTUAL_ENV": str(v), "PATH": f"{v}/bin:{p}"})
    monkeypatch.setattr(vpmod, "venv_python", lambda v: v / "bin" / "python")

    # base probe (py == base interpreter) reports base_missing; the post-install
    # verify probe (py == overlay venv python) reports venv_missing.
    def _probe(py, names, env, **k):
        return list(base_missing or []) if str(py) == sys.executable else list(venv_missing or [])

    monkeypatch.setattr(env_resolver, "_probe_missing", _probe)
    return venv, calls


def test_on_mode_provisions_overlay_for_pip_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["infercnvpy"])
    venv, calls = _stub_vp(monkeypatch, tmp_path, base_missing=["infercnvpy"], venv_missing=[])

    rt = resolve_skill_runtime({"requires": ["infercnvpy"], "alias": "spatial-cnv"},
                               base_python=sys.executable, base_env={"PATH": "/usr/bin"})
    assert rt.python == str(venv / "bin" / "python")
    assert rt.env_overlay["VIRTUAL_ENV"] == str(venv)
    assert rt.source.startswith("venv:")
    assert calls["installed"] == ["infercnvpy>=0.4.0"]


def test_on_mode_fast_reuse_skips_install(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["infercnvpy"])
    venv, calls = _stub_vp(monkeypatch, tmp_path, valid=True, fp_match=True, base_missing=["infercnvpy"])

    rt = resolve_skill_runtime({"requires": ["infercnvpy"], "alias": "x"},
                               base_python=sys.executable, base_env={"PATH": "/usr/bin"})
    assert rt.python == str(venv / "bin" / "python")
    assert calls["installed"] is None  # reused: no install


def test_on_mode_mixed_deferred_logs_hint(monkeypatch, tmp_path, caplog):
    """Mixed pip + deferred misses: provision the pip leaf AND warn about the heavy one."""
    import logging
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["infercnvpy", "torch"])
    venv, calls = _stub_vp(monkeypatch, tmp_path, base_missing=["infercnvpy", "torch"], venv_missing=[])
    with caplog.at_level(logging.WARNING, logger="omicsclaw.skill.execution.env_resolver"):
        rt = resolve_skill_runtime({"requires": ["infercnvpy", "torch"], "alias": "spatial-cnv"},
                                   base_python=sys.executable, base_env={"PATH": "/usr/bin"})
    # pip leaf provisioned...
    assert rt.source.startswith("venv:")
    assert calls["installed"] == ["infercnvpy>=0.4.0"]
    # ...and the deferred heavy dep got the conda hint.
    assert any("torch" in r.message and "0_setup_env" in r.message for r in caplog.records)


def test_on_mode_deferred_only_falls_back_to_base(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["torch"])
    _stub_vp(monkeypatch, tmp_path, base_missing=["torch"])  # torch is conda-deferred

    rt = resolve_skill_runtime({"requires": ["torch"], "alias": "x"},
                               base_python=sys.executable, base_env={"PATH": "/usr/bin"})
    assert rt.python == sys.executable and rt.env_overlay == {}
    assert any(n.startswith("deferred:torch") for n in rt.notes)


def test_on_mode_provision_failure_falls_back_to_base(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["infercnvpy"])
    _stub_vp(monkeypatch, tmp_path, ensure=False, base_missing=["infercnvpy"])  # create fails

    rt = resolve_skill_runtime({"requires": ["infercnvpy"], "alias": "x"},
                               base_python=sys.executable, base_env={"PATH": "/usr/bin"})
    assert rt.python == sys.executable and rt.env_overlay == {}
    assert "provision-failed" in rt.notes


def test_on_mode_provision_setup_exception_falls_back_to_base(monkeypatch, tmp_path):
    """A provisioning exception (even in key/fingerprint setup) must NOT propagate —
    it degrades to the base env (Codex final review: whole body wrapped)."""
    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["infercnvpy"])
    monkeypatch.setattr(
        env_resolver, "_probe_missing",
        lambda py, names, env, **k: ["infercnvpy"] if str(py) == sys.executable else [],
    )
    # key_dir() raising simulates a setup-time failure that used to be outside the try.
    monkeypatch.setattr(vpmod, "key_dir",
                        lambda bp, specs: (_ for _ in ()).throw(RuntimeError("boom")))
    rt = resolve_skill_runtime({"requires": ["infercnvpy"], "alias": "x"},
                               base_python=sys.executable, base_env={"PATH": "/usr/bin"})
    assert rt.python == sys.executable and rt.env_overlay == {}
    assert "provision-failed" in rt.notes


@pytest.mark.skipif(
    os.getenv("OMICSCLAW_TEST_NETWORK", "") not in {"1", "true", "yes"},
    reason="network E2E; set OMICSCLAW_TEST_NETWORK=1 to run",
)
def test_on_mode_real_end_to_end_provisions_and_runs(monkeypatch, tmp_path):
    """Full path: resolver provisions a real overlay for a missing pip leaf, and the
    returned interpreter+overlay can import it."""
    import os as _os
    import subprocess as _sp

    monkeypatch.setenv("OMICSCLAW_ADAPTIVE_ENV", "on")
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    # `cowsay` is a tiny pure-python leaf absent from the base env.
    monkeypatch.setattr(env_resolver.dep_spec, "required_packages", lambda info: ["cowsay"])
    monkeypatch.setattr(env_resolver.dep_spec, "pip_spec_for", lambda pkg: "cowsay")

    base_env = dict(_os.environ)
    rt = resolve_skill_runtime({"requires": ["cowsay"], "alias": "demo"},
                               base_python=sys.executable, base_env=base_env)
    assert rt.source.startswith("venv:"), rt
    assert rt.env_overlay.get("VIRTUAL_ENV")
    run_env = {**base_env, **rt.env_overlay}
    out = _sp.run([rt.python, "-c", "import cowsay; print('ok')"],
                  env=run_env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr
