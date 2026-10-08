"""``omicsclaw.bench`` and the rest of ``omicsclaw`` do not import each other.

The harness measures an agent through its process and the files that
process writes. Importing the agent's internals would tie a measurement to
the code being measured, and a product module importing the harness would
ship the harness with the product. Both directions are checked in the
source and, for the harness, in what a real import loads.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "omicsclaw"
BENCH = PACKAGE / "bench"


def imported(path: Path) -> list[str]:
    """Absolute names of the modules *path* imports, relative ones resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(REPO_ROOT).with_suffix("").parts[:-1])
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                names.append(node.module or "")
                continue
            base = package[: len(package) - (node.level - 1)]
            names.append(".".join([*base, node.module] if node.module else base))
    return names


def test_there_is_a_harness_to_check():
    assert len(list(BENCH.rglob("*.py"))) >= 10


def test_the_harness_imports_nothing_else_from_omicsclaw():
    offenders = [
        f"{path.relative_to(PACKAGE)} imports {name}"
        for path in sorted(BENCH.rglob("*.py"))
        for name in imported(path)
        if (name == "omicsclaw" or name.startswith("omicsclaw."))
        and not (name == "omicsclaw.bench" or name.startswith("omicsclaw.bench."))
    ]

    assert not offenders, offenders


def test_the_harness_imports_no_skill_code():
    offenders = [
        f"{path.relative_to(PACKAGE)} imports {name}"
        for path in sorted(BENCH.rglob("*.py"))
        for name in imported(path)
        if name == "skills" or name.startswith("skills.")
    ]

    assert not offenders, offenders


def test_nothing_in_omicsclaw_imports_the_harness():
    offenders = [
        f"{path.relative_to(PACKAGE)} imports {name}"
        for path in sorted(PACKAGE.rglob("*.py"))
        if BENCH not in path.parents
        for name in imported(path)
        if name == "omicsclaw.bench" or name.startswith("omicsclaw.bench.")
    ]

    assert not offenders, offenders


def test_loading_the_whole_harness_loads_no_agent_layer():
    """After the command line and the OmicsClaw adapter are imported for
    real, :data:`sys.modules` holds ``omicsclaw.bench`` and, from the rest
    of the package, only ``omicsclaw.version``, which the package's own
    ``__init__`` imports. A lazy import inside a function would pass the
    source check above and fail this one.
    """
    probe = (
        "import sys\n"
        "import omicsclaw.bench.__main__\n"
        "import omicsclaw.bench.adapters.omicsclaw\n"
        "import omicsclaw.bench.example\n"
        "loaded = sorted(name for name in sys.modules"
        " if name.startswith('omicsclaw.')"
        " and not name.startswith('omicsclaw.bench'))\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['omicsclaw.version']"
