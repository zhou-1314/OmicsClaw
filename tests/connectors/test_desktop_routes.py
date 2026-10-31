# SPDX-License-Identifier: Apache-2.0
"""``/connectors`` and ``/permissions/rules`` over HTTP and over the SSH
controller's ``desktop.request`` dispatch."""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

pytest.importorskip("fastapi", reason="the Desktop HTTP adapter needs it")
testclient = pytest.importorskip("fastapi.testclient")

from omicsclaw.connectors.registry import BUILTIN_CONNECTORS, load_connector_state  # noqa: E402
from omicsclaw.entry.desktop import create_desktop_app  # noqa: E402
from omicsclaw.entry.desktop.service import DesktopService  # noqa: E402
from omicsclaw.entry.session import attach_sessions  # noqa: E402
from tests.entry.test_turn_runner import Scripted, make_app  # noqa: E402  # type: ignore[import-not-found]

JSON = {"Content-Type": "application/json"}


def served(tmp_path: pathlib.Path):
    return attach_sessions(make_app(tmp_path, Scripted(), tools=()), abandon_grace_s=None)


@pytest.fixture
def http(tmp_path):
    return testclient.TestClient(create_desktop_app(served(tmp_path)))


def test_listing_names_every_builtin_connector_switched_off(http, tmp_path):
    payload = http.get("/connectors").json()
    assert payload["workspace"] == str(tmp_path)
    builtins = [c for c in payload["connectors"] if c["source"] == "builtin"]
    assert [c["name"] for c in builtins] == [c.name for c in BUILTIN_CONNECTORS]
    literature = builtins[0]
    assert literature["enabled"] is False
    assert literature["status"] == "disabled"
    assert literature["upstreams"]
    assert literature["tools"] == ["pubmed_search", "pubmed_fetch", "biorxiv_search", "openalex_works"]
    assert literature["optional_env"] == ["NCBI_API_KEY", "OPENALEX_MAILTO"]
    assert literature["needs_credentials"] is False


def test_switching_a_builtin_on_persists_and_asks_for_a_restart(http, tmp_path):
    answer = http.put("/connectors/proteins", json={"enabled": True}, headers=JSON)
    assert answer.status_code == 200, answer.text
    entry = answer.json()["connector"]
    assert entry["enabled"] is True
    assert entry["status"] == "pending"
    assert entry["restart_required"] is True
    assert load_connector_state(tmp_path) == {"proteins": True}

    entry = http.put("/connectors/proteins", json={"enabled": False}, headers=JSON).json()[
        "connector"
    ]
    assert entry["status"] == "disabled"
    assert load_connector_state(tmp_path) == {"proteins": False}


def test_put_validates_body_and_name(http):
    refused = http.put("/connectors/proteins", json={"enabled": "yes"}, headers=JSON)
    assert refused.status_code == 422
    assert refused.json() == {"detail": "invalid_enabled"}
    assert http.put("/connectors/nope", json={"enabled": True}, headers=JSON).status_code == 404
    assert (
        http.put("/connectors/proteins", content=b'{"enabled": true}', headers={"Content-Type": "text/plain"}).status_code
        == 415
    )


def test_user_servers_are_listed_and_their_switch_edits_mcp_json(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mine": {"command": "my-server", "disabled": True}}}),
        encoding="utf-8",
    )
    http = testclient.TestClient(create_desktop_app(served(tmp_path)))
    mine = next(c for c in http.get("/connectors").json()["connectors"] if c["name"] == "mine")
    assert mine["source"] == "user"
    assert mine["enabled"] is False
    entry = http.put("/connectors/mine", json={"enabled": True}, headers=JSON).json()["connector"]
    assert entry["enabled"] is True
    written = json.loads((tmp_path / ".mcp.json").read_text())
    assert written["mcpServers"]["mine"] == {"command": "my-server", "enabled": True}


def test_check_runs_a_real_handshake_and_is_remembered(http):
    answer = http.post("/connectors/expression/check", json={}, headers=JSON)
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["status"] == "connected", result
    assert [t["name"] for t in result["tools"]] == ["gtex_gene_expression"]
    assert result["server_info"]["name"] == "omicsclaw-expression"
    http.put("/connectors/expression", json={"enabled": True}, headers=JSON)
    entry = next(c for c in http.get("/connectors").json()["connectors"] if c["name"] == "expression")
    assert entry["status"] == "connected"
    assert entry["tool_count"] == 1
    assert entry["last_check"]["status"] == "connected"


def test_check_of_a_broken_user_server_reports_failure(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"broken": {"command": "definitely-not-a-command-xyz"}}}),
        encoding="utf-8",
    )
    http = testclient.TestClient(create_desktop_app(served(tmp_path)))
    result = http.post("/connectors/broken/check", json={}, headers=JSON).json()
    assert result["status"] == "failed"
    assert result["error"]
    assert http.post("/connectors/unknown/check", json={}, headers=JSON).status_code == 404


def test_permission_rules_are_read_as_the_gate_reads_them(http, tmp_path):
    empty = http.get("/permissions/rules").json()
    assert empty["exists"] is False
    assert empty["rules"] == {"deny": [], "allow": [], "ask": []}
    rules = tmp_path / ".omicsclaw" / "settings.json"
    rules.parent.mkdir(exist_ok=True)
    rules.write_text(
        json.dumps({"permissions": {"allow": ["bash(git status)"], "deny": ["bash(rm *)"]}}),
        encoding="utf-8",
    )
    payload = http.get("/permissions/rules").json()
    assert payload["exists"] is True
    assert payload["rules"]["allow"] == ["bash(git status)"]
    assert payload["rules"]["deny"] == ["bash(rm *)"]
    rules.write_text('{"permissions": {"denied": []}}', encoding="utf-8")
    assert "unknown key" in http.get("/permissions/rules").json()["error"]


def test_the_controller_dispatch_serves_the_same_routes(tmp_path):
    app = served(tmp_path)
    service = DesktopService(app, jobs=None, interactions=None)

    async def scenario():
        listing = await service.dispatch({"method": "GET", "path": "/connectors"})
        toggled = await service.dispatch(
            {"method": "PUT", "path": "/connectors/chembl", "document": {"enabled": True}}
        )
        rules = await service.dispatch({"method": "GET", "path": "/permissions/rules"})
        missing = await service.dispatch(
            {"method": "PUT", "path": "/connectors/nope", "document": {"enabled": True}}
        )
        return listing, toggled, rules, missing

    listing, toggled, rules, missing = asyncio.run(scenario())
    assert listing["status"] == 200
    assert len([c for c in listing["payload"]["connectors"] if c["source"] == "builtin"]) == 7
    assert toggled["payload"]["connector"]["enabled"] is True
    assert rules["payload"]["rules"] == {"deny": [], "allow": [], "ask": []}
    assert missing["status"] == 404
