"""Linux ``bubblewrap`` (``bwrap``) sandbox backend (Tier 1 full isolation).

Provides mount-namespace filesystem isolation, credential masking, workspace
persistence-file write protection, PID namespace isolation, ``PR_SET_PDEATHSIG``
(``--die-with-parent``), and ``--unshare-net`` network namespace isolation
bridged to the host :class:`EgressProxy` over a Unix domain socket.

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ._bridge import build_bridge_argv
from .config import (
    EgressMode,
    IsolationLevel,
    ProbeReport,
    SandboxBackendName,
    SandboxConfig,
)
from .profiles import (
    AnalysisProfile,
    build_analysis_profile,
    expand_deny_read_paths,
    resolve_workspace_deny_writes,
)

__all__ = [
    "BwrapBackend",
    "build_bwrap_argv",
    "probe",
    "probe_bwrap",
    "wrap",
]


def probe_bwrap(*, bwrap_bin: str | None = None) -> ProbeReport:
    """Probe whether ``bwrap`` is installed and allowed to create namespaces."""
    if not sys.platform.startswith("linux"):
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.BWRAP,
            level=IsolationLevel.NONE,
            summary="bwrap is only supported on Linux",
            reason=f"unsupported platform {sys.platform}",
        )
    binary = bwrap_bin or shutil.which("bwrap")
    if not binary:
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.BWRAP,
            level=IsolationLevel.NONE,
            summary="bubblewrap (bwrap) is not installed",
            reason="bwrap executable not found on PATH",
            fix_hint=(
                "Install bubblewrap (e.g. 'sudo apt install bubblewrap' or "
                "'conda install -c conda-forge bubblewrap')"
            ),
        )
    try:
        completed = subprocess.run(
            [
                binary,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--unshare-pid",
                "--die-with-parent",
                "--",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except OSError as exc:
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.BWRAP,
            level=IsolationLevel.NONE,
            summary="bubblewrap (bwrap) could not be executed",
            reason=str(exc),
            fix_hint="Verify that bwrap is executable",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {completed.returncode}"
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.BWRAP,
            level=IsolationLevel.NONE,
            summary="bubblewrap (bwrap) namespace creation was refused by the kernel",
            reason=reason,
            fix_hint=(
                "On Ubuntu 24.04+, check AppArmor unprivileged userns restrictions: "
                "'sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0'"
            ),
        )
    return ProbeReport(
        available=True,
        backend=SandboxBackendName.BWRAP,
        level=IsolationLevel.FULL,
        summary="Linux bubblewrap (FS read-only root + credential masking + PID + netns)",
    )


probe = probe_bwrap


def build_bwrap_argv(
    child_argv: Sequence[str],
    *,
    workspace: Path,
    cwd: Path,
    config: SandboxConfig,
    bwrap_bin: str = "bwrap",
    unix_socket_path: Path | None = None,
    proxy_port: int | None = None,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    home: Path | None = None,
    python_executable: str | None = None,
    profile: AnalysisProfile | None = None,
) -> list[str]:
    """Build the ``bwrap`` command line wrapping *child_argv*."""
    ws = workspace.resolve()
    work_cwd = cwd.resolve()
    effective_profile = profile or build_analysis_profile(
        ws,
        config,
        extra_read_paths=extra_read_paths,
        extra_write_paths=extra_write_paths,
        home=home,
        ensure_dirs=False,
    )
    cmd: list[str] = [
        bwrap_bin,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/var/tmp",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--unsetenv",
        "JPY_PARENT_PID",
    ]

    if config.gpus:
        dev_root = Path("/dev")
        if dev_root.is_dir():
            for nvidia_dev in sorted(dev_root.glob("nvidia*")):
                cmd.extend(["--dev-bind", str(nvidia_dev), str(nvidia_dev)])
            dri_dev = dev_root / "dri"
            if dri_dev.exists():
                cmd.extend(["--dev-bind", str(dri_dev), str(dri_dev)])

    if config.egress in (EgressMode.PROXY, EgressMode.DENY):
        cmd.append("--unshare-net")

    # Workspace is writable by default
    cmd.extend(["--bind", str(ws), str(ws)])

    # Additional read-only paths
    prof_ro = effective_profile.ro_mounts
    seen_ro: set[str] = set()
    for read_path in (*config.extra_read_paths, *prof_ro, *extra_read_paths):
        resolved_read = Path(read_path).resolve()
        key = str(resolved_read)
        if key not in seen_ro and resolved_read.exists():
            seen_ro.add(key)
            cmd.extend(["--ro-bind", key, key])

    # Protect persistence vectors inside the workspace (.git/hooks, .bashrc, .omicsclaw, etc.)
    protected_paths = effective_profile.protected_workspace_paths
    for protected in protected_paths:
        if protected.exists():
            cmd.extend(["--ro-bind", str(protected), str(protected)])

    # Additional writable paths (applied after deny_write_globs so kernel/pkg/cache dirs inside .omicsclaw work)
    prof_rw = (
        effective_profile.scratch_root,
        effective_profile.python_pkg_dir,
        effective_profile.r_pkg_dir,
        effective_profile.cache_dir,
        effective_profile.kernel_dir,
    )
    seen_rw: set[str] = set()
    for write_path in (*config.extra_write_paths, *prof_rw, *extra_write_paths):
        resolved_write = Path(write_path).resolve()
        key = str(resolved_write)
        if key not in seen_rw and resolved_write.exists():
            seen_rw.add(key)
            cmd.extend(["--bind", key, key])

    # Expose EgressProxy Unix socket directory when proxy egress is active
    if (
        config.egress == EgressMode.PROXY
        and unix_socket_path is not None
        and unix_socket_path.parent.exists()
    ):
        sock_dir = str(unix_socket_path.parent.resolve())
        cmd.extend(["--bind", sock_dir, sock_dir])

    # Credential masks must follow all mounts, including an interpreter under HOME.
    for cred_path in effective_profile.masked_read_paths:
        if cred_path.is_dir() and not cred_path.is_symlink():
            cmd.extend(["--tmpfs", str(cred_path)])
        elif cred_path.exists() or cred_path.is_symlink():
            cmd.extend(["--ro-bind", "/dev/null", str(cred_path)])
    for ws_masked in effective_profile.masked_paths:
        if ws_masked.exists() or ws_masked.is_symlink():
            cmd.extend(["--ro-bind", "/dev/null", str(ws_masked)])

    cmd.extend(["--chdir", str(work_cwd), "--"])

    if config.egress in (EgressMode.PROXY, EgressMode.DENY):
        py_bin = python_executable or sys.executable
        sock = unix_socket_path if config.egress == EgressMode.PROXY else None
        port = proxy_port if config.egress == EgressMode.PROXY else None
        cmd.extend(
            build_bridge_argv(
                py_bin,
                child_argv,
                unix_socket=sock,
                listen_port=port,
            )
        )
    else:
        cmd.extend(child_argv)

    return cmd


def wrap(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    workspace: Path,
    cwd: Path,
    config: SandboxConfig,
    bwrap_bin: str = "bwrap",
    unix_socket_path: Path | None = None,
    proxy_port: int | None = None,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    home: Path | None = None,
    profile: AnalysisProfile | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Functional wrapper returning ``(wrapped_argv, wrapped_env)`` for ``bwrap``."""
    wrapped_argv = build_bwrap_argv(
        argv,
        workspace=workspace,
        cwd=cwd,
        config=config,
        bwrap_bin=bwrap_bin,
        unix_socket_path=unix_socket_path,
        proxy_port=proxy_port,
        extra_read_paths=extra_read_paths,
        extra_write_paths=extra_write_paths,
        home=home,
        profile=profile,
    )
    out_env = dict(env)
    if profile is not None:
        out_env.update(profile.env_overrides)
    return wrapped_argv, out_env


class BwrapBackend:
    """Linux ``bwrap`` sandbox backend."""

    name = SandboxBackendName.BWRAP
    level = IsolationLevel.FULL
    kernel_transport = "ipc"

    def __init__(self, config: SandboxConfig, *, bwrap_bin: str | None = None) -> None:
        self._config = config
        self._bwrap_bin = bwrap_bin or shutil.which("bwrap") or "bwrap"

    def wrap_argv(
        self,
        child_argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        unix_socket_path: Path | None = None,
        proxy_port: int | None = None,
        extra_read_paths: Sequence[Path] = (),
        extra_write_paths: Sequence[Path] = (),
        home: Path | None = None,
    ) -> list[str]:
        return build_bwrap_argv(
            child_argv,
            workspace=workspace,
            cwd=cwd,
            config=self._config,
            bwrap_bin=self._bwrap_bin,
            unix_socket_path=unix_socket_path,
            proxy_port=proxy_port,
            extra_read_paths=extra_read_paths,
            extra_write_paths=extra_write_paths,
            home=home,
        )
