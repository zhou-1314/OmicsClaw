"""In-kernel guard for the non-bwrap isolation tier (ADR 0032).

The legacy one-shot subprocess guard (``build_python_runtime_guard`` +
``_GUARD_TEMPLATE``) was removed with the single-engine consolidation
(2026-06-22). What remains is :func:`build_kernel_guard_code`, the best-effort
defense-in-depth guard the mini-agent injects into its kernel when no OS sandbox
(bubblewrap) is available.
"""

from __future__ import annotations

import json
from pathlib import Path
import textwrap


def build_kernel_guard_code(
    *,
    workspace_root: str | Path,
    read_roots: list[str | Path] | None = None,
) -> str:
    """In-kernel defense-in-depth guard for the **non-bwrap** isolation tier.

    ADR 0032 tiered isolation: when no OS sandbox (bubblewrap) is available (e.g.
    a macOS / Windows local desktop runtime) the mini-agent kernel runs this
    *after* its imports. It reliably blocks network egress (the data-safety
    guarantee) and destructive ``os`` ops, and chdirs into the workspace so
    relative writes stay local. It does NOT block ``subprocess`` (the trusted
    ``oc`` facade needs it) and does not monkeypatch ``open`` (ineffective in an
    IPython kernel and it breaks library caches); raw LLM cells are AST-linted
    separately. Best-effort, not a security boundary — the OS envelope is the
    real boundary where available.
    """
    workspace = Path(workspace_root).resolve()
    payload = {"workspace_root": str(workspace)}
    return _KERNEL_GUARD_TEMPLATE.replace("__OMICSCLAW_KGUARD_PAYLOAD__", json.dumps(payload))


_KERNEL_GUARD_TEMPLATE = textwrap.dedent(
    r'''
    # OmicsClaw mini-agent in-kernel guard (non-bwrap tier): block network egress
    # and destructive os ops, and chdir into the run workspace so relative writes
    # stay local. subprocess stays open (the trusted skill facade needs it); raw
    # cells are AST-linted separately. The OS envelope is the real boundary.
    import os as _omics_os

    _OMICS_KG = __OMICSCLAW_KGUARD_PAYLOAD__
    try:
        _omics_os.chdir(_OMICS_KG["workspace_root"])
    except Exception:
        pass

    def _omics_blocked(*a, **k):
        raise RuntimeError("OmicsClaw mini-agent guard blocks network / destructive os actions (non-sandboxed tier).")

    _omics_os.system = _omics_blocked
    _omics_os.popen = _omics_blocked
    for _m in ("remove", "unlink", "rename", "replace", "rmdir"):
        try:
            setattr(_omics_os, _m, _omics_blocked)
        except Exception:
            pass
    try:
        import socket as _omics_socket
        _omics_socket.socket = _omics_blocked
        _omics_socket.create_connection = _omics_blocked
    except Exception:
        pass
    for _mod in ("requests", "httpx"):
        try:
            _m = __import__(_mod)
            for _fn in ("get", "post", "put", "delete", "head", "patch", "request"):
                if hasattr(_m, _fn):
                    setattr(_m, _fn, _omics_blocked)
        except Exception:
            pass
    print("[mini-agent in-kernel guard active: network blocked, destructive os ops blocked]")
    '''
).strip()
