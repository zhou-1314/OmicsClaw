"""Static validation for generated autonomous analysis code."""

from __future__ import annotations

import ast


_BLOCKED_PYTHON_IMPORTS = {
    "ftplib",
    "httpx",
    "paramiko",
    "pip",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "urllib",
    "urllib.request",
    "webbrowser",
}

_BLOCKED_PYTHON_CALLS = {"__import__", "compile", "eval", "exec"}

_BLOCKED_PYTHON_ATTRS = {
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
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else "object"
                issues.append(f"blocked attribute call: {owner}.{node.func.attr}()")
    return sorted(set(issues))


def _validate_r_code(source: str) -> list[str]:
    lowered = (source or "").lower()
    return [
        f"blocked R code pattern: {snippet}"
        for snippet in _BLOCKED_R_SNIPPETS
        if snippet in lowered
    ]
