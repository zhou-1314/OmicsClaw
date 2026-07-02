"""Adaptive skill-runtime resolver — interpreter + env overlay selection.

Implements ``docs/proposals/adaptive-environment-provisioning.md``.

:func:`resolve_skill_runtime` is the single seam ``runner._prepare_skill_run``
calls to decide which Python interpreter runs a skill and what environment overlay
(``VIRTUAL_ENV`` + ``PATH``) to merge. Modes via ``OMICSCLAW_ADAPTIVE_ENV``:

  * ``on`` (DEFAULT) → probe the skill's reconciled ``requires:`` surface; if a
    pip-installable leaf is missing, create/reuse a content-addressed overlay venv
    and run there (``venv_provision``). Fully non-fatal — any failure degrades to
    the base interpreter. Heavy/conda/R deps are deferred with a hint, never
    pip-solved.
  * ``probe`` → probe + LOG the missing set (pip-installable vs deferred), but
    always run in the base env (observability only).
  * ``off`` / ``0`` / ``false`` → return the base interpreter with an empty
    overlay: byte-for-byte the legacy path, no probe spawned.
  * ``OMICSCLAW_SKIP_ADAPTIVE_ENV=1`` → hard kill-switch, forces ``off``.

The probe is a subprocess ``importlib.util.find_spec`` check run with the EXACT
env the skill will use (``PYTHONNOUSERSITE`` etc.), because an in-process
``find_spec`` in the dispatcher can disagree with the child (Codex review).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from . import dep_spec

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}

# Optional human-readable progress sink for surfaces (e.g. the desktop chat
# routes these into the live skill-log stream). Never lets a callback error break
# a run — the module's non-fatal contract.
StatusCb = Callable[[str], None]


def _status(status_cb: StatusCb | None, message: str) -> None:
    if status_cb is None:
        return
    try:
        status_cb(message)
    except Exception:  # pragma: no cover - status reporting must never break a run
        pass

# A tiny, dependency-free probe: print the JSON list of import names whose
# spec cannot be found. ``find_spec`` may raise for broken/namespace packages —
# treat that as "present" so uncertainty never *triggers* provisioning (the
# skill's own try/except import remains the backstop).
_PROBE_CODE = (
    "import importlib.util,sys,json\n"
    "names=json.loads(sys.argv[1])\n"
    "def _miss(n):\n"
    "    try:\n"
    "        return importlib.util.find_spec(n) is None\n"
    "    except Exception:\n"
    "        return False\n"
    "print(json.dumps([n for n in names if _miss(n)]))\n"
)


@dataclass(frozen=True)
class SkillRuntime:
    """Resolved runtime for one skill invocation.

    ``python`` is the interpreter to run; ``env_overlay`` is merged onto the
    subprocess env (empty unless a venv is selected); ``source`` is a provenance
    tag (``base`` | ``skip`` | ``probe`` | ``venv:<key>``); ``notes`` carry
    human-readable detail for logging / Phase 3 provenance.
    """

    python: str
    env_overlay: dict[str, str] = field(default_factory=dict)
    source: str = "base"
    notes: tuple[str, ...] = ()


def _mode() -> str:
    """Resolve the active adaptive-env mode: ``off`` | ``probe`` | ``on``.

    Default is ``on`` (auto-provision) — only ever acts when a dep is missing, is
    fully non-fatal, and is disabled by ``OMICSCLAW_SKIP_ADAPTIVE_ENV=1`` or
    ``OMICSCLAW_ADAPTIVE_ENV=off``.
    """
    if os.getenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", "").strip().lower() in _TRUE:
        return "off"
    raw = os.getenv("OMICSCLAW_ADAPTIVE_ENV", "").strip().lower()
    if raw in {"off", "0", "false", "no"}:
        return "off"
    if raw == "probe":
        return "probe"
    return "on"


def adaptive_env_mode() -> str:
    """Public accessor for the active adaptive-env mode (``off`` | ``probe`` | ``on``).

    Lets surfaces (e.g. the desktop ``/env/adaptive-mode`` endpoint) read the mode
    without importing the private ``_mode``.
    """
    return _mode()


def _probe_missing(
    python_exe: str,
    import_names: list[str],
    env: dict[str, str],
    *,
    cwd: str | None = None,
    timeout: float = 60.0,
) -> list[str] | None:
    """Return import names not importable by ``python_exe`` under ``env``.

    ``cwd`` should match the real skill run's working directory (the script's
    parent) so that any local-module shadowing is reflected. ``None`` signals an
    inconclusive probe (subprocess failed / unparseable) — the caller treats that
    as "run in base env", never as "everything missing".
    """
    if not import_names:
        return []
    payload = json.dumps(sorted(set(import_names)))
    try:
        proc = subprocess.run(
            [python_exe, "-c", _PROBE_CODE, payload],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("adaptive-env: probe subprocess error: %s", exc)
        return None
    if proc.returncode != 0:
        logger.debug(
            "adaptive-env: probe exited %s: %s",
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return None
    try:
        result = json.loads((proc.stdout or "").strip() or "[]")
    except (ValueError, TypeError):
        return None
    return [str(name) for name in result] if isinstance(result, list) else None


def resolve_skill_runtime(
    skill_info: dict,
    *,
    method: str | None = None,
    base_python: str,
    base_env: dict[str, str] | None = None,
    cwd: str | None = None,
    status_cb: StatusCb | None = None,
) -> SkillRuntime:
    """Decide the interpreter + env overlay for one skill run.

    ``cwd`` should be the real run's working dir (the script's parent) so the probe
    matches it. ``status_cb`` (optional) receives short human-readable progress
    messages ("Preparing environment: installing …") that surfaces can stream to
    the user; it is best-effort and never breaks a run. ``method`` is accepted for
    forward-compat (the resolver is scoped to the Python ``requires:`` surface, so
    method-level R/CLI gating is handled elsewhere — see ``dep_spec.runtime_kind``).
    """
    # Explicit kill-switch: distinguishable from plain "off" for provenance.
    if os.getenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", "").strip().lower() in _TRUE:
        return SkillRuntime(python=base_python, source="skip")

    base = SkillRuntime(python=base_python, source="base")
    mode = _mode()
    if mode == "off":
        return base

    packages = dep_spec.required_packages(skill_info)
    if not packages:
        return base

    import_names = [dep_spec.import_name_for(pkg) for pkg in packages]
    env = base_env if base_env is not None else os.environ.copy()
    missing_imports = _probe_missing(base_python, import_names, env, cwd=cwd)

    alias = skill_info.get("alias") or skill_info.get("canonical_name") or "skill"

    if missing_imports is None:
        logger.debug("adaptive-env[%s] %s: probe inconclusive; base env", mode, alias)
        return base
    if not missing_imports:
        logger.debug("adaptive-env[%s] %s: all deps present; base env", mode, alias)
        return base

    # Map the missing import names back to canonical package names, then split
    # into pip-installable vs deferred (conda/R/deny).
    import_to_pkg = {dep_spec.import_name_for(pkg): pkg for pkg in packages}
    missing_pkgs = [import_to_pkg.get(name, name) for name in missing_imports]
    pip_specs, deferred = dep_spec.partition_missing(missing_pkgs)

    logger.info(
        "adaptive-env[%s] %s: missing=%s | pip-installable=%s | deferred=%s",
        mode,
        alias,
        missing_pkgs,
        pip_specs,
        deferred,
    )

    if mode == "probe":
        return SkillRuntime(
            python=base_python,
            source="probe",
            notes=tuple(f"missing:{pkg}" for pkg in missing_pkgs),
        )

    _status(status_cb, f"Checking environment — missing: {', '.join(missing_pkgs)}")

    # mode == "on": provision an overlay venv for the pip-installable misses.
    if not pip_specs:
        # Everything missing is conda-preferred / R / deny — a pip overlay can't
        # help. Degrade to base env with a clear, actionable hint (never a doomed
        # pip mega-solve).
        logger.warning(
            "adaptive-env[on] %s: missing deps %s are not pip-installable here; "
            "run `bash 0_setup_env.sh` (conda) — falling back to base env",
            alias,
            deferred,
        )
        return SkillRuntime(
            python=base_python,
            source="base",
            notes=tuple(f"deferred:{pkg}" for pkg in deferred),
        )

    if deferred:
        # Mixed misses: we provision the pip-installable leaves below, but the
        # heavy/conda/R ones cannot be overlay-installed — surface the same hint as
        # the all-deferred case so the user knows to build the conda env.
        logger.warning(
            "adaptive-env[on] %s: %s are not pip-installable here; "
            "run `bash 0_setup_env.sh` (conda) for those",
            alias,
            deferred,
        )

    provisioned = _provision_overlay(
        base_python=base_python,
        import_names=import_names,
        pip_specs=pip_specs,
        env=env,
        cwd=cwd,
        alias=alias,
        deferred=deferred,
        status_cb=status_cb,
    )
    if provisioned is not None:
        return provisioned

    # Provisioning failed (no uv/venv, install error, lock contention) — non-fatal:
    # run in the base env so the user is no worse off than before this feature.
    return SkillRuntime(
        python=base_python,
        source="base",
        notes=("provision-failed",) + tuple(f"missing:{pkg}" for pkg in missing_pkgs),
    )


def _provision_overlay(
    *,
    base_python: str,
    import_names: list[str],
    pip_specs: list[str],
    env: dict[str, str],
    cwd: str | None,
    alias: str,
    deferred: list[str],
    status_cb: StatusCb | None = None,
) -> SkillRuntime | None:
    """Create/reuse a content-addressed overlay venv and install ``pip_specs``.

    Returns a venv-backed :class:`SkillRuntime`, or ``None`` on any failure (the
    caller then degrades to the base env). The ENTIRE body — including the import,
    key, and fingerprint computation — is inside the ``try`` so no provisioning
    exception can ever propagate into the runner (Codex final review).
    """
    try:
        from . import venv_provision as vp

        key_root = vp.key_dir(base_python, pip_specs)
        venv = vp.venv_dir(base_python, pip_specs)
        fp = vp.fingerprint(base_python, pip_specs)

        with vp.venv_lock(key_root) as locked:
            if not locked:
                logger.warning("adaptive-env[on] %s: lock busy for %s; base env", alias, key_root)
                return None

            # Fast reuse: valid overlay with a matching fingerprint — no probe/install.
            if vp.venv_looks_valid(venv) and vp.fingerprint_matches(venv, fp):
                logger.info("adaptive-env[on] %s: reusing overlay %s", alias, venv)
                _status(status_cb, "Reusing cached environment")
                return _venv_runtime(vp, venv, env, installed=())

            _status(status_cb, "Preparing environment…")
            if not vp.ensure_overlay_venv(venv, base_python):
                logger.warning("adaptive-env[on] %s: overlay venv create failed; base env", alias)
                return None
            _status(status_cb, f"Installing {', '.join(pip_specs)}…")
            if not vp.install_into_venv(venv, pip_specs):
                logger.warning("adaptive-env[on] %s: install %s failed; base env", alias, pip_specs)
                return None

            vp.write_fingerprint(venv, fp)
            vp.write_meta(key_root, base_python, pip_specs, time.time())

            # Verify (warn-only): the freshly installed imports should now resolve
            # inside the overlay. A miss usually means a pip-name/import-name skew;
            # the overlay (system-site-packages) is still a superset of base, so we
            # return it regardless and let the skill's own import guard speak.
            overlay = vp.overlay_env(venv, env.get("PATH", ""))
            verify_env = {**env, **overlay}
            still = _probe_missing(str(vp.venv_python(venv)), import_names, verify_env, cwd=cwd)
            if still:
                logger.warning(
                    "adaptive-env[on] %s: after install, still unresolved in overlay: %s",
                    alias,
                    still,
                )
            logger.info("adaptive-env[on] %s: provisioned %s into %s", alias, pip_specs, venv)
            _status(status_cb, "Environment ready")
            return _venv_runtime(vp, venv, env, installed=pip_specs, deferred=deferred)
    except Exception as exc:  # pragma: no cover - defensive, must stay non-fatal
        logger.warning("adaptive-env[on] %s: provisioning error (%s); base env", alias, exc)
        return None


def _venv_runtime(vp, venv, env, *, installed, deferred=()):
    """Build a venv-backed SkillRuntime with the PATH/VIRTUAL_ENV overlay."""
    overlay = vp.overlay_env(venv, env.get("PATH", ""))
    notes = (f"venv:{venv}",)
    notes += tuple(f"installed:{spec}" for spec in installed)
    notes += tuple(f"deferred:{pkg}" for pkg in deferred)
    return SkillRuntime(
        python=str(vp.venv_python(venv)),
        env_overlay=overlay,
        source=f"venv:{venv.parent.name}",
        notes=notes,
    )


__all__ = ["SkillRuntime", "resolve_skill_runtime", "adaptive_env_mode"]
