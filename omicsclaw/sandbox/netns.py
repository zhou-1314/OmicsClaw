"""Linux ``unshare`` network + PID namespace fallback backend (Tier 1b).

Used when ``bubblewrap`` (``bwrap``) is not installed on a Linux host.
Provides:
- Network namespace isolation (``--net``) with default-deny egress bridged
  over a Unix domain socket to the host :class:`EgressProxy`.
- PID namespace isolation (``--pid --fork``) with ``--kill-child=SIGKILL`` so
  killing the parent process terminates the entire child PID namespace.
- Honest reporting as ``IsolationLevel.PARTIAL`` because filesystem isolation
  requires ``bwrap``.

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

__all__ = [
    "NetnsBackend",
    "build_netns_argv",
    "probe",
    "probe_netns",
    "wrap",
]


def probe_netns(*, unshare_bin: str | None = None) -> ProbeReport:
    """Probe whether ``unshare --user --map-root-user --net --pid --fork`` works."""
    if not sys.platform.startswith("linux"):
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.NETNS,
            level=IsolationLevel.NONE,
            summary="Linux netns is only supported on Linux",
            reason=f"unsupported platform {sys.platform}",
        )
    binary = unshare_bin or shutil.which("unshare")
    if not binary:
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.NETNS,
            level=IsolationLevel.NONE,
            summary="util-linux unshare is not installed",
            reason="unshare executable not found on PATH",
            fix_hint="Install util-linux or bubblewrap ('sudo apt install bubblewrap')",
        )
    try:
        completed = subprocess.run(
            [
                binary,
                "--user",
                "--map-root-user",
                "--net",
                "--pid",
                "--fork",
                "--kill-child=SIGKILL",
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
            backend=SandboxBackendName.NETNS,
            level=IsolationLevel.NONE,
            summary="unshare could not be executed",
            reason=str(exc),
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {completed.returncode}"
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.NETNS,
            level=IsolationLevel.NONE,
            summary="Linux user/network namespace creation was refused by the kernel",
            reason=reason,
            fix_hint=(
                "Enable unprivileged user namespaces: "
                "'sudo sysctl -w kernel.unprivileged_userns_clone=1' and "
                "'sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0'"
            ),
        )
    return ProbeReport(
        available=True,
        backend=SandboxBackendName.NETNS,
        level=IsolationLevel.PARTIAL,
        summary=(
            "Linux netns + PID namespace (network + process isolation active; "
            "install 'bubblewrap' for filesystem isolation)"
        ),
        fix_hint="Install 'bubblewrap' (bwrap) to upgrade from partial to full filesystem isolation",
    )


probe = probe_netns


def build_netns_argv(
    child_argv: Sequence[str],
    *,
    config: SandboxConfig,
    unshare_bin: str = "unshare",
    unix_socket_path: Path | None = None,
    proxy_port: int | None = None,
    python_executable: str | None = None,
) -> list[str]:
    """Build the ``unshare`` command line wrapping *child_argv*."""
    cmd: list[str] = [
        unshare_bin,
        "--user",
        "--map-root-user",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
    ]
    if config.egress in (EgressMode.PROXY, EgressMode.DENY):
        cmd.append("--net")
    cmd.append("--")

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
    config: SandboxConfig,
    unshare_bin: str = "unshare",
    unix_socket_path: Path | None = None,
    proxy_port: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Functional wrapper returning ``(wrapped_argv, wrapped_env)`` for ``netns``."""
    wrapped_argv = build_netns_argv(
        argv,
        config=config,
        unshare_bin=unshare_bin,
        unix_socket_path=unix_socket_path,
        proxy_port=proxy_port,
    )
    return wrapped_argv, dict(env)


class NetnsBackend:
    """Linux ``unshare`` network + PID namespace backend."""

    name = SandboxBackendName.NETNS
    level = IsolationLevel.PARTIAL
    kernel_transport = "ipc"

    def __init__(self, config: SandboxConfig, *, unshare_bin: str | None = None) -> None:
        self._config = config
        self._unshare_bin = unshare_bin or shutil.which("unshare") or "unshare"

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
        del workspace, cwd, extra_read_paths, extra_write_paths, home
        return build_netns_argv(
            child_argv,
            config=self._config,
            unshare_bin=self._unshare_bin,
            unix_socket_path=unix_socket_path,
            proxy_port=proxy_port,
        )
