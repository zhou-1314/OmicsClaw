"""Native OS sandboxing for OmicsClaw (Linux ``bwrap``/``netns``, macOS ``seatbelt``, Windows ``AppContainer``).

Replaces the retired Docker container sandbox with lightweight, zero-image
native OS isolation and a default-deny localhost HTTP/HTTPS ``CONNECT`` egress
proxy.

Pure standard-library leaf layer: imports only the Python standard library and
nothing else inside ``omicsclaw``.
"""

from __future__ import annotations

from .appcontainer import (
    AppContainerBackend,
    AppContainerPolicy,
    build_appcontainer_policy,
    probe_appcontainer,
)
from .bwrap import BwrapBackend, build_bwrap_argv, probe_bwrap
from .config import (
    BackendName,
    CommandOutcome,
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_DENY_READ_PATHS,
    DEFAULT_DENY_WRITE_GLOBS,
    EgressMode,
    IsolationLevel,
    Level,
    Mount,
    ProbeReport,
    SandboxBackendName,
    SandboxConfig,
    SandboxConfigError,
    SandboxError,
    SandboxMode,
    SandboxUnavailable,
    host_user,
)
from .egress import (
    BlockedEvent,
    EGRESS_DENYLIST,
    EGRESS_GROUPS,
    EgressDecision,
    EgressProxy,
    UpstreamProxy,
    domain_matches,
    evaluate_egress,
    extract_blocked_host,
    format_egress_blocked_hint,
    is_ip_allowed,
    parse_upstream_proxy,
)
from .manager import (
    KernelSpawnAdapter,
    SandboxManager,
    SandboxSession,
    SandboxStatus,
    probe_sandbox_backend,
)
from .netns import NetnsBackend, build_netns_argv, probe_netns
from .profiles import (
    AnalysisProfile,
    CREDENTIAL_FILENAMES,
    PACKAGE_RC_FILES,
    build_analysis_profile,
    escape_sbpl_literal,
    render_seatbelt_profile,
)
from .seatbelt import SeatbeltBackend, build_seatbelt_argv, probe_seatbelt

__all__ = [
    "AnalysisProfile",
    "AppContainerBackend",
    "AppContainerPolicy",
    "BackendName",
    "BlockedEvent",
    "BwrapBackend",
    "CREDENTIAL_FILENAMES",
    "CommandOutcome",
    "DEFAULT_ALLOWED_DOMAINS",
    "DEFAULT_DENY_READ_PATHS",
    "DEFAULT_DENY_WRITE_GLOBS",
    "EGRESS_DENYLIST",
    "EGRESS_GROUPS",
    "EgressDecision",
    "EgressMode",
    "EgressProxy",
    "IsolationLevel",
    "KernelSpawnAdapter",
    "Level",
    "Mount",
    "NetnsBackend",
    "PACKAGE_RC_FILES",
    "ProbeReport",
    "SandboxBackendName",
    "SandboxConfig",
    "SandboxConfigError",
    "SandboxError",
    "SandboxManager",
    "SandboxMode",
    "SandboxSession",
    "SandboxStatus",
    "SandboxUnavailable",
    "SeatbeltBackend",
    "UpstreamProxy",
    "build_analysis_profile",
    "build_appcontainer_policy",
    "build_bwrap_argv",
    "build_netns_argv",
    "build_seatbelt_argv",
    "domain_matches",
    "escape_sbpl_literal",
    "evaluate_egress",
    "extract_blocked_host",
    "format_egress_blocked_hint",
    "host_user",
    "is_ip_allowed",
    "parse_upstream_proxy",
    "probe_appcontainer",
    "probe_bwrap",
    "probe_netns",
    "probe_sandbox_backend",
    "probe_seatbelt",
    "render_seatbelt_profile",
]
