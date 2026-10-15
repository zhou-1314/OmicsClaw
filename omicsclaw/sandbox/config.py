"""Configuration, enums, and domain allowlists for native OS sandboxing.

Pure standard-library leaf module: no logging, no dynamic imports, and no
direct reads of the process environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path

__all__ = [
    "BackendName",
    "CommandOutcome",
    "DEFAULT_ALLOWED_DOMAINS",
    "DEFAULT_DENY_READ_PATHS",
    "DEFAULT_DENY_WRITE_GLOBS",
    "EgressMode",
    "IsolationLevel",
    "Level",
    "Mount",
    "ProbeReport",
    "SandboxBackendName",
    "SandboxConfig",
    "SandboxConfigError",
    "SandboxError",
    "SandboxMode",
    "SandboxUnavailable",
    "host_user",
]


class SandboxMode(StrEnum):
    """Operator-selected sandbox mode."""

    OFF = "off"
    AUTO = "auto"
    ON = "on"
    BWRAP = "bwrap"
    NETNS = "netns"
    SEATBELT = "seatbelt"
    APPCONTAINER = "appcontainer"
    DOCKER = "auto"
    LOCAL = "auto"

    @classmethod
    def _missing_(cls, value: object) -> SandboxMode | None:
        if isinstance(value, str) and value.strip().lower() in ("docker", "local"):
            return cls.AUTO
        return None


class BackendName(StrEnum):
    """Native OS sandbox backend identifier."""

    AUTO = "auto"
    BWRAP = "bwrap"
    NETNS = "netns"
    SEATBELT = "seatbelt"
    APPCONTAINER = "appcontainer"
    OFF = "off"
    NONE = "off"

    @classmethod
    def _missing_(cls, value: object) -> BackendName | None:
        if isinstance(value, str):
            norm = value.strip().lower()
            if norm in ("docker", "on", "local"):
                return cls.AUTO
            if norm == "none":
                return cls.OFF
        return None


SandboxBackendName = BackendName


class EgressMode(StrEnum):
    """Outbound network policy inside the sandbox."""

    NONE = "none"
    DENY = "none"
    PROXY = "proxy"
    OPEN = "open"

    @classmethod
    def _missing_(cls, value: object) -> EgressMode | None:
        if isinstance(value, str) and value.strip().lower() == "deny":
            return cls.NONE
        return None


class Level(StrEnum):
    """Isolation coverage achieved by the selected backend."""

    ISOLATED = "isolated"
    FULL = "isolated"
    NET_ONLY = "net_only"
    PARTIAL = "net_only"
    OFF = "off"
    NONE = "off"


IsolationLevel = Level


class SandboxError(RuntimeError):
    """Base error for sandbox startup or execution failures."""


class SandboxConfigError(SandboxError, ValueError):
    """Invalid sandbox configuration."""


class SandboxUnavailable(SandboxError):
    """Raised when a required sandbox backend cannot be started."""


def host_user() -> str:
    """Return ``uid:gid`` on POSIX or ``0:0`` on Windows."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if callable(getuid) and callable(getgid):
        return f"{getuid()}:{getgid()}"
    return "0:0"


@dataclass(frozen=True, slots=True)
class Mount:
    """A filesystem mount specification for a sandbox backend."""

    source: Path
    target: Path | None = None
    writable: bool = False

    def __post_init__(self) -> None:
        src = Path(self.source)
        object.__setattr__(self, "source", src)
        object.__setattr__(
            self, "target", Path(self.target) if self.target is not None else src
        )


DEFAULT_ALLOWED_DOMAINS: frozenset[str] = frozenset(
    {
        # NCBI / NIH / PubMed / GEO / SRA
        "ncbi.nlm.nih.gov",
        "*.ncbi.nlm.nih.gov",
        "eutils.ncbi.nlm.nih.gov",
        "ftp.ncbi.nlm.nih.gov",
        "sra-pub-run-odp.s3.amazonaws.com",
        # EMBL-EBI / Ensembl / UniProt / ArrayExpress / ChEMBL
        "ebi.ac.uk",
        "*.ebi.ac.uk",
        "ensembl.org",
        "*.ensembl.org",
        "rest.ensembl.org",
        "ftp.ensembl.org",
        "uniprot.org",
        "*.uniprot.org",
        "rest.uniprot.org",
        # UCSC / GENCODE / HGNC / GO / KEGG / Reactome / StringDB
        "ucsc.edu",
        "*.ucsc.edu",
        "hgdownload.soe.ucsc.edu",
        "api.genome.ucsc.edu",
        "gencodegenes.org",
        "*.gencodegenes.org",
        "ftp.ebi.ac.uk",
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
        # Single-cell / Spatial / Atlas / Chan Zuckerberg CELLxGENE / 10x / Figshare / Zenodo
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
        # Python / R / Bioconductor / Conda package repositories
        "pypi.org",
        "files.pythonhosted.org",
        "bioconductor.org",
        "*.bioconductor.org",
        "cran.r-project.org",
        "cloud.r-project.org",
        "conda.anaconda.org",
        "repo.anaconda.com",
        "prefix.dev",
        "*.prefix.dev",
        "registry.npmjs.org",
        # Code / Models / Preprints
        "github.com",
        "*.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "release-assets.githubusercontent.com",
        "huggingface.co",
        "*.huggingface.co",
        "biorxiv.org",
        "*.biorxiv.org",
        "medrxiv.org",
        "*.medrxiv.org",
        "doi.org",
        "*.doi.org",
    }
)
"""Default scientific and package-repository domain allowlist."""

DEFAULT_DENY_READ_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.azure",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.git-credentials",
    "~/.config/gh",
)
"""Default user credential paths hidden or blocked from reading inside the sandbox."""

DEFAULT_DENY_WRITE_GLOBS: tuple[str, ...] = (
    ".git",
    ".git/hooks",
    ".git/config",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".Rprofile",
    ".Renviron",
    ".mcp.json",
    ".omicsclaw",
)
"""Workspace-relative paths mounted read-only inside the sandbox to prevent persistence."""


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Resolved configuration for the native OS sandbox."""

    mode: SandboxMode = SandboxMode.AUTO
    backend: BackendName = BackendName.AUTO
    egress: EgressMode = EgressMode.PROXY
    egress_groups: tuple[str, ...] = ("pkg",)
    egress_allow: tuple[str, ...] = ()
    egress_endpoints: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    upstream_proxy: str | None = None
    no_proxy: tuple[str, ...] = ()
    allowed_domains: frozenset[str] = DEFAULT_ALLOWED_DOMAINS
    extra_allowed_domains: frozenset[str] = frozenset()
    allow_private_cidr: tuple[str, ...] = ()
    extra_read_paths: tuple[Path, ...] = ()
    extra_write_paths: tuple[Path, ...] = ()
    deny_read_paths: tuple[str, ...] = DEFAULT_DENY_READ_PATHS
    deny_write_globs: tuple[str, ...] = DEFAULT_DENY_WRITE_GLOBS
    sandbox_kernels: bool = True
    scrubbed_env: Mapping[str, str] | None = None

    # Retained for backwards compatibility with legacy callers/tests
    image: str = ""
    network: str = ""
    memory: str = ""
    cpus: str = ""
    gpus: str = ""
    read_only_mounts: tuple[Path, ...] = ()
    tmpfs_size: str = "64g"
    shm_size: str = "128g"
    pids_limit: int = 65536
    nofile: int = 65536
    start_timeout_s: float = 30.0
    stop_grace_s: float = 10.0
    bootstrap: str = ""
    bootstrap_timeout_s: float = 600.0
    runtime: str = "docker"
    user: str | None = None

    def __post_init__(self) -> None:
        raw_mode = self.mode
        if isinstance(raw_mode, str) and not isinstance(raw_mode, SandboxMode):
            norm_m = raw_mode.strip().lower()
            if norm_m in ("docker", "local"):
                object.__setattr__(self, "mode", SandboxMode.AUTO)
            else:
                try:
                    object.__setattr__(self, "mode", SandboxMode(norm_m))
                except ValueError as exc:
                    raise SandboxConfigError(f"invalid sandbox mode: {raw_mode!r}") from exc
        elif self.mode == SandboxMode.DOCKER:
            object.__setattr__(self, "mode", SandboxMode.AUTO)

        raw_backend = self.backend
        if isinstance(raw_backend, str) and not isinstance(raw_backend, BackendName):
            norm_b = raw_backend.strip().lower()
            if norm_b in ("docker", "on", "local"):
                object.__setattr__(self, "backend", BackendName.AUTO)
            elif norm_b == "none":
                object.__setattr__(self, "backend", BackendName.OFF)
            else:
                try:
                    object.__setattr__(self, "backend", BackendName(norm_b))
                except ValueError as exc:
                    raise SandboxConfigError(
                        f"invalid sandbox backend: {raw_backend!r}"
                    ) from exc

        # If mode specifies a concrete backend (e.g. SandboxMode.BWRAP / NETNS)
        # and backend was left at default AUTO, sync backend from mode.
        if self.backend == BackendName.AUTO and self.mode in (
            SandboxMode.BWRAP,
            SandboxMode.NETNS,
            SandboxMode.SEATBELT,
            SandboxMode.APPCONTAINER,
        ):
            object.__setattr__(self, "backend", BackendName(self.mode.value))

        raw_egress = self.egress
        if isinstance(raw_egress, str) and not isinstance(raw_egress, EgressMode):
            norm_e = raw_egress.strip().lower()
            if norm_e == "deny":
                object.__setattr__(self, "egress", EgressMode.NONE)
            else:
                try:
                    object.__setattr__(self, "egress", EgressMode(norm_e))
                except ValueError as exc:
                    raise SandboxConfigError(
                        f"invalid sandbox egress mode: {raw_egress!r}"
                    ) from exc

        # Resolve user default or validate non-empty string
        if self.user is None:
            object.__setattr__(self, "user", host_user())
        elif not isinstance(self.user, str) or not self.user.strip() or self.user.startswith("-") or any(c.isspace() for c in self.user):
            raise SandboxConfigError(f"invalid sandbox user: {self.user!r}")

        if not isinstance(self.runtime, str) or not self.runtime.strip() or self.runtime.startswith("-") or any(c.isspace() for c in self.runtime):
            raise SandboxConfigError(f"invalid sandbox runtime: {self.runtime!r}")

        for attr_name in ("image", "network", "memory", "cpus", "gpus", "tmpfs_size", "shm_size"):
            val = getattr(self, attr_name)
            if val:
                if val.startswith("-") or any(c.isspace() for c in val):
                    raise SandboxConfigError(f"invalid sandbox {attr_name}: {val!r}")

        if self.pids_limit <= 0:
            raise SandboxConfigError(f"pids_limit must be positive: {self.pids_limit}")
        if self.nofile <= 0:
            raise SandboxConfigError(f"nofile must be positive: {self.nofile}")
        if self.start_timeout_s <= 0:
            raise SandboxConfigError(f"start_timeout_s must be positive: {self.start_timeout_s}")
        if self.bootstrap_timeout_s <= 0:
            raise SandboxConfigError(f"bootstrap_timeout_s must be positive: {self.bootstrap_timeout_s}")
        if self.stop_grace_s < 0:
            raise SandboxConfigError(f"stop_grace_s must be non-negative: {self.stop_grace_s}")

        # Legacy network="none" compatibility when explicitly passed
        if self.network and self.network.strip().lower() not in ("none", "proxy"):
            if self.egress == EgressMode.PROXY:
                object.__setattr__(self, "egress", EgressMode.OPEN)

        # Validate and merge read_only_mounts and Mount entries into extra_read_paths / extra_write_paths
        ro_paths = list(self.extra_read_paths)
        rw_paths = list(self.extra_write_paths)
        norm_ro_mounts: list[Path] = []
        for p in self.read_only_mounts:
            path_obj = Path(p)
            if not path_obj.is_absolute() or ":" in str(path_obj):
                raise SandboxConfigError(f"invalid read_only_mounts entry: {p!r}")
            norm_ro_mounts.append(path_obj)
            if path_obj not in ro_paths:
                ro_paths.append(path_obj)
        object.__setattr__(self, "read_only_mounts", tuple(norm_ro_mounts))
        for m in self.mounts:
            target = m.target or m.source
            if m.writable:
                if target not in rw_paths:
                    rw_paths.append(target)
            else:
                if target not in ro_paths:
                    ro_paths.append(target)
        object.__setattr__(self, "extra_read_paths", tuple(ro_paths))
        object.__setattr__(self, "extra_write_paths", tuple(rw_paths))
        if not self.read_only_mounts and ro_paths:
            object.__setattr__(self, "read_only_mounts", tuple(ro_paths))

        if self.egress_allow:
            merged_allow = set(self.extra_allowed_domains) | {
                d.strip() for d in self.egress_allow if d.strip()
            }
            object.__setattr__(self, "extra_allowed_domains", frozenset(merged_allow))

    @property
    def effective_allowed_domains(self) -> frozenset[str]:
        """Union of default and operator-supplied allowed domains."""
        return self.allowed_domains | self.extra_allowed_domains | frozenset(self.egress_allow)

    @property
    def isolates_network(self) -> bool:
        """True when direct arbitrary outbound network access is blocked."""
        if self.network and self.network.strip().lower() not in ("none", "proxy", ""):
            return False
        return self.egress in (EgressMode.NONE, EgressMode.PROXY)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What running a command in the sandbox produced."""

    output: str
    exit_code: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """Availability and capability report for a sandbox backend."""

    available: bool
    backend: BackendName
    level: Level
    summary: str
    reason: str = ""
    fix_hint: str = ""
    detail: str = ""
