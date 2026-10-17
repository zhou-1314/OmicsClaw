"""Physical dependency evidence shared by managed scientific and control processes."""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

from .environment_coordination import CoordinationError, root_evidence


def existing_roots(paths, *, role):
    roots = []
    for value in paths:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise CoordinationError("environment_maintenance_invalid_scope")
        path = Path(value)
        # Python reports its optional stdlib zip even when that file is absent.
        if not path.exists():
            if path.suffix == ".zip":
                continue
            raise CoordinationError("environment_maintenance_scope_changed")
        root = root_evidence(value, role=role)
        if not any(
            (r["device"], r["inode"]) == (root["device"], root["inode"]) for r in roots
        ):
            roots.append(root)
    if not 1 <= len(roots) <= 64:
        raise CoordinationError("environment_maintenance_invalid_scope")
    return roots


def registered_read_roots(environment):
    observation = environment.get("observation") or {}
    runtime = observation.get("runtimePaths")
    if not isinstance(runtime, dict):
        raise CoordinationError("environment_maintenance_probe_required")
    paths = [environment["spec"]["executable"], observation["executable"]]
    editable = runtime.get("editableRoots", [])
    for path in observation["libraryPaths"]:
        if (
            editable
            and path.startswith("__editable__.")
            and path.endswith(".__path_hook__")
        ):
            continue
        paths.append(path)
    paths.extend(editable)
    if environment["spec"]["language"] == "python":
        # Prefix is an installation identity, not a complete list of imports.
        # Inherited site-packages and the base stdlib are separate dependencies.
        paths.extend(
            runtime["paths"][key]
            for key in ("stdlib", "platstdlib", "purelib", "platlib")
            if key in runtime["paths"]
        )
    else:
        paths.append(observation["prefix"])
    return existing_roots(paths, role="runtime_read")


def control_read_roots():
    paths = [sys.executable, *(p for p in sys.path if p and Path(p).is_absolute())]
    paths.extend(
        sysconfig.get_path(name)
        for name in ("stdlib", "platstdlib", "purelib", "platlib")
    )
    return existing_roots(paths, role="control_read")


def registered_write_roots(environment, *, library_path=None):
    observation = environment.get("observation") or {}
    if environment["spec"]["language"] == "python":
        paths = [observation["prefix"]]
    elif library_path in environment["spec"]["libraryPaths"]:
        paths = [library_path]
    else:
        raise CoordinationError("environment_maintenance_invalid_scope")
    return existing_roots(paths, role="install_write")
