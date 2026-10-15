"""Filesystem and network profile helpers for native OS sandbox backends.

Pure standard-library module with no logging and no process-environment reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_DENY_READ_PATHS,
    DEFAULT_DENY_WRITE_GLOBS,
    EgressMode,
    SandboxConfig,
)

__all__ = [
    "AnalysisProfile",
    "CREDENTIAL_FILENAMES",
    "PACKAGE_RC_FILES",
    "build_analysis_profile",
    "escape_sbpl_literal",
    "expand_deny_read_paths",
    "render_seatbelt_profile",
    "resolve_workspace_deny_writes",
]

CREDENTIAL_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "credentials.json",
        "application_default_credentials.json",
    }
)
"""Credential filenames carved out or masked inside the sandbox."""

PACKAGE_RC_FILES: tuple[str, ...] = (
    "/etc/pip.conf",
    "~/.pip/pip.conf",
    "~/.config/pip/pip.conf",
    "~/.condarc",
    "~/.Rprofile",
)
"""Host package-repository configuration files preserved read-only inside the sandbox."""


@dataclass(frozen=True, slots=True)
class AnalysisProfile:
    """Resolved paths and environment variables for the ``analysis`` sandbox profile."""

    workspace: Path
    scratch_root: Path
    rw_paths: tuple[Path, ...]
    ro_paths: tuple[Path, ...]
    masked_paths: tuple[Path, ...]
    masked_read_paths: tuple[Path, ...]
    protected_workspace_paths: tuple[Path, ...]
    git_ro_paths: tuple[Path, ...]
    python_pkg_dir: Path
    r_pkg_dir: Path
    cache_dir: Path
    kernel_dir: Path
    credential_filenames: frozenset[str]
    package_rc_paths: tuple[Path, ...]
    env_vars: dict[str, str]

    @property
    def ro_mounts(self) -> tuple[Path, ...]:
        return self.ro_paths

    @property
    def env_overrides(self) -> dict[str, str]:
        return self.env_vars

    @classmethod
    def for_workspace(
        cls,
        workspace: Path | str,
        config: SandboxConfig | None = None,
        *,
        scratch_root: Path | None = None,
        extra_read_paths: Sequence[Path] = (),
        extra_write_paths: Sequence[Path] = (),
        base_env: Mapping[str, str] | None = None,
        home: Path | None = None,
        ensure_dirs: bool = False,
    ) -> AnalysisProfile:
        """Build the ``analysis`` sandbox profile for *workspace*."""
        return build_analysis_profile(
            workspace,
            config,
            scratch_root=scratch_root,
            extra_read_paths=extra_read_paths,
            extra_write_paths=extra_write_paths,
            base_env=base_env,
            home=home,
            ensure_dirs=ensure_dirs,
        )


def build_analysis_profile(
    workspace: Path | str,
    config: SandboxConfig | None = None,
    *,
    scratch_root: Path | None = None,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    base_env: Mapping[str, str] | None = None,
    home: Path | None = None,
    ensure_dirs: bool = False,
) -> AnalysisProfile:
    """Construct an :class:`AnalysisProfile` for *workspace*."""
    ws = Path(workspace).expanduser().resolve()
    base_home = home if home is not None else Path.home()

    state_root = ws / ".omicsclaw"
    sbx_root = state_root / "sandbox"
    effective_scratch = (
        Path(scratch_root).expanduser().resolve()
        if scratch_root is not None
        else sbx_root
    )
    python_pkg_dir = sbx_root / "pkgs" / "python"
    r_pkg_dir = sbx_root / "pkgs" / "R"
    cache_dir = sbx_root / "cache"
    kernel_dir = state_root / "kernel"

    if ensure_dirs:
        for d in (python_pkg_dir, r_pkg_dir, cache_dir, kernel_dir):
            d.mkdir(parents=True, exist_ok=True)

    cfg_read = config.extra_read_paths if config is not None else ()
    cfg_write = config.extra_write_paths if config is not None else ()
    deny_read = config.deny_read_paths if config is not None else DEFAULT_DENY_READ_PATHS
    deny_write = config.deny_write_globs if config is not None else DEFAULT_DENY_WRITE_GLOBS

    rw_list: list[Path] = [ws, python_pkg_dir, r_pkg_dir, cache_dir, kernel_dir]
    if effective_scratch not in rw_list:
        rw_list.append(effective_scratch)
    for p in (*cfg_write, *extra_write_paths):
        path_obj = Path(p).expanduser().resolve()
        if path_obj not in rw_list:
            rw_list.append(path_obj)

    ro_list: list[Path] = []
    for p in (*cfg_read, *extra_read_paths):
        path_obj = Path(p).expanduser().resolve()
        if path_obj not in ro_list:
            ro_list.append(path_obj)

    rc_paths: list[Path] = []
    for raw_rc in PACKAGE_RC_FILES:
        if raw_rc.startswith("~/"):
            candidate = base_home / raw_rc[2:]
        else:
            candidate = Path(raw_rc)
        if candidate not in rc_paths:
            rc_paths.append(candidate)

    masked: list[Path] = [ws / ".env"]
    for name in CREDENTIAL_FILENAMES:
        cand = ws / name
        if cand not in masked and (name == ".env" or cand.exists()):
            masked.append(cand)

    expanded_deny_reads = list(expand_deny_read_paths(deny_read, home=base_home))
    for m in masked:
        if m not in expanded_deny_reads:
            expanded_deny_reads.append(m)

    protected_ws = resolve_workspace_deny_writes(ws, deny_write)
    git_ro: list[Path] = [ws / ".git"]

    env_map = dict(base_env) if base_env is not None else {}
    in_venv = bool(env_map.get("VIRTUAL_ENV") or env_map.get("CONDA_PREFIX"))
    injected_env: dict[str, str] = {
        "PYTHONUSERBASE": str(python_pkg_dir),
        "R_LIBS_USER": str(r_pkg_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "MPLCONFIGDIR": str(cache_dir / "matplotlib"),
        "MPLBACKEND": "Agg",
        "NUMBA_CACHE_DIR": str(cache_dir / "numba"),
    }
    if not in_venv:
        injected_env["PIP_USER"] = "1"

    return AnalysisProfile(
        workspace=ws,
        scratch_root=effective_scratch,
        rw_paths=tuple(rw_list),
        ro_paths=tuple(ro_list),
        masked_paths=tuple(masked),
        masked_read_paths=tuple(expanded_deny_reads),
        protected_workspace_paths=protected_ws,
        git_ro_paths=tuple(git_ro),
        python_pkg_dir=python_pkg_dir,
        r_pkg_dir=r_pkg_dir,
        cache_dir=cache_dir,
        kernel_dir=kernel_dir,
        credential_filenames=CREDENTIAL_FILENAMES,
        package_rc_paths=tuple(rc_paths),
        env_vars=injected_env,
    )


def escape_sbpl_literal(text: str) -> str:
    """Escape ``\\`` and ``"`` for safe inclusion in a Seatbelt SBPL string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def expand_deny_read_paths(
    deny_read_paths: Sequence[str] = DEFAULT_DENY_READ_PATHS,
    *,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Expand ``~`` in *deny_read_paths* against *home* (or ``Path.home()``)."""
    base_home = home if home is not None else Path.home()
    expanded: list[Path] = []
    seen: set[str] = set()
    for raw in deny_read_paths:
        item = raw.strip()
        if not item:
            continue
        if item == "~":
            candidate = base_home
        elif item.startswith("~/") or item.startswith("~\\"):
            candidate = base_home / item[2:]
        else:
            candidate = Path(item).expanduser()
        for variant in _path_variants(candidate):
            key = str(variant)
            if key not in seen:
                seen.add(key)
                expanded.append(variant)
    return tuple(expanded)


def resolve_workspace_deny_writes(
    workspace: Path,
    deny_write_globs: Sequence[str] = DEFAULT_DENY_WRITE_GLOBS,
) -> tuple[Path, ...]:
    """Return workspace paths that must be protected from writes inside the sandbox."""
    ws = workspace.resolve()
    protected: list[Path] = []
    seen: set[str] = set()
    for pattern in deny_write_globs:
        clean = pattern.strip().lstrip("/\\")
        if not clean:
            continue
        if "*" in clean or "?" in clean:
            matches = list(ws.glob(clean))
        else:
            matches = [ws / clean]
        for match in matches:
            for variant in _path_variants(match):
                key = str(variant)
                if key not in seen:
                    seen.add(key)
                    protected.append(variant)
    return tuple(protected)


def _path_variants(path: Path) -> tuple[Path, ...]:
    """Return both lexical absolute path and resolved path (handling ``/var`` -> ``/private/var``)."""
    abs_path = path.absolute()
    try:
        resolved = path.resolve()
    except OSError:
        return (abs_path,)
    if str(resolved) != str(abs_path):
        return (abs_path, resolved)
    return (abs_path,)


def render_seatbelt_profile(
    workspace: Path,
    *,
    egress: EgressMode = EgressMode.PROXY,
    proxy_port: int | None = None,
    deny_read_paths: Sequence[str] = DEFAULT_DENY_READ_PATHS,
    deny_write_globs: Sequence[str] = DEFAULT_DENY_WRITE_GLOBS,
    extra_read_paths: Sequence[Path] = (),
    extra_write_paths: Sequence[Path] = (),
    home: Path | None = None,
    allow_localhost_outbound: bool = False,
) -> str:
    """Render a macOS ``sandbox-exec`` SBPL profile.

    In SBPL, rules are evaluated in order and later rules override earlier
    ones:
    1. Allow general file reads, then explicitly deny credential paths and
       ``<workspace>/.env``.
    2. Deny general file writes, then allow workspace and temporary directories,
       then deny protected paths inside the workspace (e.g. ``.git``,
       ``.git/hooks``, ``.omicsclaw``), and finally allow explicit
       ``extra_write_paths`` and sandbox pkg/cache/kernel directories.
    3. Deny network by default and allow only ``localhost:<proxy_port>`` when
       ``egress == EgressMode.PROXY``.
    """
    profile = build_analysis_profile(
        workspace,
        extra_read_paths=extra_read_paths,
        extra_write_paths=extra_write_paths,
        home=home,
        ensure_dirs=False,
    )
    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow system-socket)",
        "(allow pseudo-tty)",
        "(allow file-read*)",
    ]

    for cred_path in expand_deny_read_paths(deny_read_paths, home=home):
        escaped = escape_sbpl_literal(str(cred_path))
        lines.append(f'(deny file-read* (subpath "{escaped}") (literal "{escaped}"))')

    for masked in profile.masked_paths:
        for variant in _path_variants(masked):
            escaped = escape_sbpl_literal(str(variant))
            lines.append(f'(deny file-read* (literal "{escaped}"))')

    for read_path in extra_read_paths:
        for variant in _path_variants(Path(read_path)):
            escaped = escape_sbpl_literal(str(variant))
            lines.append(f'(allow file-read* (subpath "{escaped}") (literal "{escaped}"))')

    lines.append("(deny file-write*)")
    lines.extend(
        [
            '(allow file-write* (literal "/dev/null") (literal "/dev/zero")'
            ' (literal "/dev/random") (literal "/dev/urandom")'
            ' (literal "/dev/tty") (literal "/dev/dtracehelper"))',
            '(allow file-write* (subpath "/tmp") (subpath "/private/tmp")'
            ' (subpath "/var/folders") (subpath "/private/var/folders"))',
        ]
    )

    for variant in _path_variants(workspace):
        escaped_ws = escape_sbpl_literal(str(variant))
        lines.append(f'(allow file-write* (subpath "{escaped_ws}"))')

    for protected in resolve_workspace_deny_writes(workspace, deny_write_globs):
        escaped_prot = escape_sbpl_literal(str(protected))
        lines.append(
            f'(deny file-write* (subpath "{escaped_prot}") (literal "{escaped_prot}"))'
        )

    # Re-allow sandbox persistent pkg/cache/kernel subdirs inside .omicsclaw
    allowed_subdirs = (
        profile.python_pkg_dir,
        profile.r_pkg_dir,
        profile.cache_dir,
        profile.kernel_dir,
        *extra_write_paths,
    )
    for write_path in allowed_subdirs:
        for variant in _path_variants(Path(write_path)):
            escaped_wr = escape_sbpl_literal(str(variant))
            lines.append(
                f'(allow file-write* (subpath "{escaped_wr}") (literal "{escaped_wr}"))'
            )

    if egress == EgressMode.OPEN:
        lines.append("(allow network*)")
    elif egress == EgressMode.NONE:
        lines.append("(deny network*)")
        lines.append("(allow network* (local unix-socket))")
        if allow_localhost_outbound:
            lines.append('(allow network* (local ip "localhost:*"))')
            lines.append('(allow network-outbound (remote ip "localhost:*"))')
    else:
        # EgressMode.PROXY
        lines.append("(deny network*)")
        lines.append("(allow network* (local unix-socket))")
        if allow_localhost_outbound:
            lines.append('(allow network* (local ip "localhost:*"))')
            lines.append('(allow network-outbound (remote ip "localhost:*"))')
        elif proxy_port is not None and proxy_port > 0:
            lines.append(
                f'(allow network-outbound (remote tcp "localhost:{int(proxy_port)}"))'
            )
            lines.append(
                f'(allow network-outbound (remote ip "localhost:{int(proxy_port)}"))'
            )
            lines.append('(allow network* (local ip "localhost:*"))')

    return "\n".join(lines) + "\n"
