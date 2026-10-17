"""Unified native OS sandbox manager for ``bash`` and persistent kernels.

Selects the highest-capability native backend available on the host OS:
- Linux: ``bwrap`` (Tier 1 full isolation) -> ``netns`` (Tier 1b network + PID
  namespace isolation).
- macOS: ``seatbelt`` (``sandbox-exec`` SBPL full isolation).
- Windows: ``appcontainer`` (AppContainer token + Job Object + NTFS ACLs + PATH
  shims).

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .appcontainer import AppContainerBackend, probe_appcontainer
from .bwrap import BwrapBackend, probe_bwrap
from .config import (
    BackendName,
    CommandOutcome,
    EgressMode,
    IsolationLevel,
    Level,
    ProbeReport,
    SandboxBackendName,
    SandboxConfig,
    SandboxMode,
    SandboxUnavailable,
)
from .egress import EgressProxy, extract_blocked_host, format_egress_blocked_hint, parse_upstream_proxy
from .netns import NetnsBackend, probe_netns
from .profiles import AnalysisProfile, build_analysis_profile
from .seatbelt import SeatbeltBackend, probe_seatbelt

__all__ = [
    "KernelSpawnAdapter",
    "SandboxManager",
    "SandboxSession",
    "SandboxStatus",
    "probe_sandbox_backend",
]

_POSIX = os.name == "posix"
_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)
_REAP_GRACE = 2.0


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    """Structured snapshot of the sandbox backend, level, and egress posture."""

    active: bool
    backend: BackendName
    level: Level
    egress: EgressMode
    summary: str
    covers: tuple[str, ...] = ("bash",)
    proxy_url: str = ""
    allowed_domains: tuple[str, ...] = ()
    degraded_reason: str = ""
    fix_hint: str = ""


def _resolve_shell_argv(command: str) -> list[str]:
    if sys.platform != "win32":
        return ["bash", "-c", command]
    which_bash = shutil.which("bash")
    if which_bash and "system32\\bash.exe" not in which_bash.lower():
        return [which_bash, "-c", command]
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        Path(sys.prefix) / "Library" / "bin" / "bash.exe",
        Path(sys.prefix) / "Library" / "usr" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return [str(candidate), "-c", command]
    pwsh = shutil.which("powershell.exe") or "powershell.exe"
    return [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]


def probe_sandbox_backend(
    preferred: SandboxBackendName = SandboxBackendName.AUTO,
) -> ProbeReport:
    """Probe the requested (or platform-auto) native OS sandbox backend."""
    if preferred in (SandboxBackendName.NONE, SandboxBackendName.OFF):
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.NONE,
            level=IsolationLevel.NONE,
            summary="sandbox backend explicitly set to off",
        )
    if preferred == SandboxBackendName.BWRAP:
        return probe_bwrap()
    if preferred == SandboxBackendName.NETNS:
        return probe_netns()
    if preferred == SandboxBackendName.SEATBELT:
        return probe_seatbelt()
    if preferred == SandboxBackendName.APPCONTAINER:
        return probe_appcontainer()

    # AUTO detection by platform
    if sys.platform.startswith("linux"):
        bwrap_report = probe_bwrap()
        if bwrap_report.available:
            return bwrap_report
        netns_report = probe_netns()
        if netns_report.available:
            return netns_report
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.NONE,
            level=IsolationLevel.NONE,
            summary="Neither bubblewrap (bwrap) nor unshare netns is available on this Linux host",
            reason=f"bwrap: {bwrap_report.reason}; netns: {netns_report.reason}",
            fix_hint=bwrap_report.fix_hint or netns_report.fix_hint,
        )
    if sys.platform == "darwin":
        return probe_seatbelt()
    if sys.platform == "win32":
        return probe_appcontainer()
    return ProbeReport(
        available=False,
        backend=SandboxBackendName.NONE,
        level=IsolationLevel.NONE,
        summary=f"No native OS sandbox backend for platform {sys.platform}",
        reason=f"unsupported platform {sys.platform}",
    )


def _instantiate_backend(
    report: ProbeReport,
    config: SandboxConfig,
) -> BwrapBackend | NetnsBackend | SeatbeltBackend | AppContainerBackend:
    if report.backend == SandboxBackendName.BWRAP:
        return BwrapBackend(config)
    if report.backend == SandboxBackendName.NETNS:
        return NetnsBackend(config)
    if report.backend == SandboxBackendName.SEATBELT:
        return SeatbeltBackend(config)
    if report.backend == SandboxBackendName.APPCONTAINER:
        return AppContainerBackend(config)
    raise SandboxUnavailable(
        f"Cannot instantiate sandbox backend {report.backend.value}: {report.reason or report.summary}"
    )


@dataclass(frozen=True, slots=True)
class KernelSpawnAdapter:
    """Structural ``SpawnWrapper`` adapter consumed by ``omicsclaw.kernel``."""

    manager: SandboxManager
    host_pid_lifecycle: bool = False

    @property
    def kernel_transport(self) -> str:
        return self.manager.kernel_transport

    def wrap(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        extra_read_paths: Sequence[Path] = (),
        extra_write_paths: Sequence[Path] = (),
    ) -> tuple[list[str], dict[str, str]]:
        return self.manager.wrap_kernel_argv(
            argv,
            cwd=cwd,
            env=env,
            extra_read_paths=extra_read_paths,
            extra_write_paths=extra_write_paths,
            host_pid_lifecycle=self.host_pid_lifecycle,
        )


class SandboxManager:
    """Manages the native OS sandbox backend and localhost :class:`EgressProxy`.

    Satisfies ``BashEnvironment`` structurally via :meth:`run_bash` and provides
    :meth:`as_kernel_spawn_wrapper` for persistent Python and R kernels.
    """

    def __init__(
        self,
        workspace: Path | str,
        *,
        config: SandboxConfig | None = None,
        report: ProbeReport | None = None,
        env_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        self._config = config or SandboxConfig(mode=SandboxMode.AUTO)
        self._report = report or probe_sandbox_backend(self._config.backend)
        if not self._report.available:
            raise SandboxUnavailable(
                f"Native OS sandbox is unavailable: {self._report.summary}"
                + (f" ({self._report.reason})" if self._report.reason else "")
                + (f". Fix: {self._report.fix_hint}" if self._report.fix_hint else "")
            )
        self._backend = _instantiate_backend(self._report, self._config)
        self._env_provider: Callable[[], Mapping[str, str]] = (
            env_provider if env_provider is not None else dict
        )
        self._proxy: EgressProxy | None = None
        self._scratch_dir: Path | None = None
        self._profile: AnalysisProfile | None = None
        self._started = False

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def config(self) -> SandboxConfig:
        return self._config

    @property
    def report(self) -> ProbeReport:
        return self._report

    @property
    def backend_name(self) -> SandboxBackendName:
        return self._report.backend

    @property
    def level(self) -> IsolationLevel:
        return self._report.level

    @property
    def kernel_transport(self) -> str:
        return self._backend.kernel_transport

    @property
    def proxy(self) -> EgressProxy | None:
        return self._proxy

    @property
    def proxy_port(self) -> int | None:
        if self._proxy is not None and self._proxy.port > 0:
            return self._proxy.port
        return None

    @property
    def unix_socket_path(self) -> Path | None:
        if self._proxy is not None:
            return self._proxy.unix_socket_path
        return None

    @property
    def analysis_profile(self) -> AnalysisProfile | None:
        return self._profile

    def spawn_wrapper(self, *, host_pid_lifecycle: bool = False) -> KernelSpawnAdapter:
        """Maintenance can provide its own host-PID descendant supervisor.

        Only the netns PID wrapper changes in that explicit mode; network and
        filesystem policy remain selected by this manager. Kernel defaults do
        not use this mode.
        """
        return KernelSpawnAdapter(self, host_pid_lifecycle)

    def status(self) -> SandboxStatus:
        """Return a structured :class:`SandboxStatus` snapshot."""
        covers = ("bash", "python", "r") if self._config.sandbox_kernels else ("bash",)
        proxy_url = self._proxy.proxy_url if self._proxy is not None else ""
        allowed = (
            self._proxy.allowed_domains
            if self._proxy is not None
            else self._config.effective_allowed_domains
        )
        return SandboxStatus(
            active=self._started,
            backend=self._report.backend,
            level=self._report.level,
            egress=self._config.egress,
            summary=self._report.summary,
            covers=covers,
            proxy_url=proxy_url,
            allowed_domains=allowed,
            degraded_reason=self._report.reason,
            fix_hint=self._report.fix_hint,
        )

    def as_kernel_spawn_wrapper(self) -> KernelSpawnAdapter | None:
        """Return a structural ``SpawnWrapper`` for ``omicsclaw.kernel``."""
        if not self._config.sandbox_kernels:
            return None
        return KernelSpawnAdapter(self)

    def allow_domain_for_session(self, domain: str) -> None:
        """Dynamically allow *domain* in the running :class:`EgressProxy`."""
        if self._proxy is not None:
            self._proxy.allow_domain_for_session(domain)

    def grant_domain(self, domain: str) -> None:
        """Alias for :meth:`allow_domain_for_session`."""
        self.allow_domain_for_session(domain)

    def revoke_domain(self, domain: str) -> bool:
        """Revoke *domain* from the running :class:`EgressProxy`."""
        if self._proxy is not None:
            return self._proxy.revoke_domain(domain)
        return False

    async def start(self) -> None:
        """Start the localhost/UDS :class:`EgressProxy` and backend profile."""
        if self._started:
            return
        # Short prefix in /tmp so AF_UNIX socket paths stay well under 92 bytes
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="oc-sbx-"))
        base_env = self._env_provider()
        home_str = base_env.get("HOME", "").strip()
        home_path = Path(home_str) if home_str else None
        self._profile = build_analysis_profile(
            self._workspace,
            self._config,
            scratch_root=self._scratch_dir,
            home=home_path,
            ensure_dirs=True,
        )
        if self._config.egress == EgressMode.PROXY:
            upstream = parse_upstream_proxy(base_env)
            if upstream is None and self._config.upstream_proxy:
                upstream = parse_upstream_proxy({"HTTPS_PROXY": self._config.upstream_proxy})
            uds_path = (
                self._scratch_dir / "egress.sock"
                if sys.platform.startswith("linux")
                else None
            )
            self._proxy = EgressProxy(
                allowed_domains=self._config.effective_allowed_domains,
                egress_groups=self._config.egress_groups,
                egress_allow=self._config.egress_allow,
                egress_endpoints=self._config.egress_endpoints,
                allow_private_cidr=self._config.allow_private_cidr,
                upstream=upstream,
                unix_socket_path=uds_path,
                no_proxy=self._config.no_proxy,
            )
            await self._proxy.start()

        if isinstance(self._backend, AppContainerBackend):
            self._backend.initialize_profile(self._workspace, self._scratch_dir)

        self._started = True

    async def stop(self) -> None:
        """Stop the :class:`EgressProxy` and clean up temporary state."""
        if self._proxy is not None:
            await self._proxy.stop()
            self._proxy = None
        if isinstance(self._backend, AppContainerBackend):
            self._backend.cleanup_profile(self._workspace)
        if self._scratch_dir is not None:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_dir = None
        self._profile = None
        self._started = False

    async def aclose(self) -> None:
        """Alias for :meth:`stop`."""
        await self.stop()

    def _build_child_env(self, base: Mapping[str, str]) -> dict[str, str]:
        env = dict(base)
        if self._config.scrubbed_env:
            env.update(self._config.scrubbed_env)
        if self._profile is not None:
            for k, v in self._profile.env_overrides.items():
                env.setdefault(k, v)

        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "PIP_PROXY",
        ):
            env.pop(key, None)

        no_proxy_items = ["localhost", "127.0.0.1", "::1", *self._config.no_proxy]
        no_proxy_val = ",".join(dict.fromkeys(i for i in no_proxy_items if i))
        env["NO_PROXY"] = no_proxy_val
        env["no_proxy"] = no_proxy_val

        if self._config.egress == EgressMode.PROXY and self._proxy is not None and self._proxy.port > 0:
            proxy_url = self._proxy.proxy_url
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "PIP_PROXY",
            ):
                env[key] = proxy_url

        if isinstance(self._backend, AppContainerBackend) and self._backend.shim_dir is not None:
            orig_path = env.get("PATH", "")
            env["PATH"] = (
                f"{self._backend.shim_dir}{os.pathsep}{orig_path}"
                if orig_path
                else str(self._backend.shim_dir)
            )
            orig_pypath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{self._backend.shim_dir}{os.pathsep}{orig_pypath}"
                if orig_pypath
                else str(self._backend.shim_dir)
            )
        return env

    def wrap(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        cwd: Path | None = None,
        extra_read_paths: Sequence[Path] = (),
        extra_write_paths: Sequence[Path] = (),
    ) -> tuple[list[str], dict[str, str]]:
        """Wrap arbitrary *argv* and *env* inside the active sandbox."""
        return self.wrap_kernel_argv(
            argv,
            cwd=cwd or self._workspace,
            env=env,
            extra_read_paths=extra_read_paths,
            extra_write_paths=extra_write_paths,
        )

    def wrap_kernel_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        extra_read_paths: Sequence[Path] = (),
        extra_write_paths: Sequence[Path] = (),
        host_pid_lifecycle: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        """Wrap a persistent kernel's *argv* and *env* to run inside the sandbox."""
        base_env = dict(self._env_provider())
        # Credential masks belong to the host user, even when a child uses
        # a private HOME for package configuration and caches.
        home_str = base_env.get("HOME", "").strip()
        home_path = Path(home_str) if home_str else None
        base_env.update(env)
        child_env = self._build_child_env(base_env)
        prof_reads = self._profile.ro_mounts if self._profile is not None else ()
        prof_writes = (self._profile.scratch_root,) if self._profile is not None else ()

        if isinstance(self._backend, SeatbeltBackend):
            wrapped = self._backend.wrap_argv(
                argv,
                workspace=self._workspace,
                cwd=cwd,
                unix_socket_path=self.unix_socket_path,
                proxy_port=self.proxy_port,
                extra_read_paths=(*prof_reads, *extra_read_paths),
                extra_write_paths=(*prof_writes, *extra_write_paths),
                home=home_path,
                allow_localhost_outbound=(self.kernel_transport == "tcp"),
            )
        else:
            lifecycle = (
                {"pid_namespace": False}
                if host_pid_lifecycle and isinstance(self._backend, NetnsBackend)
                else {}
            )
            wrapped = self._backend.wrap_argv(
                argv,
                workspace=self._workspace,
                cwd=cwd,
                unix_socket_path=self.unix_socket_path,
                proxy_port=self.proxy_port,
                extra_read_paths=(*prof_reads, *extra_read_paths),
                extra_write_paths=(*prof_writes, *extra_write_paths),
                home=home_path,
                **lifecycle,
            )
        return wrapped, child_env

    async def run_bash(
        self,
        command: str,
        cwd: str,
        timeout: float,
    ) -> CommandOutcome:
        """Run *command* in *cwd* inside the active native OS sandbox."""
        if not self._started:
            await self.start()

        if self._proxy is not None:
            self._proxy.drain_recent_blocked()

        work_cwd = Path(cwd).expanduser().resolve()
        base_env = self._env_provider()
        child_env = self._build_child_env(base_env)
        home_str = child_env.get("HOME", "").strip()
        home_path = Path(home_str) if home_str else None
        prof_reads = self._profile.ro_mounts if self._profile is not None else ()
        prof_writes = (self._profile.scratch_root,) if self._profile is not None else ()

        shell_argv = _resolve_shell_argv(command)
        wrapped_argv = self._backend.wrap_argv(
            shell_argv,
            workspace=self._workspace,
            cwd=work_cwd,
            unix_socket_path=self.unix_socket_path,
            proxy_port=self.proxy_port,
            extra_read_paths=prof_reads,
            extra_write_paths=prof_writes,
            home=home_path,
        )

        handle, capture = tempfile.mkstemp(prefix="omicsclaw-sbx-", suffix=".log")
        timed_out = False
        try:
            with os.fdopen(handle, "wb") as sink:
                proc = await asyncio.create_subprocess_exec(
                    *wrapped_argv,
                    cwd=str(work_cwd),
                    env=child_env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=sink,
                    stderr=sink,
                    start_new_session=True,
                )
                try:
                    async with asyncio.timeout(timeout):
                        code = await proc.wait()
                except TimeoutError:
                    timed_out = True
                    code = await _kill_process_group(proc)
                except asyncio.CancelledError:
                    await _kill_process_group(proc)
                    raise
                finally:
                    if proc.returncode is None:
                        _signal_process_group(proc)
            with open(capture, "rb") as reader:
                output = reader.read().decode("utf-8", errors="replace")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(capture)

        notices: list[str] = []
        seen_hosts: set[str] = set()
        if self._proxy is not None:
            for ev in self._proxy.drain_recent_blocked_events():
                if ev.host not in seen_hosts:
                    seen_hosts.add(ev.host)
                    notices.append(format_egress_blocked_hint(ev.host, ev.reason))
        if output and "[Sandbox Egress Blocked]" not in output:
            extracted = extract_blocked_host(output)
            if extracted and extracted not in seen_hosts:
                seen_hosts.add(extracted)
                notices.append(format_egress_blocked_hint(extracted))
        if notices:
            suffix = "\n" if output and not output.endswith("\n") else ""
            output = output + suffix + "\n".join(notices) + "\n"

        exit_code = (128 - code) if code < 0 else code
        return CommandOutcome(output=output, exit_code=exit_code, timed_out=timed_out)


SandboxSession = SandboxManager


def _signal_process_group(proc: asyncio.subprocess.Process) -> None:
    if _POSIX:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, _KILL)
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
        except Exception:  # noqa: BLE001
            with contextlib.suppress(OSError):
                proc.kill()


async def _kill_process_group(proc: asyncio.subprocess.Process) -> int:
    _signal_process_group(proc)
    try:
        return await asyncio.wait_for(proc.wait(), _REAP_GRACE)
    except TimeoutError:
        return -9
