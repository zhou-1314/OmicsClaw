"""OS isolation envelope for the Autonomous Code Mini-Agent kernel.

ADR 0032 §4 (and the "AST is only lint" blocker) require the persistent kernel
to run inside a real process/OS boundary, not just static checks. On this
platform the chosen primitive is **bubblewrap** (`bwrap`, an ADR open question
resolved by availability): a user-namespace sandbox that gives us, cheaply and
without root:

* ``--unshare-net`` — no network at all (genetic data never leaves);
* read-only binds for the interpreter, repo, and *declared* inputs only;
* a single writable bind: the autonomous run workspace;
* a key-stripped, allowlisted launch environment — provider API keys never
  reach the kernel (bwrap 0.4.0 has no ``--clearenv``, so the launcher builds
  the env explicitly and passes it via ``Popen(env=...)``).

The kernel speaks to the client over ZMQ **IPC** (unix sockets), not TCP, so
``--unshare-net`` does not break the kernel<->client channel; the IPC directory
is bind-mounted writable.

This module is pure: it detects availability and builds argv / env. Launching
is the kernel session's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import sys

BWRAP = "bwrap"

# Default system paths a scientific Python kernel needs to read. Filtered to
# those that exist so bwrap does not error on a missing bind source.
_SYSTEM_READ_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/alternatives",
    "/etc/resolv.conf",
    "/opt",
)

# Env keys copied from the host when present. Deny-by-default: anything not here
# (every *_API_KEY / *_TOKEN / *_SECRET / cloud credential) is dropped.
_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CONDA_SHLVL",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

# Forced values (override / inject regardless of host).
_ENV_FORCED = {
    "PYTHONNOUSERSITE": "1",
    "PYTHONUNBUFFERED": "1",
    "MPLBACKEND": "Agg",
    "OMICSCLAW_AUTONOMOUS_SANDBOX": "1",
}

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def envelope_available() -> bool:
    """True when the bubblewrap primitive is on PATH."""
    return shutil.which(BWRAP) is not None


@dataclass(slots=True)
class EnvelopeConfig:
    """Inputs for one sandboxed kernel launch."""

    workspace_root: Path
    ipc_dir: Path
    repo_root: Path
    read_roots: list[Path] = field(default_factory=list)
    allow_network: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)


def system_read_roots() -> list[Path]:
    """Existing system + interpreter paths the kernel reads."""
    roots: list[str] = list(_SYSTEM_READ_PATHS)
    # The active interpreter prefix(es): conda env, venv, and stdlib base.
    for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        if prefix:
            roots.append(prefix)
    seen: set[str] = set()
    resolved: list[Path] = []
    for raw in roots:
        path = Path(raw)
        if not path.exists():
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def scrub_env(base_env: dict[str, str] | None, *, workspace_root: Path) -> dict[str, str]:
    """Build the minimal, secret-free environment for the sandboxed kernel.

    Deny-by-default allowlist + forced values. ``HOME`` is pinned inside the
    workspace so libraries that write dotfiles cannot escape the writable bind.
    """
    source = dict(base_env if base_env is not None else os.environ)
    env: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = source.get(key)
        if value:
            env[key] = value
    env.update(_ENV_FORCED)
    env["HOME"] = str(workspace_root)
    env["TMPDIR"] = "/tmp"
    # Defensive: never leak a secret-shaped variable even if added to the
    # allowlist by mistake.
    for key in list(env):
        if any(marker in key.upper() for marker in _SECRET_MARKERS):
            del env[key]
    return env


def build_bwrap_argv(config: EnvelopeConfig, inner_argv: list[str]) -> list[str]:
    """Construct the bubblewrap command that wraps *inner_argv*.

    The returned argv launches ``inner_argv`` (e.g. an ipykernel launcher)
    inside the envelope. Network is unshared unless ``allow_network`` is set.
    """
    workspace = config.workspace_root.resolve()
    ipc_dir = config.ipc_dir.resolve()

    # NB: env is NOT set here. bubblewrap 0.4.0 has no ``--clearenv``; instead the
    # launcher starts bwrap with ``env=build_launch_env(config)`` and bwrap
    # forwards that already-scrubbed environment to the kernel. This is version
    # independent and keeps env *values* off the command line (out of ``ps``).
    argv: list[str] = [BWRAP, "--die-with-parent", "--new-session"]

    if not config.allow_network:
        argv += ["--unshare-net"]
    argv += ["--unshare-pid", "--unshare-uts", "--unshare-ipc", "--unshare-cgroup"]

    # Virtual filesystems.
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    # Read-only: interpreter + system, repo, then declared inputs.
    ro_roots: list[Path] = [*system_read_roots(), config.repo_root.resolve()]
    for root in config.read_roots:
        try:
            ro_roots.append(Path(root).expanduser().resolve())
        except OSError:
            continue
    for root in _dedupe_existing(ro_roots):
        # Never read-only-bind the workspace/ipc dir over their writable bind.
        if root == workspace or root == ipc_dir:
            continue
        argv += ["--ro-bind", str(root), str(root)]

    # Writable: workspace + ipc channel dir.
    argv += ["--bind", str(workspace), str(workspace)]
    if ipc_dir != workspace and workspace not in ipc_dir.parents:
        argv += ["--bind", str(ipc_dir), str(ipc_dir)]

    argv += ["--chdir", str(workspace), "--"]
    argv += list(inner_argv)
    return argv


def build_launch_env(config: EnvelopeConfig) -> dict[str, str]:
    """Scrubbed environment for the bwrap launcher process.

    bubblewrap forwards its own environment to the sandboxed kernel (we do not
    pass ``--clearenv``), so the launcher must start bwrap with exactly this
    secret-free env. Caller extras are merged but can never reintroduce a
    secret-shaped key.
    """
    env = scrub_env(None, workspace_root=config.workspace_root.resolve())
    for key, value in config.extra_env.items():
        if value and not any(marker in key.upper() for marker in _SECRET_MARKERS):
            env[key] = value
    return env


def _dedupe_existing(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


__all__ = [
    "BWRAP",
    "EnvelopeConfig",
    "build_bwrap_argv",
    "build_launch_env",
    "envelope_available",
    "scrub_env",
    "system_read_roots",
]
