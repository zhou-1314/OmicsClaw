"""Lifecycle binding for the Backend-owned canonical :class:`RunRuntime`.

Remote routers are mounted while the FastAPI module is imported, but the
authoritative Runtime is created and started inside the Desktop lifespan.  A
small explicit binding keeps that composition concern out of the routers and
prevents them from constructing a second Control Plane or execution owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omicsclaw.control.run_runtime import RunRuntime


@dataclass(frozen=True)
class RemoteRuntimeBinding:
    """One atomically published Backend-lifespan composition binding."""

    runtime: RunRuntime
    workspace: Path


_BOUND_BINDING: RemoteRuntimeBinding | None = None


def bind_remote_run_runtime(
    runtime: RunRuntime,
    *,
    workspace: str | Path,
) -> None:
    """Expose the one already-started Backend Runtime to Remote Adapters."""

    global _BOUND_BINDING
    if not isinstance(runtime, RunRuntime):
        raise TypeError("runtime must be RunRuntime")
    frozen_workspace = Path(workspace).expanduser().resolve()
    if not frozen_workspace.is_dir():
        raise ValueError("remote workspace must be an existing directory")
    current = _BOUND_BINDING
    if current is not None:
        if current.runtime is not runtime:
            raise RuntimeError("a different Remote RunRuntime is already bound")
        if current.workspace != frozen_workspace:
            raise RuntimeError("a different Remote workspace is already bound")
        return
    _BOUND_BINDING = RemoteRuntimeBinding(runtime, frozen_workspace)


def unbind_remote_run_runtime(runtime: RunRuntime | None = None) -> None:
    """Remove a lifespan binding without closing or otherwise mutating it."""

    global _BOUND_BINDING
    current = _BOUND_BINDING
    if runtime is None or (current is not None and current.runtime is runtime):
        _BOUND_BINDING = None


def get_remote_run_runtime() -> RunRuntime | None:
    """Return the bound Runtime for pure availability inspection."""

    binding = _BOUND_BINDING
    return binding.runtime if binding is not None else None


def get_remote_workspace() -> Path | None:
    """Return the Workspace frozen with the Backend Runtime lifespan."""

    binding = _BOUND_BINDING
    return binding.workspace if binding is not None else None


def require_remote_workspace() -> Path:
    """Return the lifespan-frozen Workspace or fail closed.

    Environment variables are composition input, not a request-time authority.
    Remote Adapters must therefore never recover a missing binding by resolving
    ``OMICSCLAW_WORKSPACE`` again after the Backend has started.
    """

    binding = _BOUND_BINDING
    if binding is None:
        raise RuntimeError("remote_workspace_unavailable")
    return binding.workspace


def require_remote_run_runtime() -> RunRuntime:
    binding = _BOUND_BINDING
    if binding is None or not binding.runtime.lifecycle_ready:
        raise RuntimeError("remote_run_runtime_unavailable")
    return binding.runtime


__all__ = [
    "bind_remote_run_runtime",
    "get_remote_run_runtime",
    "get_remote_workspace",
    "require_remote_workspace",
    "require_remote_run_runtime",
    "unbind_remote_run_runtime",
    "RemoteRuntimeBinding",
]
