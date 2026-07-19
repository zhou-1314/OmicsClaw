"""Overlay-venv provisioning for adaptive env resolution (Phase 2).

Creates and reuses content-addressed ``--system-site-packages`` overlay venvs that
sit on top of the base interpreter (conda env or bare python). The overlay only
ever *adds* the missing pip-installable leaves the base lacks; everything already
in the base imports for free. All operations are best-effort and non-fatal — any
failure degrades to "run in the base env" (the caller's responsibility).

Design (``docs/proposals/adaptive-environment-provisioning.md`` §5):
  * key = content hash of (base interpreter identity, platform, sorted pip specs),
    so identical overlays are shared and a base/spec change gets a fresh dir.
  * create with ``uv venv --python <base> --system-site-packages`` (Codex: create
    from the TARGET interpreter, not the dispatcher), falling back to
    ``<base> -m venv --system-site-packages`` on uv-less hosts.
  * a sha256 fingerprint file inside the venv records "fully provisioned for this
    basis"; a match short-circuits re-install (fast reuse).
  * a per-key lock file serializes concurrent create+install (TOCTOU), with
    stale-lock recovery so a crashed run never wedges the cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from .environment import scrub_internal_control_credentials

logger = logging.getLogger(__name__)

# Our overlay dirs are named by a 16-hex content key; used to gate destructive
# cache operations so a misconfigured OMICSCLAW_ENV_DIR cannot match user dirs.
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")

_FINGERPRINT_FILE = ".omicsclaw.fingerprint"
_META_FILE = ".meta.json"
_LOCK_FILE = ".lock"
_VENV_DIRNAME = ".venv"

_CREATE_TIMEOUT = 300.0
_INSTALL_TIMEOUT = 1800.0
# Bounded wait for a concurrent provision of the SAME overlay. flock is released
# automatically when the holder dies, so there is no stale-lock to "steal"; if we
# cannot acquire within the window we degrade to the base env (non-fatal).
_LOCK_TIMEOUT = 300.0


# --------------------------------------------------------------------------- #
# Cache root + content-addressed keys                                          #
# --------------------------------------------------------------------------- #


def env_root() -> Path:
    """Managed overlay-venv cache root.

    ``$OMICSCLAW_ENV_DIR`` wins; else ``$XDG_CACHE_HOME/omicsclaw/envs``; else
    ``~/.cache/omicsclaw/envs``. Never the repo or a per-run output dir.
    """
    override = os.getenv("OMICSCLAW_ENV_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.getenv("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "omicsclaw" / "envs"


def _resolver_tag() -> str:
    return "uv" if shutil.which("uv") else "stdlib"


def _interp_mtime(base_python: str) -> str:
    """Best-effort mtime of the base interpreter binary — cache-busts a same-path
    rebuild (e.g. a conda env recreated in place at a different Python version)."""
    try:
        return str(int(Path(base_python).resolve().stat().st_mtime))
    except OSError:
        return "0"


@lru_cache(maxsize=None)
def _interp_identity(base_python: str) -> str:
    """``version|prefix`` of the base interpreter, for ANY base (cached).

    For the running interpreter this is free (``sys``); for an
    ``OMICSCLAW_RUN_PYTHON`` override it costs one cached subprocess. Folding the
    version + prefix into the fingerprint means a Python-version bump or a conda-env
    switch at the same binary path busts the cache (Codex final review).
    """
    try:
        real = str(Path(base_python).resolve())
        if real == str(Path(sys.executable).resolve()):
            return f"{sys.version}|{sys.prefix}"
        proc = subprocess.run(
            [base_python, "-c", "import sys;print(sys.version);print(sys.prefix)"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=scrub_internal_control_credentials(os.environ),
        )
        if proc.returncode == 0:
            return proc.stdout.strip().replace("\n", "|")
    except Exception:  # pragma: no cover - defensive
        pass
    return ""


def _basis(base_python: str, pip_specs: list[str]) -> str:
    """Stable identity string for an overlay (base interpreter + platform + specs).

    Beyond the interpreter path/mtime/platform and the sorted specs, folds in the
    base interpreter's version+prefix and the active conda env, so a base
    Python/conda change at the same binary path forces a fresh overlay rather than
    reusing one whose ``--no-deps`` leaves were built against the old base.
    Note: in-place upgrades of an individual base package (without a Python/env
    change) are NOT captured — that is the inherent trade-off of a system-site
    overlay and is documented as a known limitation.
    """
    real = str(Path(base_python).resolve())
    parts = [
        real,
        _interp_mtime(base_python),
        sys.platform,
        platform.machine(),
        _interp_identity(base_python),
        os.getenv("CONDA_PREFIX", ""),
        *sorted(pip_specs),
    ]
    return "\n".join(parts)


def venv_key(base_python: str, pip_specs: list[str]) -> str:
    """16-hex content key naming the overlay dir for this (base, specs) combo."""
    return hashlib.sha256(_basis(base_python, pip_specs).encode("utf-8")).hexdigest()[:16]


def key_dir(base_python: str, pip_specs: list[str]) -> Path:
    return env_root() / venv_key(base_python, pip_specs)


def venv_dir(base_python: str, pip_specs: list[str]) -> Path:
    return key_dir(base_python, pip_specs) / _VENV_DIRNAME


def fingerprint(base_python: str, pip_specs: list[str]) -> str:
    """Full provisioning fingerprint (basis + resolver tool)."""
    payload = _basis(base_python, pip_specs) + "\n" + _resolver_tag()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Cross-platform venv shape                                                     #
# --------------------------------------------------------------------------- #


def venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def venv_python(venv: Path) -> Path:
    return venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")


def venv_looks_valid(venv: Path) -> bool:
    if not venv.is_dir():
        return False
    return (venv / "bin" / "python").is_file() or (venv / "Scripts" / "python.exe").is_file()


def overlay_env(venv: Path, base_path: str) -> dict[str, str]:
    """Env overlay that points a subprocess at ``venv`` (prepend bin to PATH)."""
    bin_dir = str(venv_bin(venv))
    new_path = bin_dir + (os.pathsep + base_path if base_path else "")
    return {"VIRTUAL_ENV": str(venv), "PATH": new_path}


# --------------------------------------------------------------------------- #
# Fingerprint                                                                   #
# --------------------------------------------------------------------------- #


def fingerprint_matches(venv: Path, fp: str) -> bool:
    path = venv / _FINGERPRINT_FILE
    try:
        return path.is_file() and path.read_text(encoding="utf-8").strip() == fp
    except OSError:
        return False


def write_fingerprint(venv: Path, fp: str) -> None:
    with contextlib.suppress(OSError):
        (venv / _FINGERPRINT_FILE).write_text(fp, encoding="utf-8")


def write_meta(key_root: Path, base_python: str, pip_specs: list[str], timestamp: float) -> None:
    with contextlib.suppress(OSError):
        (key_root / _META_FILE).write_text(
            json.dumps(
                {
                    "base_python": base_python,
                    "pip_specs": sorted(pip_specs),
                    "platform": f"{sys.platform}-{platform.machine()}",
                    "resolver": _resolver_tag(),
                    "created": timestamp,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# Per-key lock (POSIX flock; atomic-create fallback) with stale recovery        #
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def venv_lock(key_root: Path, *, timeout: float = _LOCK_TIMEOUT):
    """Serialize provisioning of one overlay across processes; non-fatal.

    Yields ``True`` when the lock is held, ``False`` if it could not be acquired
    within ``timeout`` (caller then skips provisioning and degrades to base env).
    Uses ``fcntl.flock``, which the kernel releases automatically if the holding
    process dies — so a crashed run never wedges the cache and there is no stale
    lock to recover. On non-POSIX hosts the lock is a best-effort no-op.
    """
    key_root.mkdir(parents=True, exist_ok=True)
    lock_path = key_root / _LOCK_FILE
    try:
        import fcntl  # POSIX
    except ImportError:
        fcntl = None

    handle = None
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        handle = open(lock_path, "a+")  # noqa: SIM115 - released in finally
        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.25)
        else:  # pragma: no cover - non-POSIX best effort
            acquired = True
        yield acquired
    finally:
        if handle is not None:
            if acquired and fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                handle.close()


# --------------------------------------------------------------------------- #
# Create + install                                                              #
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], *, timeout: float, env: dict[str, str] | None = None) -> bool:
    child_env = scrub_internal_control_credentials(os.environ if env is None else env)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("adaptive-env: timed out: %s", " ".join(cmd[:3]))
        return False
    except OSError as exc:
        logger.warning("adaptive-env: exec failed (%s): %s", exc, " ".join(cmd[:3]))
        return False
    if proc.returncode != 0:
        logger.warning(
            "adaptive-env: command failed (exit %s): %s\n%s",
            proc.returncode,
            " ".join(cmd[:4]),
            (proc.stderr or proc.stdout or "").strip()[:600],
        )
        return False
    return True


def ensure_overlay_venv(venv: Path, base_python: str, *, timeout: float = _CREATE_TIMEOUT) -> bool:
    """Create ``venv`` as a ``--system-site-packages`` overlay of ``base_python``.

    Idempotent (returns True if already valid). uv first, stdlib ``venv`` fallback.
    """
    if venv_looks_valid(venv):
        return True
    venv.parent.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv:
        # No ``--seed``: ``--system-site-packages`` makes the base interpreter's pip
        # visible, so ``<venv>/bin/python -m pip install`` (see install_into_venv)
        # works and targets the overlay — without paying the network cost of fetching
        # seed pip/setuptools/wheel on every create. uv venv is then ~instant.
        cmd = [uv, "venv", "--python", base_python, "--system-site-packages", str(venv)]
    else:
        cmd = [base_python, "-m", "venv", "--system-site-packages", str(venv)]
    if not _run(cmd, timeout=timeout):
        return False
    return venv_looks_valid(venv)


def install_into_venv(venv: Path, pip_specs: list[str], *, timeout: float = _INSTALL_TIMEOUT) -> bool:
    """Install ``pip_specs`` into ``venv`` using the venv's OWN pip — ABI-safe.

    Two ABI-safety guards, both essential (verified empirically):

    1. Install with the venv's OWN stdlib pip, never ``uv pip install`` — uv's
       resolver ignores ``--system-site-packages`` and would reinstall base
       ``numpy``/``pandas`` at a newer, ABI-incompatible version into the overlay.
    2. ``--no-deps`` — the overlay is a pure ADDITIVE layer of exactly the missing
       leaves; every transitive dependency is satisfied from the base env via
       system-site. This makes it impossible to shadow a compiled base package
       (numpy/scipy/torch) with a pip-resolved one. A leaf whose dependency is
       genuinely absent from the base simply fails to import at run time, which the
       caller treats non-fatally (Phase 3 may add base-pinned constraints to install
       genuinely-missing pure-python deps too).
    """
    if not pip_specs:
        return True
    py = str(venv_python(venv))
    cmd = [py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
           "--no-deps", *pip_specs]
    return _run(cmd, timeout=timeout)


# --------------------------------------------------------------------------- #
# Cache management (oc env overlays|clean)                                      #
# --------------------------------------------------------------------------- #


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        with contextlib.suppress(OSError):
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
    return total


def _looks_like_overlay(entry: Path) -> bool:
    """A managed overlay: a 16-hex content-key dir carrying our markers.

    Requiring BOTH the 16-hex name AND a ``.meta.json``/``.venv`` marker means a
    misconfigured ``OMICSCLAW_ENV_DIR`` (e.g. pointed at ``$HOME``) cannot match an
    unrelated user directory that merely contains a ``.venv`` (Codex final review).
    """
    if not _KEY_RE.match(entry.name) or not entry.is_dir():
        return False
    return (entry / _META_FILE).exists() or (entry / _VENV_DIRNAME).exists()


def list_overlays() -> list[dict]:
    """Inventory of provisioned overlay venvs under :func:`env_root` for `oc env`."""
    root = env_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not _looks_like_overlay(entry):
            continue
        venv = entry / _VENV_DIRNAME
        meta: dict = {}
        meta_path = entry / _META_FILE
        if meta_path.is_file():
            with contextlib.suppress(Exception):
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
        out.append(
            {
                "key": entry.name,
                "valid": venv_looks_valid(venv),
                "pip_specs": meta.get("pip_specs", []),
                "base_python": meta.get("base_python", ""),
                "created": meta.get("created"),
                "size_bytes": _dir_size(entry),
                "path": str(entry),
            }
        )
    return out


def remove_overlay(key: str) -> bool:
    """Delete one overlay by key. Refuses path traversal, non-16-hex keys, and
    anything that does not look like one of our managed overlays."""
    if not _KEY_RE.match(key or ""):
        return False
    root = env_root().resolve()
    target = (root / key).resolve()
    if target.parent != root or not _looks_like_overlay(target):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def clean_all() -> int:
    """Delete managed overlays under :func:`env_root`; returns the count removed.

    Only removes 16-hex content-key dirs carrying our markers, so a misconfigured
    ``OMICSCLAW_ENV_DIR`` pointed at a populated directory cannot nuke unrelated
    files (Codex final review).
    """
    root = env_root()
    removed = 0
    if not root.is_dir():
        return 0
    for entry in list(root.iterdir()):
        if _looks_like_overlay(entry):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


__all__ = [
    "env_root",
    "list_overlays",
    "remove_overlay",
    "clean_all",
    "venv_key",
    "key_dir",
    "venv_dir",
    "fingerprint",
    "fingerprint_matches",
    "write_fingerprint",
    "write_meta",
    "venv_bin",
    "venv_python",
    "venv_looks_valid",
    "overlay_env",
    "venv_lock",
    "ensure_overlay_venv",
    "install_into_venv",
]
