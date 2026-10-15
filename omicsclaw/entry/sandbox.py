"""The native OS sandbox, as one deployment uses it: started, degraded, or off.

:func:`open_sandbox` probes and starts the native OS sandbox backend
(Linux ``bwrap`` / ``netns``, macOS ``seatbelt``, Windows ``appcontainer``)
and localhost :class:`~omicsclaw.sandbox.EgressProxy` for a deployment's
workspace, returning a :class:`SandboxBinding`. A sandbox that cannot start
leaves the deployment running on this machine — logged as a warning and
stated in the system prompt — unless
:attr:`~omicsclaw.entry.config.AppConfig.sandbox_required` is set, in
which case start-up fails.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from omicsclaw.context import Section, static
from omicsclaw.sandbox import (
    BackendName,
    EGRESS_DENYLIST,
    EgressMode,
    KernelSpawnAdapter,
    Level,
    ProbeReport,
    SandboxConfig,
    SandboxError,
    SandboxManager,
    SandboxMode,
    SandboxStatus,
    domain_matches,
    extract_blocked_host,
    format_egress_blocked_hint,
    is_ip_allowed,
    probe_sandbox_backend,
)
from omicsclaw.schema import ToolDefinition
from omicsclaw.tools import ApprovalMode, RiskLevel, ToolPolicy, require_approval
from omicsclaw.tools.builtin.bash import without_control_credentials

from .config import AppConfig

__all__ = [
    "EGRESS_GRANTS_FILENAME",
    "_APPROVED_NETWORK_SCOPE",
    "EgressMode",
    "RequestNetworkAccessTool",
    "SandboxBinding",
    "SandboxSpawnWrapper",
    "SandboxStatus",
    "append_egress_blocked_hints",
    "bash_policy",
    "load_persisted_egress_grants",
    "open_sandbox",
    "python_policy",
    "sandbox_section",
    "unstarted_sandbox",
]

_log = logging.getLogger(__name__)

_REASON_CHARS = 300
EGRESS_GRANTS_FILENAME = "sandbox_egress_grants.json"
EGRESS_GRANTS_HYPHEN_FILENAME = "sandbox-egress-grants.json"
_APPROVED_NETWORK_SCOPE: ContextVar[str | None] = ContextVar(
    "_APPROVED_NETWORK_SCOPE", default=None
)
SandboxSpawnWrapper = KernelSpawnAdapter


def _candidate_grant_paths(path: Path) -> tuple[Path, ...]:
    if path.name == EGRESS_GRANTS_FILENAME:
        return (path, path.with_name(EGRESS_GRANTS_HYPHEN_FILENAME))
    if path.name == EGRESS_GRANTS_HYPHEN_FILENAME:
        return (path, path.with_name(EGRESS_GRANTS_FILENAME))
    return (path,)


def load_persisted_egress_grants(path: Path) -> tuple[str, ...]:
    """Load persisted user-approved egress domains from *path* (and hyphenated alias)."""
    found: list[str] = []
    for candidate in _candidate_grant_paths(path):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("domains"), list):
                for d in data["domains"]:
                    norm = str(d).strip().lower().rstrip(".")
                    if norm and norm not in found:
                        found.append(norm)
        except Exception:  # noqa: BLE001
            continue
    return tuple(found)


def _write_persisted_egress_grants(path: Path, domains: list[str]) -> None:
    payload = json.dumps({"domains": sorted(set(domains))}, indent=2) + "\n"
    for candidate in _candidate_grant_paths(path):
        with contextlib.suppress(OSError):
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(payload, encoding="utf-8")


def _save_persisted_egress_grant(path: Path, domain: str) -> None:
    existing = list(load_persisted_egress_grants(path))
    norm = domain.strip().lower().rstrip(".")
    if norm and norm not in existing:
        existing.append(norm)
    _write_persisted_egress_grants(path, existing)


def _remove_persisted_egress_grant(path: Path, domain: str) -> None:
    norm = domain.strip().lower().rstrip(".")
    existing = [d for d in load_persisted_egress_grants(path) if d != norm]
    _write_persisted_egress_grants(path, existing)


@dataclass(frozen=True, slots=True)
class SandboxBinding:
    """What one deployment got when it asked for a sandbox."""

    mode: SandboxMode = SandboxMode.OFF
    environment: Any = None
    """Where ``bash`` runs; ``None`` means on this machine."""

    manager: SandboxManager | None = None
    """Owns the native OS sandbox and egress proxy; ``None`` unless started."""

    config: SandboxConfig | None = None
    unavailable: str = ""
    """Why a requested sandbox is not in use. Empty when off or running."""

    backend: BackendName = BackendName.NONE
    level: Level = Level.OFF
    egress: EgressMode = EgressMode.NONE
    report: ProbeReport | None = None
    granted_domains_path: Path | None = field(default=None, compare=False)
    _session_grants: set[str] = field(
        default_factory=set, compare=False, hash=False, repr=False
    )
    _revoked_domains: set[str] = field(
        default_factory=set, compare=False, hash=False, repr=False
    )

    @property
    def active(self) -> bool:
        return self.environment is not None

    @property
    def degraded(self) -> bool:
        """A sandbox was asked for and ``bash`` runs on this machine anyway."""
        return self.mode is not SandboxMode.OFF and not self.active

    @property
    def effective_level(self) -> Level:
        if not self.active:
            return Level.OFF
        if self.report is not None:
            return self.report.level
        if self.level is not Level.OFF:
            return self.level
        return Level.ISOLATED

    @property
    def effective_egress(self) -> EgressMode:
        if self.config is not None:
            if self.config.network and self.config.network.strip().lower() not in (
                "",
                "none",
                "proxy",
            ):
                return EgressMode.OPEN
            return self.config.egress
        return self.egress

    @property
    def isolates_network(self) -> bool:
        """``True`` only while commands have no arbitrary outbound network access."""
        if not self.active:
            return False
        if self.config is not None:
            return self.config.isolates_network
        return self.effective_egress in (EgressMode.NONE, EgressMode.PROXY)

    @property
    def can_auto_approve(self) -> bool:
        """``True`` only when full filesystem + network isolation is active."""
        return (
            self.active
            and self.effective_level == Level.ISOLATED
            and self.isolates_network
        )

    @property
    def covers(self) -> tuple[str, ...]:
        if not self.active:
            return ()
        if self.config is None or self.config.sandbox_kernels:
            return ("bash", "python", "r")
        return ("bash",)

    def as_kernel_spawn_wrapper(self) -> KernelSpawnAdapter | None:
        if self.manager is not None:
            return self.manager.as_kernel_spawn_wrapper()
        return None

    def status(self) -> SandboxStatus:
        if self.manager is not None:
            return self.manager.status()
        summary = (
            self.report.summary
            if self.report is not None
            else ("active" if self.active else (self.unavailable or "sandbox off"))
        )
        return SandboxStatus(
            active=self.active,
            backend=self.backend,
            level=self.effective_level,
            egress=self.effective_egress if self.active else EgressMode.OPEN,
            summary=summary,
            covers=self.covers,
            degraded_reason=self.unavailable,
            fix_hint=self.report.fix_hint if self.report is not None else "",
        )

    def snapshot(self, *, auto_approve_configured: bool = True) -> dict[str, Any]:
        """Structured snapshot of sandbox status, domains, and recent blocked events."""
        st = self.status()
        persisted = (
            sorted(set(load_persisted_egress_grants(self.granted_domains_path)))
            if self.granted_domains_path is not None
            else []
        )
        allowed_set = (
            set(st.allowed_domains) | set(self._session_grants) | set(persisted)
        )
        allowed_set -= self._revoked_domains
        egress_groups = (
            list(self.config.egress_groups) if self.config is not None else []
        )
        recent_blocked_list: list[dict[str, Any]] = []
        if (
            self.manager is not None
            and getattr(self.manager, "proxy", None) is not None
        ):
            proxy = self.manager.proxy
            for ev in getattr(proxy, "recent_blocked_events", ()):
                recent_blocked_list.append(
                    {
                        "host": ev.host,
                        "domain": ev.host,
                        "port": ev.port,
                        "reason": ev.reason,
                        "timestamp": ev.timestamp,
                    }
                )
        reason = st.degraded_reason or self.unavailable or ""
        fix_hint = st.fix_hint or (
            self.report.fix_hint if self.report is not None else ""
        )
        return {
            "backend": st.backend.value,
            "level": st.level.value,
            "coverage": list(st.covers),
            "covers": list(st.covers),
            "egress": st.egress.value,
            "active": st.active,
            "degraded": self.degraded,
            "reason": reason,
            "fix_hint": fix_hint,
            "summary": st.summary,
            "auto_approve_active": bool(
                self.can_auto_approve and auto_approve_configured
            ),
            "can_auto_approve": self.can_auto_approve,
            "egress_groups": egress_groups,
            "allowed_domains": sorted(allowed_set),
            "persisted_domains": persisted,
            "session_domains": sorted(self._session_grants),
            "recent_blocked": recent_blocked_list,
            "warnings": [w for w in (reason, fix_hint) if w]
            if (self.degraded or st.level == Level.NET_ONLY)
            else [],
        }

    def allow_domain(self, domain: str, *, persist: bool = False) -> dict[str, Any]:
        """Grant outbound access to *domain* for the session (and optionally persist)."""
        norm = domain.strip().lower().rstrip(".")
        if not norm or any(
            ch in norm for ch in (" ", "\t", "\r", "\n", "/", "\\", "@", "?")
        ):
            raise ValueError(f"Invalid domain '{domain}'")
        check_host = norm[2:] if norm.startswith("*.") else norm
        if not check_host or "." not in check_host and check_host != "localhost":
            raise ValueError(f"Invalid domain '{domain}'")
        if domain_matches(check_host, EGRESS_DENYLIST):
            raise ValueError(
                f"Domain '{norm}' is on the non-overridable exfiltration "
                "denylist and cannot be granted."
            )
        self._revoked_domains.discard(norm)
        self._session_grants.add(norm)
        if self.manager is not None:
            self.manager.allow_domain_for_session(norm)
        if persist and self.granted_domains_path is not None:
            _save_persisted_egress_grant(self.granted_domains_path, norm)
        return self.snapshot()

    def revoke_domain(self, domain: str) -> dict[str, Any]:
        """Revoke outbound access to *domain* from memory and persisted workspace grants."""
        norm = domain.strip().lower().rstrip(".")
        if norm:
            self._session_grants.discard(norm)
            self._revoked_domains.add(norm)
            if self.manager is not None:
                if hasattr(self.manager, "revoke_domain"):
                    self.manager.revoke_domain(norm)
                granted_list = getattr(self.manager, "granted_domains", None)
                if isinstance(granted_list, list):
                    while norm in granted_list:
                        granted_list.remove(norm)
            if self.granted_domains_path is not None:
                _remove_persisted_egress_grant(self.granted_domains_path, norm)
        return self.snapshot()

    def drain_blocked_egress_hints(self, output: str = "") -> tuple[str, ...]:
        """Drain recent blocked egress notices from the proxy or *output* text."""
        hints: list[str] = []
        seen_hosts: set[str] = set()
        if self.manager is not None and self.manager.proxy is not None:
            for ev in self.manager.proxy.drain_recent_blocked_events():
                if ev.host not in seen_hosts:
                    seen_hosts.add(ev.host)
                    hints.append(format_egress_blocked_hint(ev.host, ev.reason))
        if output:
            extracted = extract_blocked_host(output)
            if extracted and extracted not in seen_hosts:
                seen_hosts.add(extracted)
                hints.append(format_egress_blocked_hint(extracted))
        return tuple(hints)

    async def aclose(self) -> None:
        """Stop the sandbox and its egress proxy, if active."""
        if self.manager is not None:
            await self.manager.aclose()


def append_egress_blocked_hints(
    binding: SandboxBinding | None,
    output: str,
) -> str:
    """Append any blocked-egress hints to *output* if not already present."""
    if binding is None or not binding.active:
        return output
    hints = binding.drain_blocked_egress_hints(output)
    missing = [h for h in hints if h not in output]
    if not missing:
        return output
    sep = "\n" if output and not output.endswith("\n") else ""
    return output + sep + "\n".join(missing) + "\n"


async def open_sandbox(
    config: AppConfig,
    *,
    on_change: Any = None,
    runner: Any = None,
) -> SandboxBinding:
    """Start the native OS sandbox *config* asks for, over its workspace."""
    del on_change, runner
    sandbox_config = config.sandbox_config()
    if sandbox_config is None:
        return SandboxBinding(
            granted_domains_path=config.state_dir() / EGRESS_GRANTS_FILENAME
        )

    grants_path = config.state_dir() / EGRESS_GRANTS_FILENAME
    persisted = load_persisted_egress_grants(grants_path)
    if persisted:
        merged_allow = tuple(dict.fromkeys((*sandbox_config.egress_allow, *persisted)))
        sandbox_config = dataclasses.replace(
            sandbox_config,
            egress_allow=merged_allow,
            extra_allowed_domains=sandbox_config.extra_allowed_domains
            | frozenset(persisted),
        )

    if sandbox_config.egress == EgressMode.PROXY:
        _warn_unlisted_private_mirrors(sandbox_config)

    report = probe_sandbox_backend(sandbox_config.backend)
    if not report.available:
        reason = report.summary
        if report.reason:
            reason = f"{reason} ({report.reason})"
        return _degraded(config, sandbox_config, reason, report=report)

    try:
        manager = SandboxManager(
            config.workspace,
            config=sandbox_config,
            report=report,
            env_provider=without_control_credentials,
        )
        await manager.start()
    except SandboxError as exc:
        return _degraded(config, sandbox_config, str(exc), report=report)

    _log.info(
        "sandbox running: backend=%s level=%s egress=%s",
        report.backend.value,
        report.level.value,
        sandbox_config.egress.value,
    )
    return SandboxBinding(
        mode=config.sandbox,
        environment=manager,
        manager=manager,
        config=sandbox_config,
        backend=report.backend,
        level=report.level,
        egress=sandbox_config.egress,
        report=report,
        granted_domains_path=grants_path,
    )


def unstarted_sandbox(config: AppConfig) -> SandboxBinding:
    """The binding for a deployment built without :func:`open_sandbox`."""
    if config.sandbox is SandboxMode.OFF:
        return SandboxBinding(
            granted_domains_path=config.state_dir() / EGRESS_GRANTS_FILENAME
        )
    return _degraded(
        config,
        config.sandbox_config(),
        "this deployment was built without starting it (use open_app)",
    )


def sandbox_section(binding: SandboxBinding) -> Section | None:
    """The system-prompt section describing where ``bash`` and kernels run."""
    if binding.mode is SandboxMode.OFF:
        return None
    if binding.active and binding.config is not None:
        text = _running_text(binding)
    else:
        fix_hint = binding.report.fix_hint if binding.report is not None else ""
        text = _degraded_text(binding.unavailable, fix_hint=fix_hint)
    return Section("sandbox", "## Execution sandbox", static(text))


def bash_policy(
    binding: SandboxBinding,
    policy: ToolPolicy,
    *,
    auto_approve: bool,
) -> ToolPolicy:
    """Relax *policy* to ``ApprovalMode.AUTO`` when full isolation + network control hold."""
    if not auto_approve:
        return policy
    if not binding.can_auto_approve:
        if not binding.active:
            why = "the sandbox is not running"
        elif binding.effective_level != Level.ISOLATED:
            why = f"the sandbox isolation level is '{binding.effective_level.value}' (not 'isolated')"
        else:
            why = "the sandbox network is open"
        _log.warning("sandbox_auto_approve ignored: %s; bash still asks", why)
        return policy
    return dataclasses.replace(policy, approval_mode=ApprovalMode.AUTO)


def python_policy(
    binding: SandboxBinding,
    policy: ToolPolicy,
    *,
    auto_approve: bool,
) -> ToolPolicy:
    """Relax ``python`` kernel tool *policy* under the same invariants as :func:`bash_policy`."""
    if not auto_approve:
        return policy
    if not binding.can_auto_approve or "python" not in binding.covers:
        return policy
    return dataclasses.replace(policy, approval_mode=ApprovalMode.AUTO)


class RequestNetworkAccessTool:
    """Built-in tool allowing the agent to request user approval for a blocked egress domain."""

    policy = ToolPolicy(
        risk_level=RiskLevel.MEDIUM,
        approval_mode=ApprovalMode.ASK,
        read_only=False,
        concurrency_safe=False,
        writes_workspace=False,
        touches_network=True,
        prompts_for_itself=True,
        ask_every_time=True,
        tags=frozenset({"sandbox", "network"}),
    )

    def __init__(self, binding: SandboxBinding) -> None:
        self._binding = binding

    def _grant_domain(self, host: str, *, persist: bool = False) -> dict[str, Any]:
        return self._binding.allow_domain(host, persist=persist)

    @property
    def name(self) -> str:
        return "request_network_access"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Request user approval to allow outbound HTTP/HTTPS access to a "
                "domain that was blocked by the sandbox egress proxy. Requires "
                "explicit human approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Exact domain name or wildcard pattern to allow "
                            "(e.g. 'api.custom-biobank.org')."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why outbound access to this domain is required for "
                            "the current scientific task."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["session", "always"],
                        "description": (
                            "'session' (default) grants access for the current "
                            "session only; 'always' persists the grant in the "
                            "workspace."
                        ),
                    },
                },
                "required": ["domain", "reason"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: str | Mapping[str, Any]) -> str:
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON arguments: {exc}") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError("arguments must be a JSON object")
            raw_args = arguments
        else:
            decoded = arguments
            raw_args = json.dumps(dict(arguments))

        raw_domain = str(decoded.get("domain", "") or "").strip().lower().rstrip(".")
        reason = str(decoded.get("reason", "") or "").strip()
        scope = str(decoded.get("scope", "session") or "session").strip().lower()
        if not raw_domain:
            raise ValueError("domain must be non-empty")
        if not reason:
            raise ValueError("reason must be non-empty")
        if scope not in ("once", "session", "always"):
            raise ValueError("scope must be 'once', 'session' or 'always'")
        check_host = raw_domain[2:] if raw_domain.startswith("*.") else raw_domain
        if domain_matches(check_host, EGRESS_DENYLIST):
            raise ValueError(
                f"Domain '{raw_domain}' is on the non-overridable exfiltration "
                "denylist and cannot be granted."
            )
        decision = await require_approval(
            self.name,
            raw_args,
            policy=self.policy,
            reason=f"Allow outbound network access to '{raw_domain}' ({scope}): {reason}",
        )
        ctx_scope = _APPROVED_NETWORK_SCOPE.get()
        if ctx_scope is not None:
            _APPROVED_NETWORK_SCOPE.set(None)
        decision_scope = getattr(decision, "scope", None)
        if decision_scope in ("once", "session", "always"):
            effective_scope = str(decision_scope)
        elif ctx_scope in ("once", "session", "always"):
            effective_scope = ctx_scope
        else:
            effective_scope = scope
        persist = effective_scope == "always"
        self._grant_domain(raw_domain, persist=persist)
        scope_label = "persisted in workspace" if persist else "for this session"
        return (
            f"Granted outbound network access to '{raw_domain}' ({scope_label}). "
            "You may now retry the command."
        )


def _degraded(
    config: AppConfig,
    sandbox_config: SandboxConfig | None,
    reason: str,
    *,
    report: ProbeReport | None = None,
) -> SandboxBinding:
    if config.sandbox_required:
        raise SandboxError(
            f"sandbox_required is set and the sandbox is unavailable: {reason}"
        )
    _log.warning(
        "sandbox unavailable, bash runs on this machine without isolation: %s",
        reason,
    )
    return SandboxBinding(
        mode=config.sandbox,
        config=sandbox_config,
        unavailable=reason,
        report=report,
        granted_domains_path=config.state_dir() / EGRESS_GRANTS_FILENAME,
    )


def _running_text(binding: SandboxBinding) -> str:
    config = binding.config
    assert config is not None
    backend_label = (
        f"native OS sandbox (`{binding.backend.value}`, level `{binding.effective_level.value}`)"
        if binding.backend != BackendName.NONE
        else (
            f"isolated sandbox (`{config.image}`)"
            if config.image
            else "native OS sandbox"
        )
    )
    covered = ", ".join(f"`{c}`" for c in binding.covers) or "`bash`"
    lines = [
        f"- {covered} runs inside an {backend_label}"
        + (
            f" (image `{config.image}`)"
            if config.image and binding.backend != BackendName.NONE
            else ""
        )
        + ", not directly on the unisolated host.",
        "- Its working directory is the workspace, mounted at the same path, "
        "so files it writes are the files read_file, write_file and "
        "edit_file see.",
    ]
    if binding.effective_level == Level.NET_ONLY:
        lines.append(
            "- Partial isolation (`net_only`): network and PID namespaces are "
            "isolated, but full filesystem isolation requires `bubblewrap` (`bwrap`)."
        )
    if binding.effective_egress == EgressMode.PROXY:
        groups = ", ".join(config.egress_groups) if config.egress_groups else "default"
        lines.append(
            f"- Network egress is filtered through a default-deny localhost proxy "
            f"(allowed groups: {groups}; no network access to unapproved hosts). "
            "If a required scientific domain is blocked with HTTP 403, call "
            "`request_network_access` to request user approval."
        )
    elif binding.isolates_network:
        lines.append(
            "- The sandbox has no network access: downloads, package "
            "installs and remote APIs fail by design."
        )
    else:
        net_name = config.network or binding.effective_egress.value
        lines.append(f"- The sandbox can reach network `{net_name}`.")
    if config.read_only_mounts:
        mounts = ", ".join(str(path) for path in config.read_only_mounts)
        lines.append(f"- Also visible, read-only: {mounts}.")
    lines.append(
        "- Sensitive host credentials (`~/.ssh`, `~/.aws`, etc.) and workspace "
        "persistence files (`.git/hooks`, `.omicsclaw`, etc.) are protected, "
        "and anything written to `/tmp` is lost when the session ends."
    )
    lines.append(
        "- Host-side tools (`web_fetch`, `web_search`) run in the host process "
        "outside the sandbox network allowlist."
    )
    return "\n".join(lines)


def _degraded_text(reason: str, *, fix_hint: str = "") -> str:
    if len(reason) > _REASON_CHARS:
        reason = reason[: _REASON_CHARS - 1] + "…"
    lines = [
        f"- A sandbox was requested but is not running: {reason}",
        "- `bash` therefore runs directly on this machine, as the user "
        "running this agent, with no isolation. Ask the user before "
        "anything destructive or irreversible.",
    ]
    if fix_hint:
        lines.append(f"- Remediation hint: {fix_hint}")
    return "\n".join(lines)


def _warn_unlisted_private_mirrors(
    sandbox_config: SandboxConfig, *, environment: Mapping[str, str] | None = None
) -> None:
    """Warn if host pip/conda config points at a private-IP mirror not in ``egress_endpoints``."""
    import re
    from urllib.parse import urlparse

    url_pattern = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
    candidate_urls: list[str] = []
    diagnostic_environment = (
        without_control_credentials() if environment is None else environment
    )
    for env_key in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL"):
        val = diagnostic_environment.get(env_key, "")
        if val:
            candidate_urls.extend(url_pattern.findall(val))

    home = Path.home()
    config_files = (
        home / ".config" / "pip" / "pip.conf",
        home / ".pip" / "pip.conf",
        Path("/etc/pip.conf"),
        home / ".condarc",
    )
    for cfg in config_files:
        try:
            if cfg.is_file():
                candidate_urls.extend(
                    url_pattern.findall(
                        cfg.read_text(encoding="utf-8", errors="ignore")
                    )
                )
        except OSError:
            continue

    seen: set[tuple[str, int]] = set()
    for raw_url in candidate_urls:
        try:
            parsed = urlparse(raw_url)
            host = (parsed.hostname or "").strip().lower()
            if not host:
                continue
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except Exception:  # noqa: BLE001
            continue
        if (host, port) in seen:
            continue
        seen.add((host, port))
        import ipaddress

        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue
        allowed_eps = {ep.strip().lower() for ep in sandbox_config.egress_endpoints}
        if (
            not is_ip_allowed(host, sandbox_config.allow_private_cidr)
            and f"{host}:{port}" not in allowed_eps
            and host not in allowed_eps
        ):
            _log.warning(
                "Detected private mirror endpoint %s:%d in pip/conda configuration, "
                "which is blocked by the sandbox egress proxy by default. "
                "Set OMICSCLAW_SANDBOX_EGRESS_ENDPOINTS=%s:%d to explicitly allow it.",
                host,
                port,
                host,
                port,
            )
