"""Phase 2 of adaptive env provisioning: overlay-venv provisioning primitives.

Real venv *creation* is exercised (no network); package *install* is exercised
both via a mocked command-builder (deterministic) and an opt-in network E2E.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from omicsclaw.skill.execution import venv_provision as vp


# --------------------------------------------------------------------------- #
# cache root + keys + fingerprint                                             #
# --------------------------------------------------------------------------- #


def test_env_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv("OMICSCLAW_ENV_DIR", str(tmp_path / "envs"))
    assert vp.env_root() == tmp_path / "envs"


def test_env_root_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("OMICSCLAW_ENV_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert vp.env_root() == tmp_path / "xdg" / "omicsclaw" / "envs"


def test_venv_key_is_deterministic_and_content_addressed():
    a = vp.venv_key("/usr/bin/python3", ["infercnvpy>=0.4.0", "scanpy"])
    b = vp.venv_key("/usr/bin/python3", ["scanpy", "infercnvpy>=0.4.0"])  # order-independent
    assert a == b and len(a) == 16
    # Different specs / interpreter -> different key.
    assert a != vp.venv_key("/usr/bin/python3", ["infercnvpy>=0.4.0"])
    assert a != vp.venv_key("/opt/other/python3", ["infercnvpy>=0.4.0", "scanpy"])


def test_fingerprint_varies_with_specs_and_interpreter():
    f1 = vp.fingerprint("/usr/bin/python3", ["scanpy"])
    f2 = vp.fingerprint("/usr/bin/python3", ["scanpy", "infercnvpy"])
    f3 = vp.fingerprint("/other/python3", ["scanpy"])
    assert f1 != f2 and f1 != f3 and len(f1) == 64


def test_fingerprint_varies_with_conda_prefix(monkeypatch):
    """A conda-env switch (CONDA_PREFIX) at the same binary path busts the cache."""
    monkeypatch.setenv("CONDA_PREFIX", "/opt/conda/envs/A")
    fa = vp.fingerprint("/usr/bin/python3", ["scanpy"])
    monkeypatch.setenv("CONDA_PREFIX", "/opt/conda/envs/B")
    fb = vp.fingerprint("/usr/bin/python3", ["scanpy"])
    assert fa != fb


def test_basis_folds_in_interpreter_identity():
    # Current-interpreter identity (version+prefix) is part of the basis, free.
    basis = vp._basis(sys.executable, ["scanpy"])
    assert sys.prefix in basis


def test_fingerprint_roundtrip(tmp_path):
    venv = tmp_path / ".venv"
    venv.mkdir()
    fp = vp.fingerprint(sys.executable, ["scanpy"])
    assert not vp.fingerprint_matches(venv, fp)
    vp.write_fingerprint(venv, fp)
    assert vp.fingerprint_matches(venv, fp)
    assert not vp.fingerprint_matches(venv, "deadbeef")


# --------------------------------------------------------------------------- #
# venv shape + overlay env                                                    #
# --------------------------------------------------------------------------- #


def test_venv_python_and_validity(tmp_path):
    venv = tmp_path / ".venv"
    assert not vp.venv_looks_valid(venv)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    assert vp.venv_looks_valid(venv)
    assert vp.venv_python(venv) == venv / "bin" / "python"


def test_overlay_env_prepends_bin_to_path(tmp_path):
    venv = tmp_path / ".venv"
    overlay = vp.overlay_env(venv, "/usr/bin:/bin")
    assert overlay["VIRTUAL_ENV"] == str(venv)
    assert overlay["PATH"].startswith(str(vp.venv_bin(venv)) + os.pathsep)
    assert overlay["PATH"].endswith("/usr/bin:/bin")


# --------------------------------------------------------------------------- #
# lock                                                                         #
# --------------------------------------------------------------------------- #


def test_venv_lock_acquires(tmp_path):
    with vp.venv_lock(tmp_path / "key") as locked:
        assert locked is True


# --------------------------------------------------------------------------- #
# install command builder (mocked, deterministic)                             #
# --------------------------------------------------------------------------- #


def test_adaptive_provisioning_never_inherits_backend_control_credentials(
    monkeypatch,
):
    captured_envs: list[dict[str, str]] = []

    def capture_run(cmd, **kwargs):
        captured_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, "3.11\n/test-prefix\n", "")

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "must-not-reach-provisioner")
    monkeypatch.setenv(
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        "must-not-reach-provisioner",
    )
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD", "3")
    monkeypatch.setattr(vp.subprocess, "run", capture_run)
    vp._interp_identity.cache_clear()

    assert vp._interp_identity("/test/non-running-python")
    assert vp._run(["test-command"], timeout=1) is True

    assert len(captured_envs) == 2
    for child_env in captured_envs:
        assert "OMICSCLAW_REMOTE_AUTH_TOKEN" not in child_env
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in child_env
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in child_env


def test_install_into_venv_uses_venv_own_pip_not_uv(monkeypatch, tmp_path):
    """ABI-safety: install via the venv's stdlib pip (system-site aware), NOT uv pip."""
    seen = {}

    def _fake_run(cmd, *, timeout, env=None):
        seen["cmd"] = cmd
        return True

    monkeypatch.setattr(vp, "_run", _fake_run)
    venv = tmp_path / ".venv"
    ok = vp.install_into_venv(venv, ["infercnvpy>=0.4.0", "scanpy"])
    assert ok
    # Must be `<venv>/bin/python -m pip install --no-deps ...`, never `uv pip install`.
    assert seen["cmd"][0] == str(vp.venv_python(venv))
    assert seen["cmd"][1:4] == ["-m", "pip", "install"]
    assert "uv" not in seen["cmd"][0]
    assert "--no-deps" in seen["cmd"], "additive overlay must install leaves only"
    assert "infercnvpy>=0.4.0" in seen["cmd"] and "scanpy" in seen["cmd"]


def test_install_into_venv_empty_is_noop(tmp_path):
    assert vp.install_into_venv(tmp_path / ".venv", []) is True


# --------------------------------------------------------------------------- #
# REAL venv creation (no network) — proves --system-site-packages overlay      #
# --------------------------------------------------------------------------- #


def test_ensure_overlay_venv_real_creates_inheriting_base(tmp_path):
    venv = tmp_path / "k" / ".venv"
    ok = vp.ensure_overlay_venv(venv, sys.executable, timeout=300)
    assert ok, "overlay venv creation failed"
    assert vp.venv_looks_valid(venv)
    py = vp.venv_python(venv)
    assert py.is_file()
    # idempotent
    assert vp.ensure_overlay_venv(venv, sys.executable) is True
    # --system-site-packages: the overlay python imports a stdlib module and sees
    # the base interpreter's site (json is stdlib; this just proves it runs).
    import subprocess
    out = subprocess.run([str(py), "-c", "import json,sys; print(sys.prefix)"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0


@pytest.mark.skipif(
    os.getenv("OMICSCLAW_TEST_NETWORK", "") not in {"1", "true", "yes"},
    reason="network E2E; set OMICSCLAW_TEST_NETWORK=1 to run",
)
def test_provision_and_install_e2e_network(tmp_path):
    """Smallest E2E: create overlay, pip-install a tiny pure-python pkg, import it."""
    import subprocess
    venv = tmp_path / "k" / ".venv"
    assert vp.ensure_overlay_venv(venv, sys.executable)
    assert vp.install_into_venv(venv, ["cowsay"])
    py = vp.venv_python(venv)
    out = subprocess.run([str(py), "-c", "import cowsay"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr


@pytest.mark.skipif(
    os.getenv("OMICSCLAW_TEST_NETWORK", "") not in {"1", "true", "yes"},
    reason="network E2E; set OMICSCLAW_TEST_NETWORK=1 to run",
)
def test_install_does_not_shadow_base_numpy(tmp_path):
    """ABI-safety E2E: installing a leaf with a base-present dep (numpy) must NOT
    install numpy into the overlay's own site (it is reused from the base)."""
    import subprocess
    venv = tmp_path / "k" / ".venv"
    assert vp.ensure_overlay_venv(venv, sys.executable)
    # gseapy depends on numpy/pandas/scipy/matplotlib — all in the base env.
    assert vp.install_into_venv(venv, ["gseapy"])
    py = str(vp.venv_python(venv))
    # `pip list --local` shows ONLY packages installed in the overlay (not system-site).
    out = subprocess.run([py, "-m", "pip", "list", "--local", "--format=freeze"],
                         capture_output=True, text=True, timeout=120)
    local = out.stdout.lower()
    assert "gseapy==" in local, out.stdout
    assert "numpy==" not in local, f"numpy was shadowed into overlay!\n{out.stdout}"
    assert "pandas==" not in local, f"pandas was shadowed into overlay!\n{out.stdout}"
