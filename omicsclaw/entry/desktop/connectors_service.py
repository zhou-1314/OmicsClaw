# SPDX-License-Identifier: Apache-2.0
"""``/connectors`` and ``/permissions/rules`` — plain functions behind the routes.

* :func:`connector_listing` — ``GET /connectors``: every built-in connector
  and every server of the user's ``.mcp.json``, with where it came from,
  whether it is switched on, how it stands in this process, its tool
  count, its last error and (for built-ins) the public services it calls.
* :func:`set_connector_enabled` — ``PUT /connectors/{name}``
  ``{"enabled": bool}``: a built-in connector's switch lives in
  ``<workspace>/.omicsclaw/connectors.json``; a user server's lives in its
  own ``.mcp.json`` entry (``"enabled"``). The tool set of a running
  backend is fixed at start-up, so the answer carries
  ``restart_required: true`` whenever the switch now disagrees with what
  is mounted.
* :func:`check_connector` — ``POST /connectors/{name}/check``: starts the
  server afresh, performs ``initialize`` + ``tools/list`` and closes it.
  The outcome is remembered for this process, so a later listing can show
  it for a connector that is not mounted.
* :func:`permission_rules_payload` — ``GET /permissions/rules``: the rule
  file the permission gate reads, as written. Read-only.

Like the rest of the package these take the assembled app and plain
dictionaries; :mod:`~omicsclaw.entry.desktop.server` and
:class:`~omicsclaw.entry.desktop.service.DesktopService` put them behind
HTTP and the SSH controller respectively.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from omicsclaw.connectors.registry import (
    BUILTIN_CONNECTORS,
    ConnectorStateError,
    builtin_connector,
    builtin_server_config,
    load_connector_state,
    save_connector_state,
)
from omicsclaw.mcp import MCPClient, MCPConfigError, load_mcp_config, open_transport
from omicsclaw.mcp.config import ServerConfig

from .turn_submission import DesktopIngressError

__all__ = [
    "CHECK_TIMEOUT_S",
    "check_connector",
    "connector_listing",
    "permission_rules_payload",
    "set_connector_enabled",
]

CHECK_TIMEOUT_S: Final = 45.0
"""How long one health check may take, start to closed."""

_MAX_ERROR_CHARS: Final = 400

_CHECKS: dict[tuple[str, str], dict[str, Any]] = {}
"""Last health-check outcome per ``(workspace, connector)`` in this process."""


def _workspace(app: Any) -> Path:
    return Path(app.config.workspace)


def _user_config(app: Any) -> tuple[Any, str]:
    """The user's ``.mcp.json`` as an :class:`MCPConfig`, and a load error."""
    try:
        return load_mcp_config(app.config.mcp_config_path()), ""
    except MCPConfigError as exc:
        return None, str(exc)[:_MAX_ERROR_CHARS]


def _live_statuses(app: Any) -> dict[str, Any]:
    manager = getattr(app, "mcp", None)
    if manager is None:
        return {}
    return {status.name: status for status in manager.statuses()}


def _status_of(
    app: Any, name: str, enabled: bool, live: Mapping[str, Any]
) -> dict[str, Any]:
    """``status``, ``tool_count``, ``last_error``, ``mounted`` and
    ``restart_required`` for one connector."""
    status = live.get(name)
    check = _CHECKS.get((str(_workspace(app)), name))
    if status is not None:
        state = status.state.value
        mounted = state == "connected"
        result = {
            "status": state,
            "tool_count": len(status.tools),
            "last_error": status.error[:_MAX_ERROR_CHARS],
            "mounted": mounted,
            "restart_required": mounted != enabled,
        }
    elif not enabled:
        result = {
            "status": "disabled",
            "tool_count": 0,
            "last_error": "",
            "mounted": False,
            "restart_required": False,
        }
    else:
        result = {
            "status": "pending",
            "tool_count": 0,
            "last_error": "",
            "mounted": False,
            "restart_required": True,
        }
    if check is not None:
        result["last_check"] = check
        if status is None and enabled:
            result["status"] = check["status"]
            result["tool_count"] = check["tool_count"]
            result["last_error"] = check["error"]
    return result


def connector_listing(app: Any) -> dict[str, Any]:
    """The ``GET /connectors`` payload."""
    workspace = _workspace(app)
    live = _live_statuses(app)
    try:
        state = load_connector_state(workspace)
        state_error = ""
    except ConnectorStateError as exc:
        state, state_error = {}, str(exc)[:_MAX_ERROR_CHARS]
    user, config_error = _user_config(app)
    user_names = (
        {s.name for s in user.servers} | {r.name for r in user.rejected} if user else set()
    )

    connectors: list[dict[str, Any]] = []
    for connector in BUILTIN_CONNECTORS:
        enabled = state.get(connector.name, connector.default_enabled)
        shadowed = connector.name in user_names
        entry = {
            **connector.to_dict(),
            "source": "builtin",
            "enabled": enabled,
            "shadowed": shadowed,
            "transport": "stdio",
            "configured_env": [
                key for key in connector.optional_env if os.environ.get(key, "").strip()
            ],
        }
        if shadowed:
            entry.update(
                status="disabled",
                tool_count=0,
                last_error="",
                mounted=False,
                restart_required=False,
            )
        else:
            entry.update(_status_of(app, connector.name, enabled, live))
        connectors.append(entry)

    if user is not None:
        for server in user.servers:
            entry = {
                "name": server.name,
                "display_name": server.name,
                "description": "",
                "upstreams": [server.url] if server.url else [],
                "tools": list(server.tools) if server.tools is not None else [],
                "optional_env": [],
                "needs_credentials": False,
                "default_enabled": True,
                "source": "user",
                "enabled": server.enabled,
                "shadowed": False,
                "transport": server.kind.value,
            }
            entry.update(_status_of(app, server.name, server.enabled, live))
            connectors.append(entry)
        for rejected in user.rejected:
            connectors.append(
                {
                    "name": rejected.name,
                    "display_name": rejected.name,
                    "description": "",
                    "upstreams": [],
                    "tools": [],
                    "optional_env": [],
                    "needs_credentials": False,
                    "default_enabled": True,
                    "source": "user",
                    "enabled": False,
                    "shadowed": False,
                    "transport": "",
                    "status": "failed",
                    "tool_count": 0,
                    "last_error": rejected.reason[:_MAX_ERROR_CHARS],
                    "mounted": False,
                    "restart_required": False,
                }
            )

    return {
        "workspace": str(workspace),
        "connectors": connectors,
        "state_error": state_error,
        "config_error": config_error,
        "mcp_mounted": getattr(app, "mcp", None) is not None,
    }


def set_connector_enabled(app: Any, name: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """Apply ``PUT /connectors/{name}`` and answer the connector's new entry.

    :raises DesktopIngressError: ``invalid_enabled`` (422) when the body has
        no boolean ``enabled``; ``connector_not_found`` (404);
        ``connector_state_unreadable`` / ``mcp_config_unreadable`` (409)
        when the file to change cannot be read as written.
    """
    enabled = document.get("enabled")
    if not isinstance(enabled, bool):
        raise DesktopIngressError("invalid_enabled")
    workspace = _workspace(app)
    user, config_error = _user_config(app)
    user_names = {s.name for s in user.servers} if user else set()
    if builtin_connector(name) is not None and name not in user_names:
        try:
            save_connector_state(workspace, name, enabled)
        except ConnectorStateError:
            raise DesktopIngressError("connector_state_unreadable", status_code=409) from None
    elif name in user_names:
        _write_user_enabled(Path(app.config.mcp_config_path()), name, enabled)
    elif config_error:
        raise DesktopIngressError("mcp_config_unreadable", status_code=409)
    else:
        raise DesktopIngressError("connector_not_found", status_code=404)
    listing = connector_listing(app)
    entry = next(c for c in listing["connectors"] if c["name"] == name)
    return {"connector": entry}


def _write_user_enabled(path: Path, name: str, enabled: bool) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data["mcpServers"]
        entry = servers[name]
        if not isinstance(entry, dict):
            raise TypeError(name)
    except (OSError, ValueError, KeyError, TypeError):
        raise DesktopIngressError("mcp_config_unreadable", status_code=409) from None
    entry["enabled"] = enabled
    entry.pop("disabled", None)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
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


def _server_for(app: Any, name: str) -> ServerConfig:
    user, _ = _user_config(app)
    if user is not None:
        for server in user.servers:
            if server.name == name:
                return server
    connector = builtin_connector(name)
    if connector is None:
        raise DesktopIngressError("connector_not_found", status_code=404)
    return builtin_server_config(connector)


async def check_connector(
    app: Any, name: str, *, timeout_s: float = CHECK_TIMEOUT_S
) -> dict[str, Any]:
    """Run ``POST /connectors/{name}/check``: handshake, list tools, close.

    Never raises for a server that fails to start or answer; the payload
    says ``status: "failed"`` and why.
    """
    server = _server_for(app, name)
    started = time.monotonic()
    tools: list[dict[str, str]] = []
    info: dict[str, str] = {}
    error = ""
    client = MCPClient(
        name,
        open_transport(server, cwd=_workspace(app), request_timeout_s=timeout_s),
    )
    try:
        async with asyncio.timeout(timeout_s):
            server_info = await client.connect()
        info = {
            "name": server_info.name,
            "version": server_info.version,
            "protocol_version": server_info.protocol_version,
        }
        tools = [{"name": t.name, "description": t.description} for t in client.tools]
    except TimeoutError:
        error = f"did not finish the handshake within {timeout_s:g}s"
    except Exception as exc:
        error = (str(exc) or type(exc).__name__)[:_MAX_ERROR_CHARS]
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
    outcome = {
        "status": "failed" if error else "connected",
        "tool_count": len(tools),
        "error": error,
        "checked_at": time.time(),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    _CHECKS[(str(_workspace(app)), name)] = outcome
    return {"name": name, **outcome, "tools": tools, "server_info": info}


def permission_rules_payload(app: Any) -> dict[str, Any]:
    """The ``GET /permissions/rules`` payload: the rule file as the gate reads it."""
    from omicsclaw.permission.rules import PermissionConfigError, load_rules

    config = app.config
    path = Path(config.permission_rules_path())
    grouped: dict[str, list[str]] = {"deny": [], "allow": [], "ask": []}
    error = ""
    try:
        rules = load_rules(path)
        for rule in rules:
            grouped[rule.verdict.value].append(rule.pattern)
    except PermissionConfigError as exc:
        error = str(exc)[:_MAX_ERROR_CHARS]
    return {
        "path": str(path),
        "exists": path.exists(),
        "rules": grouped,
        "error": error,
        "evaluation_order": ["deny", "allow", "ask"],
    }
