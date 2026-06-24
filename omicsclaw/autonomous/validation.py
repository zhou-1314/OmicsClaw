"""Static validation for generated autonomous analysis code."""

from __future__ import annotations

import ast


# Modules with no legitimate analysis use that grant code-exec / process / FFI /
# dynamic-import escapes. The OS sandbox is the real boundary (ADR 0032); this list
# is best-effort early feedback. Note it cannot stop every escape — ``getattr`` on
# an already-imported module, ``sys.modules[...]`` lookups, or ``__builtins__``
# tricks remain reachable and are left to the bwrap envelope / in-kernel guard.
_BLOCKED_PYTHON_IMPORTS = {
    "ctypes",  # ctypes.CDLL('libc').system(...) — FFI shell-out
    "ftplib",
    "httpx",
    "importlib",  # closes the dynamic-import escape (importlib.import_module('subprocess'))
    "paramiko",
    "pip",
    "requests",
    "runpy",  # runpy.run_path executes an (unlinted) file
    "shutil",
    "socket",
    "subprocess",
    "urllib",
    "urllib.request",
    "webbrowser",
}

_BLOCKED_PYTHON_CALLS = {"__import__", "compile", "eval", "exec"}

# Destructive / process / exec attributes, flagged ONLY when the call target is a
# risky owner (see ``_RISKY_ATTR_OWNERS``). Matching by bare attribute name alone
# used to reject innocent same-named methods on data objects — ``oc.run('skill', …)``
# (the facade's documented public API, see build_system_prompt), ``df.rename(…)``,
# ``df.replace(…)`` — none of which touch the filesystem/process table. The owner
# gate keeps the dangerous forms blocked (``os.system``, ``subprocess.run``,
# ``sys.modules['subprocess'].run()``, ``Path('x').unlink()``). The OS sandbox /
# in-kernel guard remain the real boundary (ADR 0032).
_BLOCKED_PYTHON_ATTRS = {
    "Popen",
    "call",
    "check_call",
    "check_output",
    "chmod",
    "chown",
    "hardlink_to",
    "popen",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "run",
    "spawn",
    "symlink_to",
    "system",
    "unlink",
}

# A blocked attribute is only flagged when the ROOT of its call target is one of
# these — the risky stdlib modules and the pathlib constructors. The root is found
# by walking down the owner expression (``sys`` for ``sys.modules['subprocess']``,
# ``Path`` for ``Path('x')``). This blocks ``os.system``, ``subprocess.run``,
# ``sys.modules['subprocess'].run()`` and ``Path('x').unlink()`` while allowing the
# same attribute names on user data (``df.rename``, ``adata.obs.rename``, ``oc.run``).
# A destructive op smuggled through a plain alias (``p = Path('x'); p.unlink()``;
# ``o = os; o.system()``) has a non-risky root and passes the lint, but is still
# caught at runtime — the in-kernel guard patches the ``os`` functions pathlib
# delegates to, and bwrap confines writes to the run workspace (ADR 0032).
_RISKY_ATTR_ROOTS = {
    "PosixPath",
    "PurePath",
    "Path",
    "WindowsPath",
    "importlib",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "sys",
}

_BLOCKED_R_SNIPPETS = (
    "curl::",
    "devtools::",
    "download.file",
    "file.remove",
    "file.rename",
    "httr::",
    "install.packages",
    "remotes::",
    "rcurl",
    "setwd(",
    "socketconnection",
    "system(",
    "system2(",
    "unlink(",
    "url(",
)


def validate_generated_code(source: str, *, language: str = "python") -> list[str]:
    """Return blocking issues for generated code."""
    normalized_language = str(language or "python").strip().lower()
    if normalized_language in {"r", "rscript"}:
        return _validate_r_code(source)
    return _validate_python_code(source)


def _validate_python_code(source: str) -> list[str]:
    issues: list[str] = []
    try:
        tree = ast.parse(source or "")
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if alias.name in _BLOCKED_PYTHON_IMPORTS or root in _BLOCKED_PYTHON_IMPORTS:
                    issues.append(f"blocked import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".", 1)[0]
            if module in _BLOCKED_PYTHON_IMPORTS or root in _BLOCKED_PYTHON_IMPORTS:
                issues.append(f"blocked import-from: {module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_PYTHON_CALLS:
                issues.append(f"blocked call: {node.func.id}()")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCKED_PYTHON_ATTRS:
                root = _attr_owner_root(node.func.value)
                if root in _RISKY_ATTR_ROOTS:
                    issues.append(f"blocked attribute call: {root}.{node.func.attr}()")
    return sorted(set(issues))


def _attr_owner_root(node: ast.expr) -> str | None:
    """Return the root identifier of an attribute/subscript/call chain.

    ``sys`` for ``sys.modules['subprocess']``, ``Path`` for ``Path('x')``,
    ``adata`` for ``adata.obs`` — or None when the chain has no Name root.
    """
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return None


def _validate_r_code(source: str) -> list[str]:
    lowered = (source or "").lower()
    return [
        f"blocked R code pattern: {snippet}"
        for snippet in _BLOCKED_R_SNIPPETS
        if snippet in lowered
    ]
