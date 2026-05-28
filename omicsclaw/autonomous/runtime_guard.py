"""Runtime guard bootstrap for generated autonomous Python scripts."""

from __future__ import annotations

import json
from pathlib import Path
import textwrap


def build_python_runtime_guard(
    *,
    workspace_root: str | Path,
    input_paths: list[str | Path] | None = None,
    upstream_paths: list[str | Path] | None = None,
    goal: str = "",
    context: str = "",
    web_context: str = "",
) -> str:
    """Return Python code that constrains generated scripts at runtime.

    The guard is intentionally defensive rather than a full sandbox. It blocks
    direct network libraries and subprocess shell-outs, and restricts writes to
    the autonomous run workspace. Read access is allowed for explicit input and
    upstream references plus the workspace itself.
    """
    workspace = Path(workspace_root).resolve()
    read_roots = [workspace]
    for raw_path in [*(input_paths or []), *(upstream_paths or [])]:
        try:
            read_roots.append(Path(raw_path).expanduser().resolve())
        except OSError:
            continue
    payload = {
        "workspace_root": str(workspace),
        "read_roots": [str(item) for item in read_roots],
        "goal": goal,
        "context": context,
        "web_context": web_context,
    }
    return _GUARD_TEMPLATE.replace("__OMICSCLAW_GUARD_PAYLOAD__", json.dumps(payload))


_GUARD_TEMPLATE = textwrap.dedent(
    r'''
    # OmicsClaw Autonomous Code Runner runtime guard.
    import builtins as _omics_builtins
    import os as _omics_os
    import pathlib as _omics_pathlib
    import socket as _omics_socket
    import subprocess as _omics_subprocess

    _OMICS_GUARD = __OMICSCLAW_GUARD_PAYLOAD__
    AUTONOMOUS_WORKSPACE = _OMICS_GUARD["workspace_root"]
    AUTONOMOUS_OUTPUT_DIR = AUTONOMOUS_WORKSPACE
    ANALYSIS_GOAL = _OMICS_GUARD.get("goal", "")
    ANALYSIS_CONTEXT = _OMICS_GUARD.get("context", "")
    WEB_CONTEXT = _OMICS_GUARD.get("web_context", "")
    INPUT_PATHS = tuple(_OMICS_GUARD.get("read_roots", ())[1:])
    OUTPUT_PATH = _omics_pathlib.Path(AUTONOMOUS_WORKSPACE)
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

    _OMICS_WORKSPACE_ROOT = _omics_pathlib.Path(AUTONOMOUS_WORKSPACE).resolve()
    _OMICS_READ_ROOTS = tuple(
        _omics_pathlib.Path(path).resolve()
        for path in _OMICS_GUARD.get("read_roots", ())
    )
    _OMICS_WRITE_MODES = ("w", "a", "x", "+")
    _OMICS_ORIGINAL_OPEN = _omics_builtins.open
    _OMICS_ORIGINAL_PATH_OPEN = _omics_pathlib.Path.open
    _OMICS_ORIGINAL_WRITE_TEXT = _omics_pathlib.Path.write_text
    _OMICS_ORIGINAL_WRITE_BYTES = _omics_pathlib.Path.write_bytes
    _OMICS_ORIGINAL_MKDIR = _omics_pathlib.Path.mkdir
    _OMICS_ORIGINAL_OS_MKDIR = _omics_os.mkdir
    _OMICS_ORIGINAL_OS_MAKEDIRS = _omics_os.makedirs

    def _omics_inside(path, root):
        try:
            _omics_pathlib.Path(path).expanduser().resolve().relative_to(root)
            return True
        except Exception:
            return False

    def _omics_validate_read(path):
        resolved = _omics_pathlib.Path(path).expanduser().resolve()
        if not any(_omics_inside(resolved, root) for root in _OMICS_READ_ROOTS):
            raise PermissionError(f"Autonomous read outside approved paths: {resolved}")
        return resolved

    def _omics_validate_write(path):
        resolved = _omics_pathlib.Path(path).expanduser().resolve()
        if not _omics_inside(resolved, _OMICS_WORKSPACE_ROOT):
            raise PermissionError(f"Autonomous write outside workspace: {resolved}")
        return resolved

    def _omics_open(file, mode="r", *args, **kwargs):
        text_mode = str(mode or "r")
        if any(marker in text_mode for marker in _OMICS_WRITE_MODES):
            _omics_validate_write(file)
        else:
            _omics_validate_read(file)
        return _OMICS_ORIGINAL_OPEN(file, mode, *args, **kwargs)

    def _omics_path_open(self, mode="r", *args, **kwargs):
        text_mode = str(mode or "r")
        if any(marker in text_mode for marker in _OMICS_WRITE_MODES):
            _omics_validate_write(self)
        else:
            _omics_validate_read(self)
        return _OMICS_ORIGINAL_PATH_OPEN(self, mode, *args, **kwargs)

    def _omics_write_text(self, *args, **kwargs):
        _omics_validate_write(self)
        return _OMICS_ORIGINAL_WRITE_TEXT(self, *args, **kwargs)

    def _omics_write_bytes(self, *args, **kwargs):
        _omics_validate_write(self)
        return _OMICS_ORIGINAL_WRITE_BYTES(self, *args, **kwargs)

    def _omics_mkdir(self, *args, **kwargs):
        _omics_validate_write(self)
        return _OMICS_ORIGINAL_MKDIR(self, *args, **kwargs)

    def _omics_os_mkdir(path, *args, **kwargs):
        _omics_validate_write(path)
        return _OMICS_ORIGINAL_OS_MKDIR(path, *args, **kwargs)

    def _omics_os_makedirs(name, *args, **kwargs):
        _omics_validate_write(name)
        return _OMICS_ORIGINAL_OS_MAKEDIRS(name, *args, **kwargs)

    def _omics_blocked(*args, **kwargs):
        raise RuntimeError("OmicsClaw Autonomous Code Runner blocks shell/network/package-install actions by default.")

    _omics_builtins.open = _omics_open
    _omics_pathlib.Path.open = _omics_path_open
    _omics_pathlib.Path.write_text = _omics_write_text
    _omics_pathlib.Path.write_bytes = _omics_write_bytes
    _omics_pathlib.Path.mkdir = _omics_mkdir
    _omics_os.mkdir = _omics_os_mkdir
    _omics_os.makedirs = _omics_os_makedirs
    _omics_os.remove = _omics_blocked
    _omics_os.unlink = _omics_blocked
    _omics_os.rename = _omics_blocked
    _omics_os.replace = _omics_blocked
    _omics_os.rmdir = _omics_blocked
    _omics_subprocess.run = _omics_blocked
    _omics_subprocess.Popen = _omics_blocked
    _omics_subprocess.call = _omics_blocked
    _omics_subprocess.check_call = _omics_blocked
    _omics_subprocess.check_output = _omics_blocked
    _omics_os.system = _omics_blocked
    _omics_os.popen = _omics_blocked
    _omics_socket.socket = _omics_blocked

    try:
        import requests as _omics_requests
        _omics_requests.get = _omics_blocked
        _omics_requests.post = _omics_blocked
        _omics_requests.put = _omics_blocked
        _omics_requests.delete = _omics_blocked
    except Exception:
        pass

    try:
        import httpx as _omics_httpx
        _omics_httpx.get = _omics_blocked
        _omics_httpx.post = _omics_blocked
        _omics_httpx.put = _omics_blocked
        _omics_httpx.delete = _omics_blocked
    except Exception:
        pass
    '''
).strip()
