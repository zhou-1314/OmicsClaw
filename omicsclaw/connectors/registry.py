# SPDX-License-Identifier: Apache-2.0
"""The built-in connector catalogue and each workspace's on/off switches.

:data:`BUILTIN_CONNECTORS` is the fixed list of connectors that ship with
OmicsClaw. Each one is a stdio MCP server started as
``<python> -m omicsclaw.connectors.bio <name>`` — the same interpreter that
runs the backend, so it needs nothing installed beyond OmicsClaw itself.

Whether a connector is switched on is a property of the workspace, kept
in ``<workspace>/.omicsclaw/connectors.json``::

    {"enabled": {"literature": true, "chembl": false}}

A connector absent from the file falls back to its
:attr:`BuiltinConnector.default_enabled`. :func:`merge_builtin_connectors`
appends every switched-on connector to an :class:`~omicsclaw.mcp.MCPConfig`
that was read from ``.mcp.json``; when the user's file already names a
server with the same name, the user's entry wins and the built-in one is
left out.

A connector's child process inherits only the handful of variables the
stdio transport allows (see :data:`omicsclaw.mcp.stdio.INHERITED_ENV_VARS`).
The ones a built-in connector needs to reach the network — proxy settings,
CA bundle, the optional API keys it understands — are therefore passed
explicitly in its :class:`~omicsclaw.mcp.ServerConfig` ``env``, and only
when they are set in this process.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from omicsclaw.mcp.config import MCPConfig, ServerConfig, TransportKind

__all__ = [
    "BUILTIN_CONNECTORS",
    "BuiltinConnector",
    "CONNECTOR_STATE_FILENAME",
    "ConnectorStateError",
    "MergeOutcome",
    "PASSTHROUGH_ENV_VARS",
    "builtin_connector",
    "builtin_server_config",
    "connector_state_path",
    "enabled_builtin_names",
    "load_connector_state",
    "merge_builtin_connectors",
    "save_connector_state",
]

CONNECTOR_STATE_FILENAME: Final = "connectors.json"
"""The per-workspace switch file, inside ``<workspace>/.omicsclaw``."""

PASSTHROUGH_ENV_VARS: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "PYTHONPATH",
)
"""Network and import settings a connector process needs to behave like
the backend that started it. Copied only when set."""


class ConnectorStateError(ValueError):
    """The switch file exists but cannot be used as written."""


@dataclass(frozen=True, slots=True)
class BuiltinConnector:
    """One connector that ships with OmicsClaw."""

    name: str
    """Server name; MCP tools are mounted as ``mcp__{name}__{tool}``."""
    display_name: str
    description: str
    upstreams: tuple[str, ...]
    """Public services the connector calls, for display."""
    tools: tuple[str, ...]
    """The tool names the server lists, known without starting it."""
    optional_env: tuple[str, ...] = ()
    """Credentials the connector uses when set and works without."""
    needs_credentials: bool = False
    """Whether the connector is unusable without a credential."""
    default_enabled: bool = False
    module: str = "omicsclaw.connectors.bio"

    def command(self) -> tuple[str, tuple[str, ...]]:
        """The executable and arguments that start this connector."""
        return sys.executable, ("-m", self.module, self.name)

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready description, for listings."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "upstreams": list(self.upstreams),
            "tools": list(self.tools),
            "optional_env": list(self.optional_env),
            "needs_credentials": self.needs_credentials,
            "default_enabled": self.default_enabled,
        }


BUILTIN_CONNECTORS: Final[tuple[BuiltinConnector, ...]] = (
    BuiltinConnector(
        name="literature",
        display_name="Literature",
        description=(
            "Search and fetch biomedical literature: PubMed records, "
            "bioRxiv / medRxiv preprints and OpenAlex scholarly works."
        ),
        upstreams=("NCBI E-utilities (PubMed)", "bioRxiv / medRxiv API", "OpenAlex"),
        tools=("pubmed_search", "pubmed_fetch", "biorxiv_search", "openalex_works"),
        optional_env=("NCBI_API_KEY", "OPENALEX_MAILTO"),
    ),
    BuiltinConnector(
        name="clinical-trials",
        display_name="Clinical Trials",
        description="Search registered clinical studies and read a study record.",
        upstreams=("ClinicalTrials.gov API v2",),
        tools=("trials_search", "trial_get"),
    ),
    BuiltinConnector(
        name="chembl",
        display_name="ChEMBL",
        description=(
            "Bioactive molecules, drug targets and measured activities "
            "from the ChEMBL database."
        ),
        upstreams=("ChEMBL REST API",),
        tools=("chembl_molecule", "chembl_target_search", "chembl_activities"),
    ),
    BuiltinConnector(
        name="genomics",
        display_name="Genomes & Variants",
        description=(
            "Gene and transcript lookup, population allele frequencies and "
            "clinical variant interpretations."
        ),
        upstreams=("Ensembl REST", "gnomAD GraphQL", "ClinVar (NCBI E-utilities)"),
        tools=("ensembl_lookup", "gnomad_variant", "clinvar_search"),
        optional_env=("NCBI_API_KEY",),
    ),
    BuiltinConnector(
        name="expression",
        display_name="Expression",
        description="Tissue-level gene expression from the GTEx portal.",
        upstreams=("GTEx Portal API v2",),
        tools=("gtex_gene_expression",),
    ),
    BuiltinConnector(
        name="pathways",
        display_name="Pathways & Interactions",
        description="Curated pathways for a gene and protein–protein interaction networks.",
        upstreams=("Reactome Content Service", "STRING API"),
        tools=("reactome_pathways_for_gene", "string_network"),
    ),
    BuiltinConnector(
        name="proteins",
        display_name="Proteins",
        description="Protein search and curated entries from UniProtKB.",
        upstreams=("UniProt REST API",),
        tools=("uniprot_search", "uniprot_entry"),
    ),
)

_BY_NAME: Final[Mapping[str, BuiltinConnector]] = MappingProxyType(
    {connector.name: connector for connector in BUILTIN_CONNECTORS}
)


def builtin_connector(name: str) -> BuiltinConnector | None:
    """The built-in connector called *name*, or ``None``."""
    return _BY_NAME.get(name)


def connector_state_path(workspace: Path) -> Path:
    """Where *workspace* keeps its connector switches."""
    return Path(workspace) / ".omicsclaw" / CONNECTOR_STATE_FILENAME


def load_connector_state(workspace: Path) -> dict[str, bool]:
    """The explicit switches of *workspace*, by connector name.

    A missing or empty file is no switches. Names that are not built-in
    connectors are dropped, so a connector removed in a later release does
    not linger.

    :raises ConnectorStateError: the file exists and is not the documented
        shape.
    """
    path = connector_state_path(workspace)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConnectorStateError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConnectorStateError(f"{path} is not valid JSON: {exc}") from exc
    enabled = data.get("enabled", {}) if isinstance(data, Mapping) else None
    if not isinstance(enabled, Mapping):
        raise ConnectorStateError(f"{path}: 'enabled' must be an object")
    state: dict[str, bool] = {}
    for name, value in enabled.items():
        if not isinstance(value, bool):
            raise ConnectorStateError(f"{path}: 'enabled.{name}' must be true or false")
        if name in _BY_NAME:
            state[str(name)] = value
    return state


def save_connector_state(workspace: Path, name: str, enabled: bool) -> dict[str, bool]:
    """Switch the built-in connector *name* on or off for *workspace*.

    Written to a temporary file and renamed into place. Returns the full
    explicit state after the change.

    :raises KeyError: *name* is not a built-in connector.
    :raises ConnectorStateError: the existing file cannot be read.
    """
    if name not in _BY_NAME:
        raise KeyError(name)
    state = load_connector_state(workspace)
    state[name] = bool(enabled)
    path = connector_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"enabled": state}, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return state


def enabled_builtin_names(state: Mapping[str, bool]) -> tuple[str, ...]:
    """Built-in connectors switched on under *state*, in catalogue order."""
    return tuple(
        connector.name
        for connector in BUILTIN_CONNECTORS
        if state.get(connector.name, connector.default_enabled)
    )


def builtin_server_config(
    connector: BuiltinConnector,
    *,
    environ: Mapping[str, str] | None = None,
    enabled: bool = True,
) -> ServerConfig:
    """The :class:`~omicsclaw.mcp.ServerConfig` that starts *connector*.

    *environ* (default ``os.environ``) supplies the passthrough variables
    and the connector's optional credentials; only variables that are set
    and non-empty are copied.
    """
    source = os.environ if environ is None else environ
    names = (*PASSTHROUGH_ENV_VARS, *connector.optional_env)
    env = {key: source[key] for key in names if source.get(key)}
    command, args = connector.command()
    return ServerConfig(
        name=connector.name,
        kind=TransportKind.STDIO,
        command=command,
        args=args,
        env=MappingProxyType(env),
        enabled=enabled,
    )


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What :func:`merge_builtin_connectors` produced."""

    config: MCPConfig
    added: tuple[str, ...] = ()
    """Built-in connectors appended to the configuration."""
    shadowed: tuple[str, ...] = field(default_factory=tuple)
    """Enabled built-in connectors left out because the user's file names
    a server of the same name."""


def merge_builtin_connectors(
    config: MCPConfig,
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    state: Mapping[str, bool] | None = None,
) -> MergeOutcome:
    """*config* plus every built-in connector *workspace* has switched on.

    User entries — valid or rejected — keep their place and win over a
    built-in connector of the same name. Built-ins are appended after
    them in catalogue order and keep :attr:`MCPConfig.source` pointing at
    the user's file. A switch file that cannot be read counts as no
    switches (every connector at its default), so a broken file never
    stops the backend from starting.
    """
    if state is None:
        try:
            state = load_connector_state(workspace)
        except ConnectorStateError:
            state = {}
    taken = {server.name for server in config.servers}
    taken.update(rejected.name for rejected in config.rejected)
    added: list[ServerConfig] = []
    shadowed: list[str] = []
    for name in enabled_builtin_names(state):
        if name in taken:
            shadowed.append(name)
            continue
        added.append(builtin_server_config(_BY_NAME[name], environ=environ))
    if not added:
        return MergeOutcome(config, (), tuple(shadowed))
    merged = MCPConfig(
        servers=(*config.servers, *added),
        rejected=config.rejected,
        source=config.source,
    )
    return MergeOutcome(merged, tuple(s.name for s in added), tuple(shadowed))
