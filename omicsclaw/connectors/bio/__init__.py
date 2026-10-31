# SPDX-License-Identifier: Apache-2.0
"""Built-in life-science connectors, one stdio MCP server per domain.

Start one with ``python -m omicsclaw.connectors.bio <name>``; ``--list``
prints the tools it offers as JSON instead of serving. See
:data:`omicsclaw.connectors.registry.BUILTIN_CONNECTORS` for the catalogue.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Final

__all__ = ["CONNECTOR_MODULES", "connector_module"]

CONNECTOR_MODULES: Final = {
    "literature": "literature",
    "clinical-trials": "clinical_trials",
    "chembl": "chembl",
    "genomics": "genomics",
    "expression": "expression",
    "pathways": "pathways",
    "proteins": "proteins",
}
"""Connector name to the module in this package that implements it."""


def connector_module(name: str) -> ModuleType:
    """The module implementing connector *name*.

    :raises KeyError: no such connector.
    """
    return import_module(f"{__name__}.{CONNECTOR_MODULES[name]}")
