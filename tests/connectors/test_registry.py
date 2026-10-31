# SPDX-License-Identifier: Apache-2.0
"""The built-in connector catalogue, the per-workspace switches and the merge."""

from __future__ import annotations

import json
import sys

import pytest

from omicsclaw.connectors import registry
from omicsclaw.connectors.bio import CONNECTOR_MODULES, connector_module
from omicsclaw.connectors.registry import (
    BUILTIN_CONNECTORS,
    ConnectorStateError,
    builtin_connector,
    builtin_server_config,
    connector_state_path,
    load_connector_state,
    merge_builtin_connectors,
    save_connector_state,
)
from omicsclaw.mcp import parse_mcp_config


def test_catalogue_names_are_unique_and_each_has_an_implementation():
    names = [c.name for c in BUILTIN_CONNECTORS]
    assert len(names) == len(set(names))
    assert set(names) == set(CONNECTOR_MODULES)


@pytest.mark.parametrize("connector", BUILTIN_CONNECTORS, ids=lambda c: c.name)
def test_declared_tools_match_what_the_module_lists(connector):
    tools = [tool.name for tool in connector_module(connector.name).build_tools()]
    assert tuple(tools) == connector.tools


def test_every_connector_is_off_until_switched_on(tmp_path):
    assert all(not c.default_enabled for c in BUILTIN_CONNECTORS)
    merged = merge_builtin_connectors(parse_mcp_config({}), tmp_path)
    assert merged.config.is_empty
    assert merged.added == ()


def test_switches_round_trip_and_ignore_unknown_names(tmp_path):
    assert load_connector_state(tmp_path) == {}
    save_connector_state(tmp_path, "literature", True)
    save_connector_state(tmp_path, "chembl", False)
    path = connector_state_path(tmp_path)
    data = json.loads(path.read_text())
    data["enabled"]["retired-connector"] = True
    path.write_text(json.dumps(data))
    assert load_connector_state(tmp_path) == {"literature": True, "chembl": False}


def test_saving_an_unknown_connector_is_refused(tmp_path):
    with pytest.raises(KeyError):
        save_connector_state(tmp_path, "nope", True)


@pytest.mark.parametrize(
    "content",
    ["not json", '{"enabled": []}', '{"enabled": {"literature": "yes"}}'],
)
def test_a_malformed_switch_file_is_reported(tmp_path, content):
    path = connector_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(ConnectorStateError):
        load_connector_state(tmp_path)


def test_a_malformed_switch_file_does_not_stop_the_merge(tmp_path):
    path = connector_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    merged = merge_builtin_connectors(parse_mcp_config({}), tmp_path)
    assert merged.added == ()


def test_enabled_builtins_are_appended_after_user_servers(tmp_path):
    save_connector_state(tmp_path, "proteins", True)
    save_connector_state(tmp_path, "literature", True)
    user = parse_mcp_config({"mcpServers": {"mine": {"command": "my-server"}}}, env={})
    merged = merge_builtin_connectors(user, tmp_path, environ={})
    assert [s.name for s in merged.config.servers] == ["mine", "literature", "proteins"]
    assert merged.added == ("literature", "proteins")
    literature = merged.config.servers[1]
    assert literature.command == sys.executable
    assert literature.args == ("-m", "omicsclaw.connectors.bio", "literature")


def test_a_user_entry_with_the_same_name_wins(tmp_path):
    save_connector_state(tmp_path, "chembl", True)
    user = parse_mcp_config(
        {"mcpServers": {"chembl": {"command": "my-own-chembl"}}}, env={}
    )
    merged = merge_builtin_connectors(user, tmp_path, environ={})
    assert [s.command for s in merged.config.servers] == ["my-own-chembl"]
    assert merged.shadowed == ("chembl",)


def test_a_rejected_user_entry_still_takes_the_name(tmp_path):
    save_connector_state(tmp_path, "chembl", True)
    user = parse_mcp_config({"mcpServers": {"chembl": {}}}, env={})
    merged = merge_builtin_connectors(user, tmp_path, environ={})
    assert merged.config.servers == ()
    assert merged.shadowed == ("chembl",)


def test_only_set_network_and_credential_variables_reach_the_child():
    literature = builtin_connector("literature")
    environ = {
        "HTTPS_PROXY": "http://proxy:1",
        "NO_PROXY": "",
        "NCBI_API_KEY": "k",
        "LLM_API_KEY": "secret",
    }
    config = builtin_server_config(literature, environ=environ)
    assert dict(config.env) == {"HTTPS_PROXY": "http://proxy:1", "NCBI_API_KEY": "k"}
    chembl = builtin_server_config(builtin_connector("chembl"), environ=environ)
    assert "NCBI_API_KEY" not in chembl.env


def test_to_dict_is_json_ready():
    for connector in BUILTIN_CONNECTORS:
        json.dumps(connector.to_dict())
    assert registry.builtin_connector("missing") is None
