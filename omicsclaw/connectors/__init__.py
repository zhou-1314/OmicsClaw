# SPDX-License-Identifier: Apache-2.0
"""``omicsclaw.connectors`` — the connectors OmicsClaw ships with.

A connector is an MCP server the agent can reach without the user writing
a ``.mcp.json`` entry for it. The built-in ones live in
:mod:`omicsclaw.connectors.bio`: small stdio servers, one per life-science
domain, each a thin client over a public scientific web API (NCBI
E-utilities, ClinicalTrials.gov, ChEMBL, Ensembl, gnomAD, GTEx, Reactome,
STRING, UniProt, bioRxiv, OpenAlex).

:mod:`omicsclaw.connectors.registry` names them, remembers which ones a
workspace has switched on, and merges the enabled ones into the
:class:`~omicsclaw.mcp.MCPConfig` the deployment connects at start-up —
a user's own ``.mcp.json`` entry of the same name always wins.
"""

from .registry import (
    BUILTIN_CONNECTORS,
    BuiltinConnector,
    ConnectorStateError,
    builtin_connector,
    builtin_server_config,
    connector_state_path,
    load_connector_state,
    merge_builtin_connectors,
    save_connector_state,
)

__all__ = [
    "BUILTIN_CONNECTORS",
    "BuiltinConnector",
    "ConnectorStateError",
    "builtin_connector",
    "builtin_server_config",
    "connector_state_path",
    "load_connector_state",
    "merge_builtin_connectors",
    "save_connector_state",
]
