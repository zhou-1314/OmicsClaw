"""macOS ``sandbox-exec`` Seatbelt SBPL sandbox backend (Tier 1 full isolation).

Enforces kernel-level Seatbelt SBPL policies:
- Blocks reading sensitive credential directories (``~/.ssh``, ``~/.aws``, etc.).
- Blocks writing outside the workspace and ``/tmp``, and protects workspace
  persistence files (``.git/hooks``, ``.bashrc``, ``.omicsclaw``, etc.).
- Blocks direct external TCP/UDP traffic while allowing connections to the
  localhost :class:`EgressProxy` port and local Unix domain sockets.

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .config import (
    IsolationLevel,
    ProbeReport,
    SandboxBackendName,
    SandboxConfig,
)
from .profiles import render_seatbelt_profile

__all__ = [
    "SeatbeltBackend",
    "build_seatbelt_argv",
    "build_seatbelt_profile",
    "probe",
    "probe_seatbelt",
    "wrap",
]

build_seatbelt_profile = render_seatbelt_profile


def probe_seatbelt(*, sandbox_exec_bin: str | None = None) -> ProbeReport:
    """Probe whether macOS ``sandbox-exec`` is available and functional."""
    if sys.platform != "darwin":
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.SEATBELT,
            level=IsolationLevel.NONE,
            summary="macOS Seatbelt is only supported on darwin",
            reason=f"unsupported platform {sys.platform}",
        )
    binary = sandbox_exec_bin or shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not Path(binary).is_file():
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.SEATBELT,
            level=IsolationLevel.NONE,
            summary="sandbox-exec binary not found on macOS",
            reason=f"{binary} does not exist",
        )
    try:
        completed = subprocess.run(
            [binary, "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except OSError as exc:
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.SEATBELT,
            level=IsolationLevel.NONE,
            summary="sandbox-exec could not be executed",
            reason=str(exc),
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {completed.returncode}"
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.SEATBELT,
            level=IsolationLevel.NONE,
            summary="macOS Seatbelt profile evaluation failed",
            reason=reason,
        )
    return ProbeReport(
        available=True,
        backend=SandboxBackendName.SEATBELT,
        level=IsolationLevel.FULL,
        summary="macOS Seatbelt (sandbox-exec SBPL filesystem + network isolation)",
    )


probe = probe_seatbelt


def build_seatbelt_argv(
    child_argv: Sequence[str],
    *,
    workspace: Path,
    config: SandboxConfig,
    sandbox_exec_bin: str = "/usr/bin/sandbox-exec",
    proxy_port: int | None = None,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    home: Path | None = None,
    allow_localhost_outbound: bool = False,
) -> list[str]:
    """Build ``sandbox-exec -p <profile> <child_argv...>``."""
    profile = render_seatbelt_profile(
        workspace,
        egress=config.egress,
        proxy_port=proxy_port,
        deny_read_paths=config.deny_read_paths,
        deny_write_globs=config.deny_write_globs,
        extra_read_paths=(*config.extra_read_paths, *extra_read_paths),
        extra_write_paths=(*config.extra_write_paths, *extra_write_paths),
        home=home,
        allow_localhost_outbound=allow_localhost_outbound,
    )
    return [sandbox_exec_bin, "-p", profile, *child_argv]


def wrap(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    workspace: Path,
    config: SandboxConfig,
    sandbox_exec_bin: str = "/usr/bin/sandbox-exec",
    proxy_port: int | None = None,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    home: Path | None = None,
    allow_localhost_outbound: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Functional wrapper returning ``(wrapped_argv, wrapped_env)`` for Seatbelt."""
    wrapped_argv = build_seatbelt_argv(
        argv,
        workspace=workspace,
        config=config,
        sandbox_exec_bin=sandbox_exec_bin,
        proxy_port=proxy_port,
        extra_read_paths=extra_read_paths,
        extra_write_paths=extra_write_paths,
        home=home,
        allow_localhost_outbound=allow_localhost_outbound,
    )
    return wrapped_argv, dict(env)


class SeatbeltBackend:
    """macOS ``sandbox-exec`` Seatbelt backend."""

    name = SandboxBackendName.SEATBELT
    level = IsolationLevel.FULL
    kernel_transport = "ipc"

    def __init__(
        self,
        config: SandboxConfig,
        *,
        sandbox_exec_bin: str | None = None,
    ) -> None:
        self._config = config
        self._sandbox_exec_bin = (
            sandbox_exec_bin
            or shutil.which("sandbox-exec")
            or "/usr/bin/sandbox-exec"
        )

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
        allow_localhost_outbound: bool = False,
    ) -> list[str]:
        del cwd, unix_socket_path
        return build_seatbelt_argv(
            child_argv,
            workspace=workspace,
            config=self._config,
            sandbox_exec_bin=self._sandbox_exec_bin,
            proxy_port=proxy_port,
            extra_read_paths=extra_read_paths,
            extra_write_paths=extra_write_paths,
            home=home,
            allow_localhost_outbound=allow_localhost_outbound,
        )
