"""Apply the configured policy to outbound HTTP and HTTPS connections.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import re
import socket
import time
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_ALLOWED_DOMAINS

__all__ = [
    "BlockedEvent",
    "EGRESS_DENYLIST",
    "EGRESS_GROUPS",
    "EgressDecision",
    "EgressProxy",
    "UpstreamProxy",
    "domain_matches",
    "evaluate_egress",
    "extract_blocked_host",
    "format_egress_blocked_hint",
    "is_ip_allowed",
    "normalize_host",
    "parse_upstream_proxy",
]

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_MAX_HEADER_BYTES = 65536
_PIPE_CHUNK = 65536
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)

EGRESS_GROUPS: dict[str, tuple[str, ...]] = {
    "pkg": (
        "pypi.org",
        "files.pythonhosted.org",
        "conda.anaconda.org",
        "repo.anaconda.com",
        "prefix.dev",
        "*.prefix.dev",
        "cran.r-project.org",
        "cloud.r-project.org",
        "bioconductor.org",
        "*.bioconductor.org",
        "registry.npmjs.org",
        "github.com",
        "*.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "release-assets.githubusercontent.com",
    ),
    "literature": (
        "pubmed.ncbi.nlm.nih.gov",
        "eutils.ncbi.nlm.nih.gov",
        "europepmc.org",
        "*.europepmc.org",
        "api.semanticscholar.org",
        "semanticscholar.org",
        "api.crossref.org",
        "crossref.org",
        "api.openalex.org",
        "openalex.org",
        "biorxiv.org",
        "*.biorxiv.org",
        "medrxiv.org",
        "*.medrxiv.org",
        "arxiv.org",
        "*.arxiv.org",
        "doi.org",
        "*.doi.org",
    ),
    "nih": (
        "ncbi.nlm.nih.gov",
        "*.ncbi.nlm.nih.gov",
        "nih.gov",
        "*.nih.gov",
        "sra-pub-run-odp.s3.amazonaws.com",
    ),
    "genomics": (
        "ensembl.org",
        "*.ensembl.org",
        "rest.ensembl.org",
        "ftp.ensembl.org",
        "ebi.ac.uk",
        "*.ebi.ac.uk",
        "ftp.ebi.ac.uk",
        "ucsc.edu",
        "*.ucsc.edu",
        "hgdownload.soe.ucsc.edu",
        "api.genome.ucsc.edu",
        "gencodegenes.org",
        "*.gencodegenes.org",
        "genenames.org",
        "*.genenames.org",
        "geneontology.org",
        "*.geneontology.org",
        "kegg.jp",
        "*.kegg.jp",
        "rest.kegg.jp",
        "reactome.org",
        "*.reactome.org",
        "string-db.org",
        "*.string-db.org",
        "cellxgene.cziscience.com",
        "*.cellxgene.cziscience.com",
        "api.cellxgene.cziscience.com",
        "datasets.cellxgene.cziscience.com",
        "10xgenomics.com",
        "*.10xgenomics.com",
        "cf.10xgenomics.com",
        "scverse.org",
        "*.scverse.org",
        "figshare.com",
        "*.figshare.com",
        "ndownloader.figshare.com",
        "zenodo.org",
        "*.zenodo.org",
        "huggingface.co",
        "*.huggingface.co",
    ),
    "proteomics": (
        "uniprot.org",
        "*.uniprot.org",
        "rest.uniprot.org",
        "rcsb.org",
        "*.rcsb.org",
        "files.rcsb.org",
        "data.rcsb.org",
        "alphafold.ebi.ac.uk",
        "pride.ebi.ac.uk",
        "proteomecentral.proteomexchange.org",
    ),
    "clinical": (
        "clinicaltrials.gov",
        "*.clinicaltrials.gov",
        "gdc.cancer.gov",
        "api.gdc.cancer.gov",
        "cbioportal.org",
        "*.cbioportal.org",
        "orphanet.org",
        "omim.org",
        "clinvar.ncbi.nlm.nih.gov",
        "pharmgkb.org",
        "api.fda.gov",
    ),
    "bio": (
        "pubmed.ncbi.nlm.nih.gov",
        "eutils.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
        "*.ncbi.nlm.nih.gov",
        "ensembl.org",
        "*.ensembl.org",
        "ebi.ac.uk",
        "*.ebi.ac.uk",
        "ucsc.edu",
        "*.ucsc.edu",
        "uniprot.org",
        "*.uniprot.org",
        "cellxgene.cziscience.com",
        "*.cellxgene.cziscience.com",
        "10xgenomics.com",
        "*.10xgenomics.com",
        "scverse.org",
        "*.scverse.org",
        "huggingface.co",
        "*.huggingface.co",
        "zenodo.org",
        "*.zenodo.org",
    ),
    "git": (
        "github.com",
        "*.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "release-assets.githubusercontent.com",
    ),
    "registries": (
        "pypi.org",
        "files.pythonhosted.org",
        "conda.anaconda.org",
        "repo.anaconda.com",
        "prefix.dev",
        "*.prefix.dev",
        "cran.r-project.org",
        "cloud.r-project.org",
        "bioconductor.org",
        "*.bioconductor.org",
        "registry.npmjs.org",
    ),
    "default": tuple(DEFAULT_ALLOWED_DOMAINS),
}
"""Preset domain allowlist groups aligned with Claude-Science ``UOe``."""

EGRESS_DENYLIST: frozenset[str] = frozenset(
    {
        "webhook.site",
        "*.webhook.site",
        "pastebin.com",
        "*.pastebin.com",
        "hastebin.com",
        "ghostbin.com",
        "0x0.st",
        "transfer.sh",
        "file.io",
        "ngrok.io",
        "*.ngrok.io",
        "ngrok-free.app",
        "*.ngrok-free.app",
        "trycloudflare.com",
        "*.trycloudflare.com",
        "localtunnel.me",
        "upload.pypi.org",
        "test.pypi.org",
        "gist.github.com",
        "hooks.slack.com",
        "discord.com",
        "discordapp.com",
        "api.telegram.org",
        "requestbin.com",
        "pipedream.net",
        "*.pipedream.net",
        "beeceptor.com",
        "interact.sh",
        "*.interact.sh",
        "oastify.com",
        "*.oastify.com",
        "burpcollaborator.net",
    }
)
"""Non-overridable exfiltration denylist aligned with Claude-Science ``D1``."""


@dataclass(frozen=True, slots=True)
class BlockedEvent:
    """Record of a connection attempt rejected by :class:`EgressProxy`."""

    host: str
    port: int
    reason: str
    timestamp: float


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """Outcome of :func:`evaluate_egress`."""

    allowed: bool
    reason: str
    target_ip: str = ""

    def __bool__(self) -> bool:
        return self.allowed

    def __iter__(self) -> Iterator[object]:
        yield self.allowed
        yield self.reason


@dataclass(frozen=True, slots=True)
class UpstreamProxy:
    """Upstream corporate/clash HTTP proxy to chain allowed egress through."""

    host: str
    port: int
    no_proxy: tuple[str, ...] = ()


def normalize_host(host: str) -> str:
    """Lowercase *host*, stripping brackets and trailing dots."""
    cleaned = host.strip().lower()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return cleaned.rstrip(".")


def _is_valid_host(target: str) -> bool:
    if not target or len(target) > 253:
        return False
    if any(ch in target for ch in (" ", "\t", "\r", "\n", "/", "\\", "@", "?")):
        return False
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(target))


def domain_matches(host: str, patterns: Iterable[str]) -> bool:
    """Return True when *host* matches any domain pattern in *patterns*.

    Supports exact matches (``ncbi.nlm.nih.gov``) and subdomain wildcards
    (``*.ncbi.nlm.nih.gov``, which matches ``eutils.ncbi.nlm.nih.gov`` and
    ``ncbi.nlm.nih.gov``).
    """
    target = normalize_host(host)
    if not target:
        return False
    for raw in patterns:
        pat = normalize_host(raw)
        if not pat:
            continue
        if pat == "*":
            return True
        if pat.startswith("*."):
            suffix = pat[2:]
            if target == suffix or target.endswith("." + suffix):
                return True
        elif target == pat:
            return True
    return False


def _matches_denylist(target: str) -> bool:
    for item in EGRESS_DENYLIST:
        norm = normalize_host(item)
        if norm.startswith("*."):
            norm = norm[2:]
        if target == norm or target.endswith("." + norm):
            return True
    return False


def _matches_endpoint(target: str, port: int, endpoints: Sequence[str]) -> bool:
    host_port = f"{target}:{port}"
    for raw in endpoints:
        cleaned = raw.strip().lower()
        if not cleaned:
            continue
        if ":" in cleaned:
            ep_host, ep_port = _parse_Authority(cleaned, default_port=port)
            if normalize_host(ep_host) == target and ep_port == port:
                return True
        elif normalize_host(cleaned) == target:
            return True
        elif cleaned == host_port:
            return True
    return False


def _ip_in_cidrs(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    cidrs: Sequence[str],
) -> bool:
    for raw in cidrs:
        text = raw.strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        if ip_obj in net:
            return True
    return False


def is_ip_allowed(ip_str: str, allow_private_cidr: Sequence[str] = ()) -> bool:
    """Return True if *ip_str* is a public routable IP or in *allow_private_cidr*."""
    try:
        ip_obj = ipaddress.ip_address(normalize_host(ip_str))
    except ValueError:
        return False

    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped

    if _ip_in_cidrs(ip_obj, allow_private_cidr):
        return True

    if (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return False

    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NET:
        return False

    return True


def evaluate_egress(
    host: str,
    port: int = 443,
    *,
    egress_groups: Sequence[str] = ("pkg",),
    egress_allow: Iterable[str] = (),
    egress_endpoints: Sequence[str] = (),
    granted_domains: Iterable[str] = (),
    allowed_domains: Iterable[str] | None = None,
    allow_private_cidr: Sequence[str] = (),
    resolved_ips: Sequence[str] | None = None,
    open_mode: bool = False,
) -> EgressDecision:
    """Evaluate outbound connection ``(host, port)`` in strict §4.3 3-1 order."""
    target = normalize_host(host)
    # 1. Valid hostname / IP check
    if not _is_valid_host(target):
        return EgressDecision(False, "invalid-host")

    endpoint_matched = _matches_endpoint(target, port, egress_endpoints)

    # 2. Resolved IP check (private / loopback / link-local / metadata 169.254.169.254)
    is_literal_ip = False
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_obj = ipaddress.ip_address(target)
        is_literal_ip = True
    except ValueError:
        ip_obj = None

    ips_to_check: list[str] = []
    if is_literal_ip:
        ips_to_check.append(target)
    elif resolved_ips is not None:
        ips_to_check.extend(str(ip) for ip in resolved_ips if ip)
    elif target == "localhost" or target.endswith(".localhost"):
        ips_to_check.append("127.0.0.1")

    first_ip = ips_to_check[0] if ips_to_check else ""
    if not endpoint_matched:
        for ip_candidate in ips_to_check:
            if not is_ip_allowed(ip_candidate, allow_private_cidr):
                return EgressDecision(False, "blocked-by-resolved-ip")

    # 3. Exfiltration denylist check (never overridable)
    if _matches_denylist(target):
        return EgressDecision(False, "blocked-by-denylist")

    # 4. Port check ({80, 443} or explicit endpoint or open_mode)
    if port not in (80, 443) and not endpoint_matched and not open_mode:
        return EgressDecision(False, "blocked-by-port")

    # 5. Allowlist check (endpoints, open_mode, groups, egress_allow, granted_domains)
    if endpoint_matched or open_mode:
        return EgressDecision(True, "allowed", target_ip=first_ip or target)

    if is_literal_ip and ip_obj is not None and _ip_in_cidrs(ip_obj, allow_private_cidr):
        return EgressDecision(True, "allowed", target_ip=target)

    patterns: set[str] = set()
    if allowed_domains is not None:
        patterns.update(normalize_host(d) for d in allowed_domains if d.strip())
    for grp in egress_groups:
        grp_domains = EGRESS_GROUPS.get(grp.strip().lower(), ())
        patterns.update(grp_domains)
    patterns.update(normalize_host(d) for d in egress_allow if d.strip())
    patterns.update(normalize_host(d) for d in granted_domains if d.strip())

    if domain_matches(target, patterns):
        return EgressDecision(True, "allowed", target_ip=first_ip)

    # 6. Default deny
    return EgressDecision(False, "blocked-by-allowlist")


def parse_upstream_proxy(
    env: Mapping[str, str],
    *,
    ignore_port: int | None = None,
) -> UpstreamProxy | None:
    """Extract an upstream HTTP(S) proxy from *env*, if configured."""
    raw_url = ""
    for key in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        candidate = env.get(key, "").strip()
        if candidate:
            raw_url = candidate
            break
    if not raw_url:
        return None
    no_proxy_raw = env.get("NO_PROXY") or env.get("no_proxy") or ""
    no_proxy = tuple(
        item.strip()
        for item in no_proxy_raw.split(",")
        if item.strip()
    )
    return _coerce_upstream_proxy(raw_url, no_proxy=no_proxy, ignore_port=ignore_port)


def _coerce_upstream_proxy(
    raw_url: str,
    *,
    no_proxy: Sequence[str] = (),
    ignore_port: int | None = None,
) -> UpstreamProxy | None:
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    if "://" not in cleaned:
        cleaned = "http://" + cleaned
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 8080)
    if not host or port <= 0:
        return None
    if ignore_port is not None and host in ("127.0.0.1", "localhost", "::1") and port == ignore_port:
        return None
    return UpstreamProxy(host=host, port=port, no_proxy=tuple(no_proxy))


def _bypass_upstream(host: str, no_proxy: Sequence[str]) -> bool:
    target = normalize_host(host)
    for token in no_proxy:
        pat = normalize_host(token)
        if not pat:
            continue
        if pat == "*":
            return True
        if pat.startswith("."):
            suffix = pat[1:]
            if target == suffix or target.endswith("." + suffix):
                return True
        elif pat.startswith("*."):
            suffix = pat[2:]
            if target == suffix or target.endswith("." + suffix):
                return True
        elif target == pat or target.endswith("." + pat):
            return True
    return False


_BLOCK_INDICATORS: tuple[str, ...] = (
    "Tunnel connection failed: 403",
    "CONNECT tunnel failed",
    "blocked-by-allowlist",
    "blocked-by-denylist",
    "blocked-by-resolved-ip",
    "blocked-by-port",
    "Connection blocked by sandbox network policy",
    "Received HTTP code 403 from proxy after CONNECT",
    "[Sandbox Egress Blocked]",
    "X-Proxy-Error:",
)

_EXPLICIT_BLOCK_HOST_RE = re.compile(
    r"(?:"
    r"Connection blocked by sandbox network policy[^:\n]*:\s*([A-Za-z0-9._-]+)(?::\d+)?"
    r"|Access to '([A-Za-z0-9._-]+)' was denied"
    r"|X-Sandbox-Blocked-Domain:\s*([A-Za-z0-9._-]+)"
    r")",
    re.IGNORECASE,
)
_URL_HOST_RE = re.compile(
    r"(?:https?://|CONNECT\s+|Host:\s*)([A-Za-z0-9._-]+)(?::\d+)?",
    re.IGNORECASE,
)


def extract_blocked_host(output: str) -> str | None:
    """Extract the blocked hostname from command or cell output if a proxy 403 occurred."""
    if not output:
        return None
    if not any(indicator in output for indicator in _BLOCK_INDICATORS):
        return None
    match = _EXPLICIT_BLOCK_HOST_RE.search(output)
    if match:
        for group in match.groups():
            if group:
                host = normalize_host(group)
                if host not in ("127.0.0.1", "localhost"):
                    return host
    for url_match in _URL_HOST_RE.finditer(output):
        candidate = normalize_host(url_match.group(1))
        if candidate and candidate not in ("127.0.0.1", "localhost"):
            return candidate
    return "unknown-host"


def format_egress_blocked_hint(
    host_or_output: str,
    reason_or_recent: str | Sequence[str] = (),
) -> str:
    """Return a concise guidance note when a host or command output shows a proxy block."""
    cleaned = normalize_host(host_or_output)
    reason = reason_or_recent if isinstance(reason_or_recent, str) else ""
    if cleaned and "\n" not in host_or_output and " " not in host_or_output.strip() and _is_valid_host(cleaned):
        host: str | None = cleaned
    else:
        host = extract_blocked_host(host_or_output)
        if (host is None or host == "unknown-host") and reason_or_recent and not isinstance(reason_or_recent, str):
            host = normalize_host(str(reason_or_recent[-1]))
    if not host:
        return ""
    reason_suffix = f" ({reason})" if reason else ""
    if host == "unknown-host":
        return (
            f"[Sandbox Egress Blocked] Outbound connection was blocked by the sandbox "
            f"network policy{reason_suffix}. Call `request_network_access` with the "
            "target domain to request user approval."
        )
    return (
        f"[Sandbox Egress Blocked] Host '{host}' was blocked by the sandbox "
        f"network policy{reason_suffix}. You can call "
        f'`request_network_access(domain="{host}", reason="...")` '
        "to request user approval for this domain."
    )


class EgressProxy:
    """Asyncio HTTP/HTTPS CONNECT egress proxy with domain & SSRF filtering."""

    def __init__(
        self,
        *,
        allowed_domains: Iterable[str] | None = DEFAULT_ALLOWED_DOMAINS,
        egress_groups: Sequence[str] = ("pkg",),
        egress_allow: Sequence[str] = (),
        egress_endpoints: Sequence[str] = (),
        allow_private_cidr: Sequence[str] = (),
        upstream: UpstreamProxy | str | None = None,
        no_proxy: Sequence[str] = (),
        unix_socket_path: Path | None = None,
        proxy_auth_secret: str = "",
        open_mode: bool = False,
    ) -> None:
        self._base_allowed: set[str] | None = (
            {normalize_host(d) for d in allowed_domains if d.strip()}
            if allowed_domains is not None
            else None
        )
        self._egress_groups: tuple[str, ...] = tuple(egress_groups)
        self._egress_allow: set[str] = {normalize_host(d) for d in egress_allow if d.strip()}
        self._egress_endpoints: tuple[str, ...] = tuple(egress_endpoints)
        self._session_allowed: set[str] = set()
        self._allow_private_cidr: tuple[str, ...] = tuple(allow_private_cidr)
        if isinstance(upstream, str):
            self._upstream = _coerce_upstream_proxy(upstream, no_proxy=no_proxy)
        elif isinstance(upstream, UpstreamProxy) and no_proxy and not upstream.no_proxy:
            self._upstream = UpstreamProxy(
                host=upstream.host,
                port=upstream.port,
                no_proxy=tuple(no_proxy),
            )
        else:
            self._upstream = upstream
        self._unix_socket_path = Path(unix_socket_path) if unix_socket_path is not None else None
        self._proxy_auth_secret = proxy_auth_secret.strip()
        self._open_mode = bool(open_mode)
        self._tcp_server: asyncio.AbstractServer | None = None
        self._unix_server: asyncio.AbstractServer | None = None
        self._port: int = 0
        self._recent_blocked: list[str] = []
        self._recent_blocked_events: list[BlockedEvent] = []
        self._active_writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        """Bound localhost TCP port, or 0 if not started."""
        return self._port

    @property
    def proxy_url(self) -> str:
        """``http://127.0.0.1:<port>`` (with credentials when ``proxy_auth_secret`` is set)."""
        if self._proxy_auth_secret:
            return f"http://oc:{self._proxy_auth_secret}@127.0.0.1:{self._port}"
        return f"http://127.0.0.1:{self._port}"

    @property
    def unix_socket_path(self) -> Path | None:
        """Optional Unix domain socket path for cross-namespace bridge."""
        return self._unix_socket_path

    @property
    def granted_domains(self) -> frozenset[str]:
        """Dynamically granted domains in memory."""
        return frozenset(self._session_allowed)

    @property
    def allowed_domains(self) -> frozenset[str]:
        """All currently allowed domain patterns (base + groups + allow + session-granted)."""
        combined: set[str] = set(self._session_allowed) | set(self._egress_allow)
        if self._base_allowed is not None:
            combined |= self._base_allowed
        for grp in self._egress_groups:
            combined.update(EGRESS_GROUPS.get(grp.strip().lower(), ()))
        return frozenset(combined)

    def grant_domain(self, domain: str) -> bool:
        """Dynamically add *domain* to the in-memory allowlist (unless denylisted)."""
        cleaned = normalize_host(domain)
        if not cleaned or _matches_denylist(cleaned):
            return False
        self._session_allowed.add(cleaned)
        return True

    def allow_domain_for_session(self, domain: str) -> bool:
        """Alias for :meth:`grant_domain`."""
        return self.grant_domain(domain)

    def revoke_domain(self, domain: str) -> bool:
        """Remove *domain* from dynamic session grants and egress_allow."""
        cleaned = normalize_host(domain)
        if not cleaned:
            return False
        removed = False
        if cleaned in self._session_allowed:
            self._session_allowed.discard(cleaned)
            removed = True
        if cleaned in self._egress_allow:
            self._egress_allow.discard(cleaned)
            removed = True
        return removed

    def is_domain_allowed(self, host: str) -> bool:
        """Check whether *host* matches the allowed domains and is not denylisted."""
        target = normalize_host(host)
        if not target or _matches_denylist(target):
            return False
        return domain_matches(target, self.allowed_domains)

    @property
    def recent_blocked_events(self) -> tuple[BlockedEvent, ...]:
        """Structured list of recently blocked connection attempts."""
        return tuple(self._recent_blocked_events)

    def drain_recent_blocked_events(self) -> tuple[BlockedEvent, ...]:
        """Return and clear the structured list of recently blocked connection attempts."""
        drained = tuple(self._recent_blocked_events)
        self._recent_blocked_events.clear()
        self._recent_blocked.clear()
        return drained

    def recent_blocked(self) -> tuple[str, ...]:
        """Recently blocked domains without clearing."""
        return tuple(self._recent_blocked)

    def drain_recent_blocked(self) -> tuple[str, ...]:
        """Return and clear the list of recently blocked domains."""
        drained = tuple(self._recent_blocked)
        self._recent_blocked.clear()
        return drained

    def _record_blocked(self, host: str, port: int, reason: str) -> None:
        cleaned = normalize_host(host)
        if cleaned:
            if cleaned not in self._recent_blocked:
                self._recent_blocked.append(cleaned)
            self._recent_blocked_events.append(
                BlockedEvent(
                    host=cleaned,
                    port=port,
                    reason=reason,
                    timestamp=time.time(),
                )
            )

    async def start(self) -> None:
        """Start listening on ``127.0.0.1:0`` (and optional Unix socket)."""
        if self._tcp_server is not None:
            return
        self._tcp_server = await asyncio.start_server(
            lambda r, w: self._handle_client(r, w, via_unix=False),
            host="127.0.0.1",
            port=0,
        )
        sockets = self._tcp_server.sockets or ()
        if sockets:
            self._port = int(sockets[0].getsockname()[1])

        if self._unix_socket_path is not None and hasattr(asyncio, "start_unix_server"):
            self._unix_socket_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                self._unix_socket_path.unlink()
            self._unix_server = await asyncio.start_unix_server(
                lambda r, w: self._handle_client(r, w, via_unix=True),
                path=str(self._unix_socket_path),
            )

    async def stop(self) -> None:
        """Close listeners and active client streams."""
        for writer in list(self._active_writers):
            with contextlib.suppress(Exception):
                writer.close()
        self._active_writers.clear()

        if self._unix_server is not None:
            self._unix_server.close()
            with contextlib.suppress(Exception):
                await self._unix_server.wait_closed()
            self._unix_server = None
        if self._unix_socket_path is not None:
            with contextlib.suppress(OSError):
                self._unix_socket_path.unlink()

        if self._tcp_server is not None:
            self._tcp_server.close()
            with contextlib.suppress(Exception):
                await self._tcp_server.wait_closed()
            self._tcp_server = None
            self._port = 0

    def _verify_proxy_auth(self, header_lines: Sequence[str], *, via_unix: bool) -> bool:
        if via_unix or not self._proxy_auth_secret:
            return True
        expected_raw = f"oc:{self._proxy_auth_secret}".encode("utf-8")
        expected_b64 = base64.b64encode(expected_raw).decode("ascii")
        for line in header_lines:
            if line.lower().startswith("proxy-authorization:"):
                val = line.partition(":")[2].strip()
                if val == f"Basic {expected_b64}" or self._proxy_auth_secret in val:
                    return True
        return False

    async def _check_and_resolve(self, host: str, port: int) -> tuple[bool, str, str]:
        """Validate ``(host, port)`` against :func:`evaluate_egress` and DNS resolution."""
        target = normalize_host(host)
        if not _is_valid_host(target):
            self._record_blocked(target or host, port, "invalid-host")
            return False, "", "invalid-host"

        # Pre-check policy before resolving DNS (prevents DNS exfiltration on
        # unapproved/denylisted domains and reports accurate policy reason).
        pre_decision = evaluate_egress(
            target,
            port,
            egress_groups=self._egress_groups,
            egress_allow=self._egress_allow,
            egress_endpoints=self._egress_endpoints,
            granted_domains=self._session_allowed,
            allowed_domains=self._base_allowed,
            allow_private_cidr=self._allow_private_cidr,
            resolved_ips=None,
            open_mode=self._open_mode,
        )
        if not pre_decision.allowed:
            self._record_blocked(target, port, pre_decision.reason)
            return False, "", pre_decision.reason

        endpoint_matched = _matches_endpoint(target, port, self._egress_endpoints)
        use_upstream = (
            self._upstream is not None
            and not _bypass_upstream(target, self._upstream.no_proxy)
        )

        # Resolve DNS for allowed non-IP hostnames to enforce SSRF IP filtering
        resolved_ips: list[str] = []
        is_literal_ip = False
        try:
            ipaddress.ip_address(target)
            is_literal_ip = True
            resolved_ips = [target]
        except ValueError:
            is_literal_ip = False

        if not is_literal_ip:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(target, port, type=socket.SOCK_STREAM)
                for family, _, _, _, sockaddr in infos:
                    if family in (socket.AF_INET, socket.AF_INET6) and sockaddr:
                        resolved_ips.append(str(sockaddr[0]))
            except OSError:
                if not use_upstream and not endpoint_matched:
                    self._record_blocked(target, port, "dns-resolution-failed")
                    return False, "", "dns-resolution-failed"

        decision = evaluate_egress(
            target,
            port,
            egress_groups=self._egress_groups,
            egress_allow=self._egress_allow,
            egress_endpoints=self._egress_endpoints,
            granted_domains=self._session_allowed,
            allowed_domains=self._base_allowed,
            allow_private_cidr=self._allow_private_cidr,
            resolved_ips=resolved_ips if resolved_ips else None,
            open_mode=self._open_mode,
        )
        if not decision.allowed:
            self._record_blocked(target, port, decision.reason)
            return False, "", decision.reason

        connect_target = (
            target
            if (use_upstream or not decision.target_ip)
            else decision.target_ip
        )
        return True, connect_target, decision.reason

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        via_unix: bool = False,
    ) -> None:
        self._active_writers.add(writer)
        remote_writer: asyncio.StreamWriter | None = None
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=15.0,
            )
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            OSError,
        ):
            self._close_writer(writer)
            return

        try:
            if len(raw_head) > _MAX_HEADER_BYTES:
                await self._send_error(writer, 431, "Request Header Fields Too Large", "")
                return

            header_text = raw_head.decode("latin-1", errors="replace")
            lines = header_text.split("\r\n")
            if not lines or not lines[0]:
                return
            parts = lines[0].split(" ")
            if len(parts) < 3:
                await self._send_error(writer, 400, "Bad Request", "Malformed request line")
                return

            if not self._verify_proxy_auth(lines[1:], via_unix=via_unix):
                await self._send_auth_required(writer)
                return

            method, request_target, version = parts[0].upper(), parts[1], parts[2]
            if method == "CONNECT":
                host, port = _parse_Authority(request_target, default_port=443)
                allowed, connect_target, reason = await self._check_and_resolve(host, port)
                if not allowed:
                    await self._send_blocked(writer, host, port, reason)
                    return
                remote_reader, remote_writer = await self._open_tunnel(
                    host, connect_target, port
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await _pipe_bidirectional(reader, writer, remote_reader, remote_writer)
            else:
                parsed = urllib.parse.urlsplit(request_target)
                host = parsed.hostname or _extract_host_header(lines[1:])
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if not host:
                    await self._send_error(writer, 400, "Bad Request", "Missing target host")
                    return
                allowed, connect_target, reason = await self._check_and_resolve(host, port)
                if not allowed:
                    await self._send_blocked(writer, host, port, reason)
                    return

                use_upstream = (
                    self._upstream is not None
                    and not _bypass_upstream(host, self._upstream.no_proxy)
                )
                if use_upstream and self._upstream is not None:
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(self._upstream.host, self._upstream.port),
                        timeout=15.0,
                    )
                    filtered_headers = [
                        line
                        for line in lines[1:]
                        if not line.lower().startswith("proxy-authorization:")
                    ]
                    rebuilt = (lines[0] + "\r\n" + "\r\n".join(filtered_headers)).encode("latin-1")
                    remote_writer.write(rebuilt)
                else:
                    path_and_query = urllib.parse.urlunsplit(
                        ("", "", parsed.path or "/", parsed.query, "")
                    )
                    rebuilt_first = f"{method} {path_and_query} {version}\r\n"
                    filtered_headers = [
                        line
                        for line in lines[1:]
                        if not line.lower().startswith(("proxy-connection:", "proxy-authorization:"))
                    ]
                    rebuilt = (rebuilt_first + "\r\n".join(filtered_headers)).encode("latin-1")
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(connect_target, port),
                        timeout=15.0,
                    )
                    remote_writer.write(rebuilt)

                await remote_writer.drain()
                await _pipe_bidirectional(reader, writer, remote_reader, remote_writer)
        except OSError as exc:
            with contextlib.suppress(Exception):
                await self._send_error(writer, 502, "Bad Gateway", str(exc))
        finally:
            if remote_writer is not None:
                self._close_writer(remote_writer)
            self._close_writer(writer)

    async def _open_tunnel(
        self,
        original_host: str,
        connect_target: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        use_upstream = (
            self._upstream is not None
            and not _bypass_upstream(original_host, self._upstream.no_proxy)
        )
        if use_upstream and self._upstream is not None:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._upstream.host, self._upstream.port),
                timeout=15.0,
            )
            try:
                authority = f"{original_host}:{port}"
                req = (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n"
                    "Proxy-Connection: keep-alive\r\n\r\n"
                ).encode("latin-1")
                writer.write(req)
                await writer.drain()
                resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
                status_line = resp.decode("latin-1", errors="replace").split("\r\n", 1)[0]
                if " 200 " not in status_line and not status_line.endswith(" 200"):
                    raise OSError(f"upstream proxy refused CONNECT: {status_line}")
                return reader, writer
            except BaseException:
                writer.close()
                raise

        return await asyncio.wait_for(
            asyncio.open_connection(connect_target, port),
            timeout=15.0,
        )

    async def _send_blocked(
        self,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        reason: str,
    ) -> None:
        cleaned = normalize_host(host)
        body = (
            f"Connection blocked by sandbox network policy ({reason}): {cleaned}:{port}\n"
            f"[Sandbox Egress Blocked] Access to '{cleaned}' was denied by the "
            f"sandbox egress policy ({reason}). Use the 'request_network_access' "
            "tool to request user approval for this domain.\n"
        ).encode("utf-8")
        headers = (
            "HTTP/1.1 403 Forbidden\r\n"
            f"X-Proxy-Error: {reason}\r\n"
            f"X-Sandbox-Blocked-Domain: {cleaned}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(headers + body)
        await writer.drain()

    async def _send_auth_required(self, writer: asyncio.StreamWriter) -> None:
        body = b"Proxy authentication required (missing or invalid Proxy-Authorization token).\n"
        headers = (
            "HTTP/1.1 407 Proxy Authentication Required\r\n"
            "X-Proxy-Error: proxy-auth-required\r\n"
            'Proxy-Authenticate: Basic realm="omicsclaw-sandbox"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(headers + body)
        await writer.drain()

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        phrase: str,
        detail: str,
    ) -> None:
        body = f"{code} {phrase}: {detail}\n".encode("utf-8")
        headers = (
            f"HTTP/1.1 {code} {phrase}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(headers + body)
        await writer.drain()

    def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        self._active_writers.discard(writer)
        with contextlib.suppress(Exception):
            writer.close()


def _parse_Authority(target: str, *, default_port: int = 443) -> tuple[str, int]:
    cleaned = target.strip()
    if cleaned.startswith("["):
        bracket_end = cleaned.find("]")
        if bracket_end != -1:
            host = cleaned[1:bracket_end]
            rest = cleaned[bracket_end + 1 :]
            if rest.startswith(":"):
                try:
                    return host, int(rest[1:])
                except ValueError:
                    return host, default_port
            return host, default_port
    if ":" in cleaned:
        host, _, port_str = cleaned.rpartition(":")
        try:
            return host, int(port_str)
        except ValueError:
            return host, default_port
    return cleaned, default_port


def _extract_host_header(header_lines: Sequence[str]) -> str:
    for line in header_lines:
        if line.lower().startswith("host:"):
            value = line.partition(":")[2].strip()
            host, _ = _parse_Authority(value, default_port=80)
            return host
    return ""


async def _pipe_one_way(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(_PIPE_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def _pipe_bidirectional(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    t1 = asyncio.create_task(_pipe_one_way(client_reader, remote_writer))
    t2 = asyncio.create_task(_pipe_one_way(remote_reader, client_writer))
    try:
        await asyncio.gather(t1, t2)
    finally:
        for task in (t1, t2):
            if not task.done():
                task.cancel()
