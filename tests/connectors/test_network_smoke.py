# SPDX-License-Identifier: Apache-2.0
"""Upstream smoke tests: real requests to the public APIs, through the
same path the agent uses (``open_app``-style merge → :class:`MCPManager`
→ mounted :class:`~omicsclaw.tools.MCPTool`).

They need the network, so they only run when
``OMICSCLAW_RUN_NETWORK_TESTS=1`` is set (and are marked ``network``).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from omicsclaw.connectors.registry import merge_builtin_connectors, save_connector_state
from omicsclaw.mcp import MCPManager, ServerState, parse_mcp_config
from omicsclaw.schema import ToolCall
from omicsclaw.tools import ToolRegistry, use_tool_context

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("OMICSCLAW_RUN_NETWORK_TESTS") != "1",
        reason="set OMICSCLAW_RUN_NETWORK_TESTS=1 to call the public APIs",
    ),
]


@pytest.fixture(scope="module")
def calls(tmp_path_factory):
    """Run every smoke call in one manager, so seven servers start once."""
    workspace = tmp_path_factory.mktemp("connectors-smoke")
    for name in ("literature", "chembl", "clinical-trials", "genomics", "proteins"):
        save_connector_state(workspace, name, True)
    merged = merge_builtin_connectors(parse_mcp_config({}), workspace)
    plan = {
        "pubmed_search": ("literature", "pubmed_search", {"query": "TP53[Title] AND spatial transcriptomics", "max_results": 3}),
        "pubmed_fetch": ("literature", "pubmed_fetch", {"pmids": ["31018141"]}),
        "chembl": ("chembl", "chembl_molecule", {"query": "CHEMBL25"}),
        "trial": ("clinical-trials", "trial_get", {"nct_id": "NCT04368728"}),
        "ensembl": ("genomics", "ensembl_lookup", {"query": "BRCA2"}),
        "uniprot": ("proteins", "uniprot_entry", {"accession": "P04637"}),
    }

    async def scenario():
        async with MCPManager(
            merged.config, cwd=workspace, connect_timeout_s=60, request_timeout_s=120
        ) as manager:
            for status in manager.statuses():
                assert status.state is ServerState.CONNECTED, (status.name, status.error)
            registry = ToolRegistry(manager.tools())
            names = {
                (status.name, detail.tool): detail.name
                for status in manager.statuses()
                for detail in status.tools
            }
            out = {}
            with use_tool_context(approval=lambda _request: True):
                for key, (server, tool, args) in plan.items():
                    result = await registry.execute(
                        ToolCall(id=key, name=names[(server, tool)], arguments=json.dumps(args))
                    )
                    assert result.is_error is False, result.output
                    out[key] = json.loads(result.output)
            return out

    return asyncio.run(scenario())


def test_pubmed_search_returns_pmids(calls):
    result = calls["pubmed_search"]
    assert result["pmids"], result
    assert all(pmid.isdigit() for pmid in result["pmids"])


def test_pubmed_fetch_returns_the_known_title(calls):
    article = calls["pubmed_fetch"]["articles"][0]
    assert article["pmid"] == "31018141"
    assert "Alzheimer" in article["title"]


def test_chembl25_is_aspirin(calls):
    assert calls["chembl"]["results"][0]["pref_name"] == "ASPIRIN"


def test_known_nct_number_has_its_title(calls):
    trial = calls["trial"]
    assert trial["nct_id"] == "NCT04368728"
    assert "COVID-19" in trial["title"]


def test_brca2_resolves_to_its_ensembl_gene(calls):
    assert calls["ensembl"]["id"] == "ENSG00000139618"


def test_p04637_is_tp53(calls):
    entry = calls["uniprot"]
    assert entry["accession"] == "P04637"
    assert "TP53" in entry["genes"]
