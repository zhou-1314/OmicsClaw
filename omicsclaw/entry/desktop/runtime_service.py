"""Shared process facts for HTTP and stdio adapters."""

from __future__ import annotations

import secrets
import sys
from typing import Any, Final

from omicsclaw.entry.assembly import AgentApp
from omicsclaw.kernel.session import r_available
from omicsclaw.version import __version__, build_identity

from .doctor import effective_model
from .wire_contract import (
    CONNECTION_EPOCH,
    DESKTOP_CAPABILITIES,
    SERVED_PATHS,
    desktop_artifacts_contract,
    desktop_chat_contract,
    desktop_jobs_contract,
)

BACKEND_PROCESS_EPOCH: Final = secrets.token_hex(32)


def health_payload(app: AgentApp) -> dict[str, Any]:
    """What ``GET /health`` answers. Pure, so a test needs no HTTP.

    ``contracts.desktop_chat`` is the wire contract; a client checks its
    versions before it speaks, and refuses a backend whose versions it
    does not implement. Its ``abandon_grace_s`` is the session registry's,
    ``None`` for an app without one.

    ``provider``, ``model``, ``skills_count``, ``python_executable``,
    ``skill_python_executable``, ``omicsclaw_dir`` and ``launch_id`` are
    all required by the desktop client's health validator: a payload with
    any of them must carry every one. ``model`` is the model the provider
    calls (:func:`~omicsclaw.entry.desktop.doctor.effective_model`), not
    only the one the deployment named.
    ``skill_python_executable`` is this interpreter, because no skill runs
    in a subprocess of its own. ``omicsclaw_dir`` is the workspace, which
    skills (unless ``skills_dir`` is set) and ``.mcp.json`` are resolved
    against. ``launch_id`` is
    :attr:`~omicsclaw.entry.config.AppConfig.launch_id`, which a managed
    launch compares with the id it passed.
    """
    chat_contract = desktop_chat_contract(
        abandon_grace_s=app.sessions.abandon_grace_s if app.sessions else None
    )
    chat_contract["durable_ingress_idempotency"] = (
        app.sessions is not None and app.sessions.durable_identity is not None
    )
    return {
        "status": "ok",
        "version": __version__,
        "backend_process_epoch": BACKEND_PROCESS_EPOCH,
        "connection_epoch": CONNECTION_EPOCH,
        "capabilities": {
            **DESKTOP_CAPABILITIES,
            "kernel_r": r_available(),
            # C1's product switch, as this process actually runs it: the
            # plane exists exactly when ``remote_execution`` was on and the
            # store opened (``build_app`` binds ``app.remote``), so this is
            # "the remote tools, the remote jobs runtime and the
            # ``/remote/hosts`` face are all live" — the same per-app fact
            # ``kernel_r`` is, and additive in the health payload the same
            # way: a client that does not know the key ignores it.
            "remote_execution": getattr(app, "remote", None) is not None,
            "remote_execution_aliases": getattr(app, "remote", None) is not None,
            "remote_execution_files": getattr(app, "remote", None) is not None,
            # Read-only target files use the probe's POSIX shell transport;
            # a host whose probe is incomplete answers remote_files_unsupported
            # instead of silently assuming GNU utilities or SFTP semantics.
            "remote_execution_files_transport": "sftp-readonly",
        },
        "build": dict(build_identity()),
        "provider": app.provider.name,
        "model": effective_model(app),
        "skills_count": len(app.skills.skills),
        "python_executable": sys.executable,
        "skill_python_executable": sys.executable,
        "omicsclaw_dir": str(app.config.workspace),
        "launch_id": app.config.launch_id,
        "served_paths": list(SERVED_PATHS),
        "contracts": {
            "desktop_chat": chat_contract,
            "desktop_jobs": desktop_jobs_contract(),
            "desktop_artifacts": desktop_artifacts_contract(),
        },
    }


def unauthenticated_health_payload(launch_id: str = "") -> dict[str, Any]:
    """The reduced answer given to an unauthenticated ``/health`` probe.

    A desktop launcher polls this route to learn whether the backend is up
    *and* whether it needs a token, before it has one; answering it with
    provider and model would publish the deployment's configuration to
    anybody who can reach the port. ``launch_id`` is kept because it is how
    a launcher tells its own child from a leftover backend squatting the
    port; the launcher minted it, so publishing it costs nothing.
    """
    return {
        "status": "ok",
        "version": __version__,
        "launch_id": launch_id,
        "auth_required": True,
    }
