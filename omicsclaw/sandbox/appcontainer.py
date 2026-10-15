"""Windows AppContainer + Job Object + NTFS ACL sandbox backend (Tier 1).

Enforces kernel-level AppContainer security tokens on Windows 10/11:
- Zero network capabilities (``CapabilityCount = 0``) so direct external
  outbound connections fail at the kernel socket layer, while localhost
  loopback exemption allows traffic to the host :class:`EgressProxy` and
  local ZMQ kernel ports.
- Explicit NTFS ACL grants via ``icacls`` on the workspace (Modify) and
  interpreter prefixes (Read & Execute), while user credential directories
  (``~/.ssh``, ``~/.aws``, etc.) have no AppContainer SID grant and are
  unreadable.
- Workspace persistence targets (``.git/hooks``, ``.git/config``, ``.omicsclaw``,
  etc.) are restricted to Read & Execute ACLs and guarded by PATH shims.
- Win32 Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` ensures zero
  orphan processes on timeout or exit.

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._win_appcontainer_exec import build_appcontainer_exec_argv
from ._win_shims import ensure_win_shim_dir
from .config import (
    IsolationLevel,
    ProbeReport,
    SandboxBackendName,
    SandboxConfig,
)
from .profiles import CREDENTIAL_FILENAMES, AnalysisProfile, build_analysis_profile, resolve_workspace_deny_writes

__all__ = [
    "AppContainerBackend",
    "AppContainerPolicy",
    "build_appcontainer_policy",
    "build_icacls_commands",
    "derive_profile_name",
    "load_ro_roots_cache",
    "probe",
    "probe_appcontainer",
    "save_ro_roots_cache",
    "wrap",
]


@dataclass(frozen=True, slots=True)
class AppContainerPolicy:
    """Declarative Windows AppContainer policy for a workspace."""

    profile_name: str
    sid_string: str
    capability_count: int
    ro_roots: tuple[str, ...]
    rw_roots: tuple[str, ...]
    protected_rx_paths: tuple[str, ...]
    icacls_commands: tuple[tuple[str, ...], ...]


def derive_profile_name(workspace: Path) -> str:
    """Deterministic AppContainer profile name for *workspace*."""
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"omicsclaw.sandbox.{digest}"


def probe_appcontainer() -> ProbeReport:
    """Probe whether Windows AppContainer Win32 APIs are available."""
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.APPCONTAINER,
            level=IsolationLevel.NONE,
            summary="Windows AppContainer is only supported on win32",
            reason=f"unsupported platform {sys.platform}",
        )
    win_ver = getattr(sys, "getwindowsversion", lambda: None)()
    if win_ver is not None and getattr(win_ver, "build", 17763) < 17763:
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.APPCONTAINER,
            level=IsolationLevel.NONE,
            summary="Windows 10 v1809 (Build 17763) or later is required for AppContainer",
            reason=f"windows build {win_ver.build} < 17763",
        )
    try:
        userenv = ctypes.windll.userenv
        advapi32 = ctypes.windll.advapi32
        if not hasattr(userenv, "CreateAppContainerProfile") or not hasattr(
            advapi32, "ConvertSidToStringSidW"
        ):
            return ProbeReport(
                available=False,
                backend=SandboxBackendName.APPCONTAINER,
                level=IsolationLevel.NONE,
                summary="userenv.dll AppContainer APIs are unavailable",
                reason="missing CreateAppContainerProfile",
            )
    except Exception as exc:  # noqa: BLE001
        return ProbeReport(
            available=False,
            backend=SandboxBackendName.APPCONTAINER,
            level=IsolationLevel.NONE,
            summary="Windows AppContainer probe failed",
            reason=str(exc),
        )
    return ProbeReport(
        available=True,
        backend=SandboxBackendName.APPCONTAINER,
        level=IsolationLevel.FULL,
        summary="Windows AppContainer + Job Object + NTFS ACL isolation",
    )


probe = probe_appcontainer


def load_ro_roots_cache(state_file: Path) -> dict[str, str]:
    """Load cached read-only root mtimes from ``win-state.json``."""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("ro_roots"), dict):
            return {str(k): str(v) for k, v in data["ro_roots"].items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_ro_roots_cache(
    state_file: Path,
    *,
    sid_string: str,
    ro_roots: Mapping[str, str],
) -> None:
    """Persist read-only root DACL generation state to ``win-state.json``."""
    with contextlib.suppress(OSError):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {"sid": sid_string, "ro_roots": dict(ro_roots)},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def build_icacls_commands(
    sid_string: str,
    *,
    workspace: Path,
    config: SandboxConfig,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    cached_ro_roots: Mapping[str, str] | None = None,
) -> list[list[str]]:
    """Build ``icacls`` commands granting and restricting ACLs for *sid_string*."""
    ws = workspace.resolve()
    commands: list[list[str]] = [
        ["icacls", str(ws), "/grant", f"*{sid_string}:(OI)(CI)(M)", "/T", "/C", "/Q"],
    ]
    cache = cached_ro_roots or {}

    py_prefix = Path(sys.prefix).resolve()
    ro_candidates: list[Path] = []
    if py_prefix.exists():
        ro_candidates.append(py_prefix)
    for read_path in (*config.extra_read_paths, *extra_read_paths):
        rp = Path(read_path).resolve()
        if rp.exists():
            ro_candidates.append(rp)

    for rp in ro_candidates:
        rp_str = str(rp)
        with contextlib.suppress(OSError):
            mtime_tag = str(rp.stat().st_mtime_ns)
            if cache.get(rp_str) == mtime_tag:
                continue
        commands.append(
            ["icacls", rp_str, "/grant", f"*{sid_string}:(OI)(CI)(RX)", "/T", "/C", "/Q"]
        )

    for protected in resolve_workspace_deny_writes(ws, config.deny_write_globs):
        if protected.exists():
            commands.append(
                [
                    "icacls",
                    str(protected),
                    "/grant:r",
                    f"*{sid_string}:(OI)(CI)(RX)",
                    "/T",
                    "/C",
                    "/Q",
                ]
            )

    # Explicitly deny reading/writing workspace .env and credential files (carve-out)
    cred_targets: list[Path] = [ws / ".env"]
    for cred_name in sorted(CREDENTIAL_FILENAMES):
        cand = ws / cred_name
        if cand not in cred_targets and cand.exists():
            cred_targets.append(cand)
    for cred_file in cred_targets:
        if cred_file.name == ".env" or cred_file.exists():
            commands.append(
                ["icacls", str(cred_file), "/deny", f"*{sid_string}:(F)", "/C", "/Q"]
            )

    prof = build_analysis_profile(ws, config, ensure_dirs=False)
    prof_rw_subdirs = (
        prof.python_pkg_dir,
        prof.r_pkg_dir,
        prof.cache_dir,
        prof.kernel_dir,
    )
    for write_path in (*config.extra_write_paths, *prof_rw_subdirs, *extra_write_paths):
        wp = Path(write_path).resolve()
        if wp.exists():
            commands.append(
                ["icacls", str(wp), "/grant", f"*{sid_string}:(OI)(CI)(M)", "/T", "/C", "/Q"]
            )

    return commands


def build_appcontainer_policy(
    workspace: Path,
    *,
    config: SandboxConfig,
    sid_string: str = "S-1-15-2-1",
    profile: AnalysisProfile | None = None,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    cached_ro_roots: Mapping[str, str] | None = None,
) -> AppContainerPolicy:
    """Build the declarative :class:`AppContainerPolicy` for *workspace*."""
    ws = workspace.resolve()
    prof_reads = profile.ro_mounts if profile is not None else ()
    prof_writes = (profile.scratch_root,) if profile is not None else ()
    combined_reads = (*prof_reads, *extra_read_paths)
    combined_writes = (*prof_writes, *extra_write_paths)
    cmds = build_icacls_commands(
        sid_string,
        workspace=ws,
        config=config,
        extra_read_paths=combined_reads,
        extra_write_paths=combined_writes,
        cached_ro_roots=cached_ro_roots,
    )
    protected = tuple(
        str(p) for p in resolve_workspace_deny_writes(ws, config.deny_write_globs)
    )
    ro_roots = tuple(
        str(Path(p).resolve())
        for p in (Path(sys.prefix), *config.extra_read_paths, *combined_reads)
        if Path(p).exists()
    )
    rw_roots = tuple(
        str(Path(p).resolve())
        for p in (ws, *config.extra_write_paths, *combined_writes)
        if Path(p).exists()
    )
    return AppContainerPolicy(
        profile_name=derive_profile_name(ws),
        sid_string=sid_string,
        capability_count=0,
        ro_roots=ro_roots,
        rw_roots=rw_roots,
        protected_rx_paths=protected,
        icacls_commands=tuple(tuple(c) for c in cmds),
    )


def wrap(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    workspace: Path,
    cwd: Path,
    config: SandboxConfig,
    sid_string: str = "S-1-15-2-1",
    shim_dir: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Functional wrapper returning ``(wrapped_argv, wrapped_env)`` for AppContainer."""
    backend = AppContainerBackend(config, sid_string=sid_string, shim_dir=shim_dir)
    wrapped_argv = backend.wrap_argv(argv, workspace=workspace, cwd=cwd)
    out_env = dict(env)
    if shim_dir is not None:
        orig_path = out_env.get("PATH", "")
        out_env["PATH"] = f"{shim_dir};{orig_path}" if orig_path else str(shim_dir)
        orig_pypath = out_env.get("PYTHONPATH", "")
        out_env["PYTHONPATH"] = f"{shim_dir};{orig_pypath}" if orig_pypath else str(shim_dir)
    return wrapped_argv, out_env


class AppContainerBackend:
    """Windows AppContainer + Job Object + PATH shims sandbox backend."""

    name = SandboxBackendName.APPCONTAINER
    level = IsolationLevel.FULL
    kernel_transport = "tcp"

    def __init__(
        self,
        config: SandboxConfig,
        *,
        sid_string: str = "S-1-15-2-1",
        shim_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._sid_string = sid_string
        self._shim_dir = shim_dir
        self._profile_name: str = ""

    @property
    def sid_string(self) -> str:
        return self._sid_string

    @property
    def shim_dir(self) -> Path | None:
        return self._shim_dir

    def initialize_profile(self, workspace: Path, scratch_dir: Path) -> None:
        """Create or derive the AppContainer SID, configure ACLs, and write PATH shims."""
        self._profile_name = derive_profile_name(workspace)
        shim_target = self._shim_dir or (scratch_dir / "win-shims")
        self._shim_dir = ensure_win_shim_dir(
            shim_target,
            workspace=workspace,
            protected_targets=self._config.deny_write_globs,
            sid_string=self._sid_string,
        )
        if sys.platform != "win32" or not hasattr(ctypes, "windll"):
            return

        userenv = ctypes.windll.userenv
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        psid = ctypes.c_void_p()
        hr = userenv.CreateAppContainerProfile(
            ctypes.c_wchar_p(self._profile_name),
            ctypes.c_wchar_p("OmicsClaw Sandbox"),
            ctypes.c_wchar_p("OmicsClaw Agent Execution Sandbox"),
            None,
            0,
            ctypes.byref(psid),
        )
        if hr != 0 or not psid:
            userenv.DeriveAppContainerSidFromAppContainerName(
                ctypes.c_wchar_p(self._profile_name),
                ctypes.byref(psid),
            )
        if psid:
            str_ptr = ctypes.c_wchar_p()
            if advapi32.ConvertSidToStringSidW(psid, ctypes.byref(str_ptr)):
                self._sid_string = str_ptr.value or self._sid_string
                kernel32.LocalFree(str_ptr)
            advapi32.FreeSid(psid)

        with contextlib.suppress(Exception):
            subprocess.run(
                [
                    "CheckNetIsolation.exe",
                    "LoopbackExempt",
                    "-a",
                    f"-n={self._profile_name}",
                ],
                capture_output=True,
                timeout=5.0,
                check=False,
            )

        state_file = scratch_dir / "win-state.json"
        cached = load_ro_roots_cache(state_file)
        for cmd in build_icacls_commands(
            self._sid_string,
            workspace=workspace,
            config=self._config,
            extra_write_paths=(scratch_dir,),
            cached_ro_roots=cached,
        ):
            with contextlib.suppress(Exception):
                subprocess.run(cmd, capture_output=True, timeout=10.0, check=False)

    def cleanup_profile(self, workspace: Path) -> None:
        """Revoke AppContainer ACL grants on cleanup."""
        if sys.platform != "win32" or not self._sid_string:
            return
        with contextlib.suppress(Exception):
            subprocess.run(
                ["icacls", str(workspace.resolve()), "/remove", f"*{self._sid_string}", "/T", "/C", "/Q"],
                capture_output=True,
                timeout=10.0,
                check=False,
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
    ) -> list[str]:
        del workspace, unix_socket_path, proxy_port, extra_read_paths, extra_write_paths, home
        return build_appcontainer_exec_argv(
            sys.executable,
            child_argv,
            sid_string=self._sid_string,
            cwd=cwd,
        )
