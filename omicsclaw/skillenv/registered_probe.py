"""Standalone probe entry point, executed by the explicitly selected Python.

Only this child reads its command line. The controller supplies a JSON list
of import names and starts it in isolated mode with a scrubbed environment.
The child needs no OmicsClaw installation and imports no framework modules.
"""

import importlib
import importlib.metadata
import json
import sys
import sysconfig
from urllib.parse import unquote, urlsplit
from pathlib import Path


def main(arguments):
    """Emit package and interpreter observations for one bounded parent request."""
    requested = json.loads(arguments[0])
    packages = {}
    editable_roots = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name", "")
        if name:
            packages[name] = dist.version
        direct = dist.read_text("direct_url.json")
        if direct:
            document = json.loads(direct)
            location = urlsplit(document.get("url", ""))
            if (
                document.get("dir_info", {}).get("editable")
                and location.scheme == "file"
                and location.netloc in ("", "localhost")
            ):
                project = Path(unquote(location.path))
                packages_in_project = []
                for top in (dist.read_text("top_level.txt") or "").splitlines():
                    if not top.isidentifier():
                        continue
                    for candidate in (
                        project / top,
                        project / (top + ".py"),
                        project / "src" / top,
                    ):
                        if candidate.exists():
                            packages_in_project.append(str(candidate))
                editable_roots.extend(packages_in_project or [str(project)])
        if len(packages) > 2048:
            raise RuntimeError("package_inventory_limit")
    missing = []
    for name in requested:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    print(
        json.dumps(
            {
                "version": sys.version.split()[0],
                "prefix": sys.prefix,
                "executable": sys.executable,
                "libraryPaths": sys.path,
                "runtimePaths": {
                    "basePrefix": sys.base_prefix,
                    "baseExecPrefix": sys.base_exec_prefix,
                    "paths": sysconfig.get_paths(),
                    "editableRoots": sorted(set(editable_roots)),
                },
                "packages": packages,
                "missing": missing,
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
