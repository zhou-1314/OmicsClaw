"""Every optional backend a skill's code asks for is declared by that skill (plan 0061 case 7)."""

from __future__ import annotations

import ast
from pathlib import Path

from omicsclaw.skillenv.registry import parse_dependencies, read_registry, resolve

from .conftest import REAL_REGISTRY, REPO

EXCLUDED_MODULES: dict[str, str] = {}
EXCEPTIONS: dict[tuple[str, str], str] = {}

_API = {"require", "is_available", "install_hint", "get_dependency"}


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "skills._sdk":
            aliases |= {a.asname or a.name for a in node.names if a.name == "deps"}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "skills._sdk.deps":
            direct |= {a.asname or a.name for a in node.names if a.name in _API | {"get"}}
        elif isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == "skills._sdk.deps" and a.asname}
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in direct:
            names.add(node.args[0].value)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in aliases:
            names.add(node.args[0].value)
    return names


def _lib_modules(script: Path) -> set[Path]:
    """The ``skills.<domain>._lib`` modules *script* imports directly."""
    found: set[Path] = set()
    for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
        modules: list[tuple[str, list[str]]] = []
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append((node.module, [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            modules.extend((a.name, []) for a in node.names)
        for module, names in modules:
            parts = module.split(".")
            if len(parts) < 3 or parts[0] != "skills" or parts[2] != "_lib":
                continue
            base = REPO.joinpath(*parts)
            if base.with_suffix(".py").is_file():
                found.add(base.with_suffix(".py"))
                continue
            for name in names:
                if (base / f"{name}.py").is_file():
                    found.add(base / f"{name}.py")
                elif (base / "__init__.py").is_file():
                    found.add(base / "__init__.py")
            if not names and (base / "__init__.py").is_file():
                found.add(base / "__init__.py")
    return found


def undeclared() -> dict[tuple[str, str], set[str]]:
    """(skill, key) → where the call was found, for every call the skill does not declare."""
    registry = read_registry(REAL_REGISTRY)
    out: dict[tuple[str, str], set[str]] = {}
    for skill_md in sorted((REPO / "skills").rglob("SKILL.md")):
        directory = skill_md.parent
        declared = {
            resolve(name, registry).key or name
            for name in parse_dependencies(skill_md.read_text(encoding="utf-8"), source=skill_md)
        }
        for script in sorted(p for p in directory.glob("*.py")
                             if not p.name.startswith("_") or p.name in ("_api.py", "kernel.py")):
            for source in {script} | _lib_modules(script):
                where = source.relative_to(REPO).as_posix()
                if where in EXCLUDED_MODULES:
                    continue
                for name in _call_names(source):
                    key = resolve(name, registry).key or name
                    if key not in declared:
                        out.setdefault((directory.name, key), set()).add(where)
    return out


def test_every_called_backend_is_declared_by_its_skill():
    missing = {pair: sorted(where) for pair, where in undeclared().items() if pair not in EXCEPTIONS}
    assert missing == {}


def test_every_exception_is_still_needed():
    """A stale exception — a skill that no longer exists, or a hit that went away — fails."""
    found = set(undeclared())
    assert set(EXCEPTIONS) - found == set()


def test_exclusions_and_exceptions_carry_reasons():
    for reason in (*EXCLUDED_MODULES.values(), *EXCEPTIONS.values()):
        assert len(reason) > 40
    for path in EXCLUDED_MODULES:
        assert (REPO / path).is_file()


def test_an_aliased_deps_module_is_collected(tmp_path):
    source = tmp_path / "x.py"
    source.write_text(
        "from skills._sdk import deps as dm\n"
        "from skills._sdk.deps import get\n"
        "def f():\n"
        "    dm.require('louvain')\n"
        "    get('gseapy')\n"
        "    {}.get('not-a-dependency')\n",
        encoding="utf-8",
    )
    assert _call_names(source) == {"louvain", "gseapy"}


def test_scan_includes_api_and_its_direct_domain_helpers(tmp_path, monkeypatch):
    monkeypatch.setitem(undeclared.__globals__, "REPO", tmp_path)
    directory = tmp_path / "skills" / "singlecell" / "test-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("## Dependencies\n\n`numpy`\n")
    (directory / "_api.py").write_text(
        "from skills._sdk.deps import require\n"
        "from skills.singlecell._lib.helper import compute\n"
        "def analyze():\n    require('scanpy')\n    require('not-declared')\n"
    )
    helper = tmp_path / "skills" / "singlecell" / "_lib" / "helper.py"
    helper.parent.mkdir()
    helper.write_text("from skills._sdk.deps import require\nrequire('scrublet')\n")
    assert undeclared() == {
        ("test-skill", "scanpy"): {"skills/singlecell/test-skill/_api.py"},
        ("test-skill", "not-declared"): {"skills/singlecell/test-skill/_api.py"},
        ("test-skill", "scrublet"): {"skills/singlecell/_lib/helper.py"},
    }
