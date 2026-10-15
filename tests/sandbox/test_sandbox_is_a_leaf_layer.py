"""``omicsclaw.sandbox`` is a leaf layer: imports only the standard library.

Enforces three architectural boundaries:

*Downward.* ``omicsclaw/sandbox/`` imports only Python's standard library and
nothing else inside ``omicsclaw``.

*Upward / Sideways.* Neither ``omicsclaw/tools/`` nor ``omicsclaw/kernel/`` nor
any other lower layer imports ``omicsclaw.sandbox`` — only ``omicsclaw.entry``
wires ``SandboxManager`` into ``BashTool`` and ``KernelManager``.

*Runtime.* Driving the sandbox layer (probing backends, starting the localhost
egress proxy, building profiles) loads no forbidden package into ``sys.modules``.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PACKAGE = _REPO_ROOT / "omicsclaw" / "sandbox"

_OTHER_LAYERS = (
    "schema",
    "provider",
    "engine",
    "tools",
    "context",
    "skills",
    "mcp",
    "memory",
    "kernel",
)

_FORBIDDEN_AT_RUNTIME = (
    "omicsclaw.tools",
    "omicsclaw.schema",
    "omicsclaw.engine",
    "omicsclaw.provider",
    "omicsclaw.providers",
    "omicsclaw.context",
    "omicsclaw.entry",
    "omicsclaw.mcp",
    "omicsclaw.runtime",
    "omicsclaw.autonomous",
    "omicsclaw.memory",
    "omicsclaw.skills",
    "omicsclaw.kernel",
    "docker",
    "pydantic",
    "requests",
)

_DYNAMIC_IMPORT_CALLS = frozenset(
    {
        "__import__",
        "import_module",
        "reload",
        "spec_from_file_location",
        "module_from_spec",
        "exec_module",
        "load_module",
    }
)


def _module_paths(package: pathlib.Path) -> list[pathlib.Path]:
    return sorted(package.rglob("*.py"))


def _imported_modules(path: pathlib.Path) -> list[str]:
    """Absolute module names imported by *path*, relative ones resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(_REPO_ROOT).with_suffix("").parts[:-1])
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                if node.module:
                    names.append(node.module)
                continue
            climbed = len(package) - (node.level - 1)
            base = package[:climbed] if climbed > 0 else []
            names.append(".".join([*base, node.module] if node.module else base))
    return names


def test_the_package_has_modules_to_check():
    assert _module_paths(_PACKAGE)


@pytest.mark.parametrize("path", _module_paths(_PACKAGE), ids=lambda p: p.name)
def test_a_sandbox_module_imports_no_other_omicsclaw_package(path: pathlib.Path):
    offenders = [
        name
        for name in _imported_modules(path)
        if name.split(".")[0] == "omicsclaw"
        and not (name == "omicsclaw.sandbox" or name.startswith("omicsclaw.sandbox."))
    ]

    assert not offenders, f"{path.name} imports {offenders}"


@pytest.mark.parametrize("path", _module_paths(_PACKAGE), ids=lambda p: p.name)
def test_a_sandbox_module_imports_only_the_standard_library(path: pathlib.Path):
    offenders = [
        name
        for name in _imported_modules(path)
        if name.split(".")[0] not in sys.stdlib_module_names
        and name.split(".")[0] != "omicsclaw"
    ]

    assert not offenders, f"{path.name} imports non-stdlib {offenders}"


@pytest.mark.parametrize("path", _module_paths(_PACKAGE), ids=lambda p: p.name)
def test_a_sandbox_module_imports_nothing_at_runtime(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", ""))
        in _DYNAMIC_IMPORT_CALLS
    ]

    assert not offenders, f"{path.name} imports dynamically at lines {offenders}"


@pytest.mark.parametrize("path", _module_paths(_PACKAGE), ids=lambda p: p.name)
def test_a_sandbox_module_never_logs(path: pathlib.Path):
    """Like ``mcp``: report through values, let the composition root log."""
    assert "logging" not in _imported_modules(path)


@pytest.mark.parametrize("layer", _OTHER_LAYERS)
def test_no_other_rebuild_layer_imports_the_sandbox(layer: str):
    offenders = [
        f"{layer}/{path.name}"
        for path in _module_paths(_REPO_ROOT / "omicsclaw" / layer)
        for name in _imported_modules(path)
        if name == "omicsclaw.sandbox" or name.startswith("omicsclaw.sandbox.")
    ]

    assert not offenders, f"{offenders} import omicsclaw.sandbox"


def test_kernel_layer_does_not_import_sandbox_entry_or_tools():
    kernel_dir = _REPO_ROOT / "omicsclaw" / "kernel"
    forbidden = ("omicsclaw.sandbox", "omicsclaw.entry", "omicsclaw.tools")
    offenders = [
        f"kernel/{path.name}:{name}"
        for path in _module_paths(kernel_dir)
        for name in _imported_modules(path)
        if any(name == pkg or name.startswith(pkg + ".") for pkg in forbidden)
    ]
    assert not offenders, f"kernel layer has forbidden imports: {offenders}"


def _probe(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=120,
    )


_BEHAVIOUR_PROBE = """
import asyncio
import sys
import tempfile
from pathlib import Path

from omicsclaw.sandbox import (
    EgressProxy,
    SandboxConfig,
    build_analysis_profile,
    probe_sandbox_backend,
)


async def main(root: str) -> None:
    ws = Path(root) / "ws"
    ws.mkdir()
    cfg = SandboxConfig()
    _ = probe_sandbox_backend(cfg.backend)
    _ = build_analysis_profile(ws, cfg)
    proxy = EgressProxy(egress_groups=("pkg",))
    await proxy.start()
    assert proxy.port > 0
    await proxy.stop()


with tempfile.TemporaryDirectory() as root:
    asyncio.run(main(root))

FORBIDDEN = __FORBIDDEN__
print(sorted(
    name for name in sys.modules
    if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
))
"""


def test_running_the_layer_loads_nothing_it_must_not():
    source = _BEHAVIOUR_PROBE.replace("__FORBIDDEN__", repr(_FORBIDDEN_AT_RUNTIME))
    result = _probe(source)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"running the layer loaded {result.stdout}"


@pytest.mark.parametrize("layer", _OTHER_LAYERS)
def test_importing_another_layer_does_not_load_the_sandbox(layer: str):
    result = _probe(
        f"import sys, omicsclaw.{layer}; print('omicsclaw.sandbox' in sys.modules)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_the_public_surface_is_deliberate():
    import omicsclaw.sandbox as sandbox

    assert sandbox.__all__ == sorted(sandbox.__all__), "keep the list sorted"
    assert all(hasattr(sandbox, name) for name in sandbox.__all__)
    assert set(sandbox.__all__) == {
        "AnalysisProfile",
        "AppContainerBackend",
        "AppContainerPolicy",
        "BackendName",
        "BlockedEvent",
        "BwrapBackend",
        "CREDENTIAL_FILENAMES",
        "CommandOutcome",
        "DEFAULT_ALLOWED_DOMAINS",
        "DEFAULT_DENY_READ_PATHS",
        "DEFAULT_DENY_WRITE_GLOBS",
        "EGRESS_DENYLIST",
        "EGRESS_GROUPS",
        "EgressDecision",
        "EgressMode",
        "EgressProxy",
        "IsolationLevel",
        "KernelSpawnAdapter",
        "Level",
        "Mount",
        "NetnsBackend",
        "PACKAGE_RC_FILES",
        "ProbeReport",
        "SandboxBackendName",
        "SandboxConfig",
        "SandboxConfigError",
        "SandboxError",
        "SandboxManager",
        "SandboxMode",
        "SandboxSession",
        "SandboxStatus",
        "SandboxUnavailable",
        "SeatbeltBackend",
        "UpstreamProxy",
        "build_analysis_profile",
        "build_appcontainer_policy",
        "build_bwrap_argv",
        "build_netns_argv",
        "build_seatbelt_argv",
        "domain_matches",
        "escape_sbpl_literal",
        "evaluate_egress",
        "extract_blocked_host",
        "format_egress_blocked_hint",
        "host_user",
        "is_ip_allowed",
        "parse_upstream_proxy",
        "probe_appcontainer",
        "probe_bwrap",
        "probe_netns",
        "probe_sandbox_backend",
        "probe_seatbelt",
        "render_seatbelt_profile",
    }



def test_envsupply_layer_does_not_import_sandbox_entry_or_tools():
    envsupply_dir = _REPO_ROOT / "omicsclaw" / "envsupply"
    forbidden = ("omicsclaw.sandbox", "omicsclaw.entry", "omicsclaw.tools", "omicsclaw.kernel")
    offenders = [
        f"envsupply/{path.name}:{name}"
        for path in _module_paths(envsupply_dir)
        for name in _imported_modules(path)
        if any(name == pkg or name.startswith(pkg + ".") for pkg in forbidden)
    ]
    assert not offenders, f"envsupply layer has forbidden imports: {offenders}"
