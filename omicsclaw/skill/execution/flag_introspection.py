"""Derive the CLI flags a skill actually accepts, from the skill's own script.

Single source of truth for "which ``--flags`` are legitimate" — consumed by
both the runtime gate (``argv_builder.filter_forwarded_args`` via the registry)
and ``scripts/skill_lint.py``.  Before ADR 0041 every skill hand-listed this in
``skill.yaml``'s ``allowed_extra_flags`` and a lint enforced it equalled the
script's argparse surface — a redundant mirror kept in sync by hand.  Now the
runtime derives it directly and ``allowed_extra_flags`` survives only as an
optional *narrowing override* (see :func:`effective_allowed_flags`).

Two flag sources exist:

* **leaf / default** — the flags the script declares via ``add_argument``.
* **consensus** (ADR 0016) — a thin shim delegates to the shared
  ``omicsclaw.runtime.consensus.run`` parser, so the accepted flags live there,
  not in the shim.  Consensus skills expose a hand-picked *subset* of those
  flags, so their ``allowed_extra_flags`` is genuine, non-derivable information
  and is always kept.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Iterable, Mapping

# Framework-injected flags the runner resolves itself; a skill never needs to
# (and must not) re-declare them.  Mirrors ``schema.RESERVED_FLAGS`` and
# ``argv_builder._BLOCKED_FLAGS``.
RUNNER_BLOCKED_FLAGS = frozenset({"--input", "--output", "--demo"})

# Locate each ``add_argument(`` call so we can scan its body for every
# ``--flag`` literal — handles short+long pairs like ``add_argument("-m",
# "--method")`` and multi-line calls.  Requires a literal ``--`` so single-dash
# short flags (e.g. ``-m``) are correctly NOT captured.
_ADD_ARGUMENT_OPEN_RE = re.compile(r"add_argument\s*\(")
_FLAG_LITERAL_RE = re.compile(r"""["'](--[\w-]+)["']""")


def _module_assignments(tree: ast.Module) -> dict[str, ast.AST]:
    assignments: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            assignments[statement.target.id] = statement.value
    return assignments


def _static_string_values(
    node: ast.AST,
    assignments: dict[str, ast.AST],
    resolving: frozenset[str] = frozenset(),
    external_values: Mapping[str, set[str]] | None = None,
) -> set[str] | None:
    """Resolve bounded literal values without importing or executing a Skill."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: set[str] = set()
        for element in node.elts:
            resolved = _static_string_values(
                element,
                assignments,
                resolving,
                external_values,
            )
            if resolved is None:
                return None
            values.update(resolved)
        return values
    if isinstance(node, ast.Dict):
        values: set[str] = set()
        for key in node.keys:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return None
            values.add(key.value)
        return values
    if isinstance(node, ast.Name):
        if node.id in resolving:
            return None
        if node.id in assignments:
            return _static_string_values(
                assignments[node.id],
                assignments,
                resolving | {node.id},
                external_values,
            )
        if external_values is not None and node.id in external_values:
            return set(external_values[node.id])
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_values(
            node.left,
            assignments,
            resolving,
            external_values,
        )
        right = _static_string_values(
            node.right,
            assignments,
            resolving,
            external_values,
        )
        if left is None or right is None:
            return None
        return left | right
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"list", "tuple", "set"}
            and len(node.args) == 1
            and not node.keywords
        ):
            return _static_string_values(
                node.args[0],
                assignments,
                resolving,
                external_values,
            )
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "keys"
            and not node.args
            and not node.keywords
        ):
            return _static_string_values(
                node.func.value,
                assignments,
                resolving,
                external_values,
            )
    return None


def argparse_flag_accepts_value(
    script_text: str,
    flag: str,
    value: str,
    *,
    external_values: Mapping[str, set[str]] | None = None,
) -> bool | None:
    """Statically decide whether one argparse flag accepts a value.

    ``True`` covers either an open string flag or a resolved literal choice.
    ``False`` means the flag is absent or the resolved choices reject the
    value. ``None`` means a choices expression exists but cannot be proven
    without executing Skill code; governed plan binding must fail closed on
    that state.
    """
    try:
        tree = ast.parse(script_text)
    except (SyntaxError, ValueError):
        return None
    assignments = _module_assignments(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            (isinstance(function, ast.Attribute) and function.attr == "add_argument")
            or (isinstance(function, ast.Name) and function.id == "add_argument")
        ):
            continue
        declared_flags = {
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and argument.value.startswith("--")
        }
        if flag not in declared_flags:
            continue
        choices = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "choices"),
            None,
        )
        if choices is None or (
            isinstance(choices, ast.Constant) and choices.value is None
        ):
            return True
        accepted = _static_string_values(
            choices,
            assignments,
            external_values=external_values,
        )
        return None if accepted is None else value in accepted
    return False


def _project_root_for_script(script_path: Path) -> Path | None:
    for parent in script_path.parents:
        if parent.name == "skills":
            return parent.parent
    return None


def _read_stable_python_source(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"refusing symbolic-link Python source: {path}")
    before = path.read_bytes()
    text = before.decode("utf-8")
    if path.read_bytes() != before:
        raise ValueError(f"Python source changed while being inspected: {path}")
    return text


def _controlled_import_values(
    tree: ast.Module,
    *,
    script_path: Path,
) -> dict[str, set[str]]:
    """Resolve literal constants imported from project-local ``skills`` modules."""
    project_root = _project_root_for_script(script_path)
    if project_root is None:
        return {}
    values: dict[str, set[str]] = {}
    for statement in tree.body:
        if (
            not isinstance(statement, ast.ImportFrom)
            or statement.level != 0
            or not statement.module
            or not statement.module.startswith("skills.")
        ):
            continue
        relative = Path(*statement.module.split("."))
        module_path = project_root / relative.with_suffix(".py")
        if not module_path.is_file():
            module_path = project_root / relative / "__init__.py"
        try:
            module_text = _read_stable_python_source(module_path)
            module_tree = ast.parse(module_text)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        assignments = _module_assignments(module_tree)
        for imported in statement.names:
            if imported.name == "*" or imported.name not in assignments:
                continue
            resolved = _static_string_values(
                assignments[imported.name],
                assignments,
            )
            if resolved is not None:
                values[imported.asname or imported.name] = resolved
    return values


def argparse_path_flag_accepts_value(
    script_path: str | Path,
    flag: str,
    value: str,
) -> bool | None:
    """Path-aware value check with controlled project-local import resolution."""
    path = Path(os.path.abspath(Path(script_path).expanduser()))
    try:
        text = _read_stable_python_source(path)
        tree = ast.parse(text)
        external_values = _controlled_import_values(tree, script_path=path)
        accepted = argparse_flag_accepts_value(
            text,
            flag,
            value,
            external_values=external_values,
        )
        if _read_stable_python_source(path) != text:
            raise ValueError(f"Python source changed while being inspected: {path}")
        return accepted
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return None


def extract_argparse_flags(script_text: str) -> set[str]:
    """Find every ``--flag`` literal inside an ``add_argument(...)`` call body.

    Walks the source balancing parens so ``default=foo(bar)`` and similar
    nested calls don't truncate the body early.
    """
    flags: set[str] = set()
    i = 0
    n = len(script_text)
    while i < n:
        match = _ADD_ARGUMENT_OPEN_RE.search(script_text, i)
        if not match:
            break
        body_start = match.end()
        depth = 1
        j = body_start
        while j < n and depth > 0:
            if script_text[j] == "(":
                depth += 1
            elif script_text[j] == ")":
                depth -= 1
            j += 1
        body = script_text[body_start : j - 1]
        for fm in _FLAG_LITERAL_RE.finditer(body):
            flags.add(fm.group(1))
        i = j
    return flags


def consensus_parser_flags() -> set[str] | None:
    """Long-option flags accepted by the shared consensus run parser.

    Returns ``None`` when the consensus runtime cannot be imported (optional
    dependency surface), so callers can degrade rather than hard-fail.
    """
    try:
        from omicsclaw.runtime.consensus.run import _build_parser

        parser = _build_parser()
    except Exception:
        return None
    return {
        opt
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--")
    }


def derive_accepted_flags(skill_dir: Path, script_name: str, skill_type: str) -> set[str]:
    """Compute the flags a skill accepts, minus the runner-blocked trio.

    ``consensus`` skills resolve against the shared consensus parser; every
    other type reads the co-located script's argparse surface.  Returns an
    empty set when the source is unavailable (missing script / un-importable
    consensus runtime) — the gate then fails closed, dropping all extra flags.
    """
    if skill_type == "consensus":
        # A consensus shim's accepted surface is a hand-curated *subset* of the
        # shared run parser (the parser also exposes --help/--source and every
        # other flavour's flags). It cannot be derived, so it MUST be declared
        # in ``allowed_extra_flags`` (skill_lint enforces this). With no override
        # we fail *closed* — return empty so the gate drops all extra flags —
        # rather than fail *open* by exposing the whole parser.
        return set()

    if not script_name:
        return set()
    script_path = skill_dir / script_name
    if not script_path.exists():
        return set()
    try:
        text = script_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return extract_argparse_flags(text) - RUNNER_BLOCKED_FLAGS


def effective_allowed_flags(
    declared: Iterable[str] | None,
    skill_dir: Path,
    script_name: str,
    skill_type: str,
) -> set[str]:
    """Resolve the flag allow-list the runtime gate should enforce.

    A non-empty ``declared`` (``skill.yaml``'s ``allowed_extra_flags``) is an
    explicit *narrowing override* and wins verbatim — this is how consensus
    skills expose a curated subset of the parser's flags.  When ``declared`` is
    empty or absent (the common leaf case after ADR 0041) the accepted flags
    are derived from the script.
    """
    declared_set = {f for f in (declared or ()) if f}
    if declared_set:
        return declared_set
    return derive_accepted_flags(skill_dir, script_name, skill_type)


def effective_allowed_flags_from_script_text(
    declared: Iterable[str] | None,
    script_text: str,
    skill_type: str,
) -> set[str]:
    """Same override-else-derive rule as :func:`effective_allowed_flags`, but for
    callers holding the script source in memory before it is on disk (the
    scaffolder writes ``parameters.md`` before the script). Consensus fails
    closed — it must declare its curated subset.
    """
    declared_set = {f for f in (declared or ()) if f}
    if declared_set:
        return declared_set
    if skill_type == "consensus":
        return set()
    return extract_argparse_flags(script_text) - RUNNER_BLOCKED_FLAGS


def params_dump_with_effective_flags(
    params_dump: dict,
    skill_dir: Path,
    script_name: str,
    skill_type: str,
) -> dict:
    """A manifest ``parameters`` dump with ``allowed_extra_flags`` resolved.

    Replaces the (now usually empty) declared list with the effective
    derived-or-override set so ``references/parameters.md`` documents the flags
    the runtime gate actually accepts, not the empty override field.
    """
    out = dict(params_dump)
    out["allowed_extra_flags"] = sorted(
        effective_allowed_flags(
            params_dump.get("allowed_extra_flags"), skill_dir, script_name, skill_type
        )
    )
    return out
