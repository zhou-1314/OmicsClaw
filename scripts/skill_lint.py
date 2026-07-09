#!/usr/bin/env python3
"""Lint OmicsClaw skills against the canonical contract (dual-track, ADR 0037).

Contract detection:
- **v2** — a `skill.yaml` is present → validated against the declarative schema
  (`omicsclaw.skill.schema`) plus the narrative/script checks that still apply
  (`_lint_v2`).
- **v1** — a `parameters.yaml` sidecar (no `skill.yaml`) → the legacy contract,
  byte-unchanged.
- **pre-migration** — neither sidecar → lint clean by default so migration can
  proceed one PR at a time without breaking CI.

Usage:
    python scripts/skill_lint.py <skill_dir>          # one skill
    python scripts/skill_lint.py --all                # every skill under skills/
    python scripts/skill_lint.py --all --strict       # treat warnings as errors
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.parameters_md import render_parameters_md  # noqa: E402
from omicsclaw.skill.execution.flag_introspection import (  # noqa: E402
    RUNNER_BLOCKED_FLAGS as _RUNNER_BLOCKED_FLAGS,
    consensus_parser_flags as _consensus_parser_flags,
    effective_allowed_flags as _effective_allowed_flags,
    extract_argparse_flags as _extract_argparse_flags,
    params_dump_with_effective_flags as _params_dump_with_effective_flags,
)

REQUIRED_SECTIONS = (
    "## When to use",
    "## Inputs & Outputs",
    "## Flow",
    "## Gotchas",
    "## Key CLI",
    "## See also",
)

REQUIRED_REFERENCES = ("methodology.md", "output_contract.md", "parameters.md")

ALLOWED_FRONTMATTER_KEYS = {
    "name", "description", "version", "author", "license", "tags", "requires",
}

REQUIRED_SIDECAR_KEYS = {
    "domain", "script", "saves_h5ad", "requires_preprocessed",
    "trigger_keywords", "legacy_aliases", "allowed_extra_flags", "param_hints",
}

MAX_BODY_LINES = 200
MAX_DESCRIPTION_WORDS = 50


def _parse_skill_md(skill_dir: Path) -> tuple[dict, str] | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    body = parts[2].lstrip("\n")
    return frontmatter, body


_SKIP_CLAUSES = ("skip when", "skip if", "skip for")


def _check_description(description: str) -> list[str]:
    errors: list[str] = []
    desc = (description or "").strip()
    # Whitespace-normalise so YAML folded scalars (`>-` / `|`) that wrap
    # "Skip when" across lines, or that introduce double spaces, still match.
    normalised = " ".join(desc.lower().split())
    if not normalised.startswith("load when"):
        errors.append("description: must start with 'Load when'")
    if not any(clause in normalised for clause in _SKIP_CLAUSES):
        errors.append(
            "description: must include a 'Skip when' / 'Skip if' / 'Skip for' "
            "clause"
        )
    if len(desc.split()) > MAX_DESCRIPTION_WORDS:
        errors.append(
            f"description: must be <= {MAX_DESCRIPTION_WORDS} words "
            f"(found {len(desc.split())})"
        )
    return errors


def _check_body(body: str) -> list[str]:
    errors: list[str] = []
    line_count = len(body.splitlines())
    if line_count > MAX_BODY_LINES:
        errors.append(
            f"body: exceeds {MAX_BODY_LINES} lines (found {line_count})"
        )
    # Line-anchored match: a heading must start the line (after optional
    # whitespace).  Avoids false positives from HTML comments or prose that
    # quotes a section name inline.
    body_lines = [ln.lstrip() for ln in body.splitlines()]
    for section in REQUIRED_SECTIONS:
        if not any(line.startswith(section) for line in body_lines):
            errors.append(f"body: missing required section '{section}'")
    return errors


def _check_requires_complete(skill_dir: Path) -> list[str]:
    """Fail when frontmatter `requires:` omits a statically-provable dependency.

    Conservative (Codex-reviewed): MISSING real deps are errors; stale extras are
    only warned about by the standalone `scripts/audit_skill_requires.py` (skills
    that delegate to ``omicsclaw.*`` legitimately declare more than is importable).
    Auto-fix with ``python scripts/audit_skill_requires.py --write``.
    """
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from audit_skill_requires import audit_skill
    except Exception:  # pragma: no cover - import path / optional yaml
        return []
    result = audit_skill(skill_dir)
    if result["missing"]:
        return [
            "requires: missing statically-detected deps "
            f"{result['missing']} — run `python scripts/audit_skill_requires.py --write`"
        ]
    return []


def _check_frontmatter_keys(frontmatter: dict) -> list[str]:
    errors: list[str] = []
    extra = set(frontmatter) - ALLOWED_FRONTMATTER_KEYS
    if "metadata" in extra:
        meta = frontmatter.get("metadata") or {}
        if isinstance(meta, dict) and "omicsclaw" in meta:
            errors.append(
                "frontmatter: legacy 'metadata.omicsclaw' block must be removed "
                "from v2 skills (runtime fields live in skill.yaml — ADR 0037)"
            )
        extra.discard("metadata")
    if extra:
        errors.append(
            f"frontmatter: unexpected keys {sorted(extra)} "
            f"(allowed: {sorted(ALLOWED_FRONTMATTER_KEYS)})"
        )
    return errors


def _check_sidecar(sidecar: dict) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_SIDECAR_KEYS - set(sidecar)
    for key in sorted(missing):
        errors.append(f"parameters.yaml: missing required field '{key}'")
    flags = sidecar.get("allowed_extra_flags", []) or []
    for flag in flags:
        if not isinstance(flag, str) or not flag.startswith("--"):
            errors.append(
                f"parameters.yaml: allowed_extra_flags entry {flag!r} "
                f"must be a string starting with '--'"
            )
    if "param_hints" in sidecar and not isinstance(sidecar["param_hints"], dict):
        errors.append("parameters.yaml: param_hints must be a dict")
    return errors


_GOTCHA_FILE_LINE_RE = re.compile(r"`?([\w./-]+\.py):(\d+)(?:-(\d+))?`?")
_GOTCHA_RESULT_JSON_RE = re.compile(r"`?result\.json(?:\[[\"'][^\"']+[\"']\])+`?")
_GOTCHA_RESULT_KEY_RE = re.compile(r"\[[\"']([^\"']+)[\"']\]")
_GOTCHA_TABLE_FIG_RE = re.compile(r"`?(?:tables|figures)/([\w._-]+)`?")
_GOTCHA_BULLET_RE = re.compile(r"^\s*-\s+(.*)$", re.MULTILINE)
_GOTCHA_EMPTY_BULLET_RE = re.compile(
    r"^[-*\s_`]*(no gotchas yet|none yet|no gotchas surfaced)\b",
    re.IGNORECASE,
)


def _check_gotchas_anchors(
    skill_dir: Path, body: str, sidecar: dict
) -> list[str]:
    """Verify every code anchor in the Gotchas section grep-resolves.

    The dominant hallucination pattern from PR #4 review was Gotchas that
    described "the desired script" rather than what the code actually does —
    references to `result.json` keys that didn't exist, table filenames that
    weren't written, file:line anchors past EOF.  This lint catches all
    three by greping the skill's `script` for each anchor before the PR
    leaves the contributor's branch.
    """
    errors: list[str] = []

    section = re.search(
        r"^## Gotchas\b\s*\n(.*?)(?=^## |\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        return []  # missing-section error already raised by _check_body
    block = section.group(1)

    # Per-bullet processing.  Skip the empty-template marker bullet only
    # when the bullet's lead matches it (so a real Gotcha that *mentions*
    # the phrase "none yet" inside its prose does not silently bypass the
    # anchor lint).
    real_bullets: list[str] = []
    for bullet in _GOTCHA_BULLET_RE.findall(block):
        if _GOTCHA_EMPTY_BULLET_RE.match(bullet):
            continue
        real_bullets.append(bullet)
    if not real_bullets:
        return []

    script_name = sidecar.get("script") or ""
    script_path = skill_dir / script_name if script_name else None
    if not (script_path and script_path.exists()):
        return []  # script not co-located — skip rather than false-fail
    script_text = script_path.read_text(encoding="utf-8")
    script_line_count = script_text.count("\n") + 1

    real_block = "\n".join(real_bullets)

    # TODO(cross-file): when an anchor references a sibling library file
    # (e.g. `_lib/de.py:485-506`, used in spatial-de Gotchas), the
    # filename's basename will not match `script_name` so the line-bound
    # check is skipped.  A project-wide grep would close the gap, but
    # would require resolving the path relative to the repo root.
    for fname, l1, l2 in _GOTCHA_FILE_LINE_RE.findall(real_block):
        if Path(fname).name != Path(script_name).name:
            continue  # cross-file anchor — see TODO above
        upper = int(l2) if l2 else int(l1)
        if upper > script_line_count:
            anchor = f"{fname}:{l1}" + (f"-{l2}" if l2 else "")
            errors.append(
                f"gotchas: anchor {anchor} exceeds {script_name} length "
                f"({script_line_count} lines)"
            )

    for full in _GOTCHA_RESULT_JSON_RE.findall(real_block):
        keys = _GOTCHA_RESULT_KEY_RE.findall(full)
        if keys and keys[0] not in script_text:
            errors.append(
                f'gotchas: result.json["{keys[0]}"] not referenced in '
                f"{script_name}"
            )

    for fname in _GOTCHA_TABLE_FIG_RE.findall(real_block):
        if fname not in script_text:
            errors.append(
                f"gotchas: '{fname}' not referenced in {script_name}"
            )

    return errors


# --- Check: allowed_extra_flags override (ADR 0041) -----------------------
#
# The runner filters LLM-supplied `--foo` flags against the set a skill
# accepts (`argv_builder.filter_forwarded_args`). That set is now DERIVED at
# runtime from the script's argparse surface (leaf) or the consensus run
# parser (consensus) — see `omicsclaw.skill.execution.flag_introspection`, the
# single source of truth this lint also imports. `allowed_extra_flags` is no
# longer a hand-maintained mirror; a skill lists it only to NARROW below the
# derived surface (e.g. a consensus shim exposing a curated subset). The check
# below only guards such an override against typos / stale flags.


def _check_allowed_extra_flags(skill_dir: Path, sidecar: dict) -> list[str]:
    """Guard an optional `allowed_extra_flags` override against drift.

    Empty / absent is the norm after ADR 0041 — the runtime derives the
    allow-list from the script, so there is nothing to check. When a skill
    *does* declare the list (a deliberate narrowing override) every entry must
    be a flag the script actually accepts; otherwise the override silently
    drops a real flag or references a stale one.
    """
    allowed = set(sidecar.get("allowed_extra_flags") or [])
    if not allowed:
        return []  # derive-mode: nothing declared, nothing to verify
    script_name = sidecar.get("script") or ""
    script_path = skill_dir / script_name if script_name else None
    if not (script_path and script_path.exists()):
        return []  # script not co-located — skip rather than false-fail
    script_text = script_path.read_text(encoding="utf-8")
    declared = _extract_argparse_flags(script_text) - _RUNNER_BLOCKED_FLAGS
    errors: list[str] = []
    for flag in sorted(allowed - declared):
        errors.append(
            f"parameters.yaml: allowed_extra_flags lists '{flag}' but "
            f"{script_name} does not declare it via add_argument"
        )
    return errors


def _check_hint_flags_accepted(skill_dir: Path, sidecar: dict, hints: dict) -> list[str]:
    """Every flag a per-method hint names must be one the gate actually accepts.

    `hints.*.(params|advanced_params)` feed the parameter card and autoagent
    search space as CLI flags (`--kebab`). If a hint names a flag outside the
    skill's effective allow-list — derived from the script (leaf) or the declared
    consensus subset — the runner silently drops it, so the suggestion / tuned
    value is a no-op. Runner-blocked framework flags are exempt (they are
    injected by the runner, not forwarded from hints).
    """
    if not isinstance(hints, dict) or not hints:
        return []
    skill_type = _skill_type(sidecar)
    script_name = sidecar.get("script") or ""
    if skill_type != "consensus":
        # A leaf's allow-list is DERIVED from the script; with no co-located
        # script there is nothing to derive, so skip rather than flag every hint
        # (mirrors _check_allowed_extra_flags). Consensus uses its declared list.
        script_path = skill_dir / script_name if script_name else None
        if not (script_path and script_path.exists()):
            return []
    allowed = _effective_allowed_flags(
        sidecar.get("allowed_extra_flags"), skill_dir, script_name, skill_type
    )
    named: set[str] = set()
    for info in hints.values():
        if not isinstance(info, dict):
            continue
        for key in ("params", "advanced_params"):
            for param in info.get(key) or []:
                named.add("--" + str(param).replace("_", "-"))
    errors: list[str] = []
    for flag in sorted(named - allowed - _RUNNER_BLOCKED_FLAGS):
        errors.append(
            f"skill.yaml (interface.parameters): hint names '{flag}' but the skill "
            f"does not accept it (not in the script's flags / declared override) — "
            f"the runner would drop it"
        )
    return errors


def _check_corpus_source_refs(
    skill_dir: Path, provenance_origin: str, source_ref: str | None, hints: dict
) -> list[str]:
    """P5 iron rule (acquisition-plan.md §P5): never ship a fabricated default.

    For a corpus-derived skill (`provenance.origin == "corpus"`), every
    `hints.*.defaults[param]` must have a matching `source_refs[param]` that
    is a real, well-formed `{quote, char_span, doc_ref}` triple — never a
    `{"todo": True}` placeholder standing in for a live default. When
    `references/source_corpus.txt` is present, `char_span` is re-sliced out
    of it and must equal `quote` — the actual anti-fabrication check, not
    just a structural one (file-exists-guarded, mirroring
    `_check_allowed_extra_flags`'s tolerance for a transitional skill).

    A no-op for any non-corpus skill (the overwhelming majority) — returns
    `[]` immediately, so this rule cannot regress an existing skill's lint
    status.
    """
    if provenance_origin != "corpus":
        return []
    errors: list[str] = []
    if not (source_ref or "").strip():
        errors.append(
            "skill.yaml (provenance): origin is 'corpus' but source_ref (DOI/URL/PMID) is not set"
        )

    corpus_text: str | None = None
    corpus_path = skill_dir / "references" / "source_corpus.txt"
    if corpus_path.exists():
        corpus_text = corpus_path.read_text(encoding="utf-8")

    for method, info in (hints or {}).items():
        if not isinstance(info, dict):
            continue
        defaults = info.get("defaults")
        if not isinstance(defaults, dict):
            continue
        source_refs = info.get("source_refs")
        if not isinstance(source_refs, dict):
            source_refs = {}
        for param, value in defaults.items():
            ref = source_refs.get(param)
            if not isinstance(ref, dict) or ref.get("todo") is True:
                errors.append(
                    f"skill.yaml (interface.parameters.hints.{method}): "
                    f"'{param}' has a default ({value!r}) but no source_ref — "
                    "corpus-derived defaults must never be unsourced"
                )
                continue
            quote, span, doc_ref = ref.get("quote"), ref.get("char_span"), ref.get("doc_ref")
            # Bounds are validated as real ints with 0 <= start < end BEFORE any
            # slicing: Python silently CLAMPS out-of-range/negative slice indices
            # instead of raising, so a naive `span[0] < span[1]` check alone would
            # let a bogus span (e.g. [-14, 999]) sail through as "verified" —
            # exactly the fabrication the iron rule exists to catch.
            span_valid = (
                isinstance(span, list)
                and len(span) == 2
                and isinstance(span[0], int)
                and isinstance(span[1], int)
                and not isinstance(span[0], bool)
                and not isinstance(span[1], bool)
                and 0 <= span[0] < span[1]
            )
            malformed = not quote or not doc_ref or not span_valid
            if malformed:
                errors.append(
                    f"skill.yaml (interface.parameters.hints.{method}): "
                    f"source_refs['{param}'] is malformed (needs quote/char_span/doc_ref)"
                )
                continue
            if corpus_text is not None and (
                span[1] > len(corpus_text) or corpus_text[span[0]:span[1]] != quote
            ):
                errors.append(
                    f"skill.yaml (interface.parameters.hints.{method}): "
                    f"source_refs['{param}'] char_span does not slice out its own quote "
                    "in references/source_corpus.txt"
                )
    return errors


# --- Type dispatch (ADR 0030) ---------------------------------------------
#
# `type` is an optional sidecar field: `leaf` (default) | `consensus` |
# `workflow` | `knowledge` | `adapter`.  Only `consensus` currently has a
# distinct profile (a shim over the consensus runtime, ADR 0016). `workflow` is
# RESERVED for a future skill-composition type and, like everything else
# (including a missing/unknown `type`), lints as `leaf` for now, so the existing
# single-script skills are byte-unchanged.

_SKILL_TYPES = ("leaf", "workflow", "consensus", "knowledge", "adapter")
_CONSENSUS_RUN_MODULE = "omicsclaw.runtime.consensus.run"


def _skill_type(sidecar: dict) -> str:
    value = sidecar.get("type") or "leaf"
    return value if value in _SKILL_TYPES else "leaf"


def _const_truthiness(node: ast.AST) -> bool | None:
    """Truthiness of a constant test (`if False:` / `if 0:` → False), else None."""
    return bool(node.value) if isinstance(node, ast.Constant) else None


def _top_level_main(tree: ast.AST) -> ast.AST | None:
    """The module-level `def main(...)` / `async def main(...)`, if any."""
    body = getattr(tree, "body", [])
    for node in body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "main"
        ):
            return node
    return None


def _block_terminates(stmts: list[ast.stmt]) -> bool:
    """True iff executing ``stmts`` always reaches a `return`/`raise` — i.e. any
    statement that follows this block on the same path is unreachable.

    Handles the constant-branch and both-branches-terminate cases so dead code
    after `if True: return` / `if/else` (both return) is also recognised.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        if isinstance(stmt, ast.If):
            truth = _const_truthiness(stmt.test)
            if truth is True:
                if _block_terminates(stmt.body):
                    return True
            elif truth is False:
                if _block_terminates(stmt.orelse):
                    return True
            elif _block_terminates(stmt.body) and _block_terminates(stmt.orelse):
                return True
    return False


def _reachable_calls(stmts: list[ast.stmt]):
    """Yield `ast.Call` nodes reachable in a statement list.

    - Prunes the bodies of trivially-constant `if`/`while` branches (so a call
      under `if False:` does not count).
    - Does NOT descend into nested function/class definitions (so a call in a
      helper is off the `main` path).
    - STOPS scanning a block after an unconditional terminator (`return`/`raise`,
      or a branch construct that terminates on all live paths), so a call placed
      after `return 0` is dead code and does not count. The terminator's own
      expression is still inspected before stopping.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.If, ast.While)):
            truth = _const_truthiness(stmt.test)
            if truth is True:
                yield from _reachable_calls(stmt.body)
                if _block_terminates(stmt.body):
                    return
                continue
            if truth is False:
                yield from _reachable_calls(stmt.orelse)
                if _block_terminates(stmt.orelse):
                    return
                continue
            yield from _reachable_calls(stmt.body)
            yield from _reachable_calls(stmt.orelse)
            if _block_terminates(stmt.body) and _block_terminates(stmt.orelse):
                return
            continue
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                yield from _reachable_calls([child])
            else:
                for node in ast.walk(child):
                    if isinstance(node, ast.Call):
                        yield node
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return


def _is_run_main_delegation(
    node: ast.AST, run_main_names: set[str], module_aliases: set[str]
) -> bool:
    """True iff ``node`` calls the run-main target with the EXACT live-delegation
    shape ``main(["--source", SOURCE, *argv])`` — three list elements:
    ``"--source"``, the name ``SOURCE``, and ``*argv`` (the local argv name).

    Rejects non-forwarding shapes such as ``*[]`` / ``*sys.argv[1:]`` and any
    extra/short element count, so the lint proves the shim forwards user argv.
    """
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    calls_run_main = (
        isinstance(func, ast.Name) and func.id in run_main_names
    ) or (
        isinstance(func, ast.Attribute)
        and func.attr == "main"
        and isinstance(func.value, ast.Name)
        and func.value.id in module_aliases
    )
    if not calls_run_main:
        return False
    first = node.args[0]
    if not isinstance(first, ast.List) or len(first.elts) != 3:
        return False
    e0, e1, e2 = first.elts
    return (
        isinstance(e0, ast.Constant)
        and e0.value == "--source"
        and isinstance(e1, ast.Name)
        and e1.id == "SOURCE"
        and isinstance(e2, ast.Starred)
        and isinstance(e2.value, ast.Name)
        and e2.value.id == "argv"
    )


def _analyse_consensus_shim(tree: ast.AST) -> tuple[set[str], set[str], str | None, bool]:
    """Structurally inspect a consensus shim's AST (ADR 0016/0030).

    Returns ``(run_main_names, module_aliases, source_value, delegates)``:
    - ``run_main_names`` — local names bound to
      ``omicsclaw.runtime.consensus.run.main`` via ``import ... as``.
    - ``module_aliases`` — local names bound to the run *module* via
      ``import omicsclaw.runtime.consensus.run as ...``.
    - ``source_value`` — the string assigned to a module-level ``SOURCE = "..."``.
    - ``delegates`` — True iff the shim's top-level ``main`` makes a REACHABLE
      call to the run-main target shaped exactly ``["--source", SOURCE, *argv]``.
      Calls in helper functions, module-level dead code, or under ``if False:``
      do NOT count — the shim must forward user argv on its live ``main`` path.
    """
    run_main_names: set[str] = set()
    module_aliases: set[str] = set()
    source_value: str | None = None

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _CONSENSUS_RUN_MODULE:
            for alias in node.names:
                if alias.name == "main":
                    run_main_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _CONSENSUS_RUN_MODULE:
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "SOURCE"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    source_value = node.value.value

    main_fn = _top_level_main(tree)
    delegates = main_fn is not None and any(
        _is_run_main_delegation(call, run_main_names, module_aliases)
        for call in _reachable_calls(main_fn.body)
    )
    return run_main_names, module_aliases, source_value, delegates


def _consensus_sources() -> set[str] | None:
    """Keys of ``CONSENSUS_SOURCES`` — None if the runtime can't be imported.

    The lint degrades gracefully in minimal envs: an import failure skips the
    SOURCE-membership check rather than crashing the whole run.
    """
    try:
        from omicsclaw.runtime.consensus.sources import CONSENSUS_SOURCES
    except Exception:
        return None
    return set(CONSENSUS_SOURCES)


# `_consensus_parser_flags` is the shared `flag_introspection.consensus_parser_flags`
# (imported at module top) — one source of truth for the consensus flag surface.


def _check_consensus_shim(skill_dir: Path, sidecar: dict) -> list[str]:
    """Validate a `type: consensus` shim against the consensus runtime (ADR 0016).

    Consensus skills are thin shims whose argparse surface lives in the shared
    ``runtime/consensus/run`` parser, not in the shim — so the leaf
    `allowed_extra_flags ↔ add_argument` check is replaced by: the shim
    delegates to the run entry, defines a `SOURCE` that resolves in
    ``CONSENSUS_SOURCES``, and only lists flags the generic run parser accepts.
    """
    errors: list[str] = []
    script_name = sidecar.get("script") or ""
    script_path = skill_dir / script_name if script_name else None
    if not (script_path and script_path.exists()):
        return [f"type=consensus: declared script {script_name!r} is missing"]
    text = script_path.read_text(encoding="utf-8")

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"type=consensus: shim {script_name} is not parseable ({exc})"]

    run_main_names, module_aliases, source, delegates = _analyse_consensus_shim(tree)

    imports_run = bool(run_main_names or module_aliases)
    if not imports_run:
        errors.append(
            f"type=consensus: shim must import {_CONSENSUS_RUN_MODULE}.main "
            f"(not found in {script_name})"
        )
    if source is None:
        errors.append(
            f'type=consensus: shim must define SOURCE = "<flavour>" in {script_name}'
        )
    # The key structural check: a real CALL to run-main, not just the strings.
    # Only assert it once the import + SOURCE prerequisites exist, so the error
    # list names the root cause rather than piling on.
    if imports_run and source is not None and not delegates:
        errors.append(
            "type=consensus: shim's main() must delegate as "
            f'main(["--source", SOURCE, *argv]) in {script_name} '
            "(no reachable main()-path call forwarding argv found)"
        )

    sources = _consensus_sources()
    if source is not None and sources is not None and source not in sources:
        errors.append(
            f"type=consensus: SOURCE {source!r} not in CONSENSUS_SOURCES "
            f"{sorted(sources)}"
        )

    allowed = set(sidecar.get("allowed_extra_flags") or [])
    if not allowed:
        # Unlike leaf skills, a consensus shim cannot derive its allow-list: the
        # run parser exposes every flavour's flags plus --help/--source, so an
        # absent list would over-expose them. The curated subset must be explicit.
        errors.append(
            "type=consensus: allowed_extra_flags must be declared (a curated "
            "subset of the consensus run parser; it is not auto-derived)"
        )
    parser_flags = _consensus_parser_flags()
    if parser_flags is not None:
        unknown = allowed - parser_flags - _RUNNER_BLOCKED_FLAGS
        for flag in sorted(unknown):
            errors.append(
                f"type=consensus: allowed_extra_flags lists {flag!r} which the "
                f"consensus run parser does not accept"
            )
    return errors


# --- Check: output_contract.md paths exist in the script ------------------
#
# `references/output_contract.md` is supposed to describe the files the
# script actually writes.  PR-F discovered that generated output-contract headers copy the
# legacy SKILL.md "Output Structure" section verbatim, so output_contract.md
# often lists files the script never touches.  This lint forces every
# `tables/X.csv` / `figures/X.png` / etc. mentioned in the contract to
# appear as a substring in the script (and any sibling `_lib/*.py`).

# Match file-shaped tokens with extension.  Multi-dot extensions like
# `checksums.sha256` and `archive.tar.gz` are listed BEFORE their shorter
# prefixes (regex alternation is leftmost — order matters).
_OUTPUT_CONTRACT_PATH_RE = re.compile(
    r"""
    `?
    (?:[a-z_<>-]+/)?       # optional parent dir
    (
        [a-zA-Z][\w._-]*    # filename stem
        \.
        (?:                 # extension — longest matches first
            tar\.gz | sha256 | h5ad | narrowPeak | broadPeak |
            fasta | fastq | json | jpeg | jpg | html |
            csv | tsv | png | svg | pdf | bed | gmt | sam | bam | vcf | txt |
            md  | sh  | fa
        )
    )
    `?
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Framework-standard files written by the common report helper (NOT by the
# skill script directly) — exempt from the substring check.  Also exempt
# self-referencing doc filenames that an output_contract.md header comment
# may accidentally surface.
_FRAMEWORK_FILES = frozenset({
    "report.md", "result.json",
    "commands.sh", "requirements.txt", "checksums.sha256",
    "processed.h5ad", "processed.bam",
    "SKILL.md", "output_contract.md", "parameters.md",
    "methodology.md", "r_visualization.md",
    "manifest.json",
})
# Strip HTML comments — generated output contracts prepend a `<!-- Generated ... -->`
# header that contains references to output_contract.md / SKILL.md which
# would otherwise be mis-counted as path claims.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# `from skills.<domain>._lib.<module> import ...` or
# `from .._lib.<module> import ...` — captures <module> only, used to
# scope the cross-file haystack search (avoid falsely-validating filenames
# that live in unrelated _lib siblings).
_LIB_IMPORT_RE = re.compile(
    r"from\s+(?:[\w.]+\._lib\.|\.+_lib\.)(\w+)\s+import"
)


def _check_output_contract_paths(skill_dir: Path, sidecar: dict) -> list[str]:
    """Verify every file path in output_contract.md is referenced in the script.

    Searches the script + any `_lib/*.py` siblings for the basename as a
    substring.  Framework-standard outputs (report.md, result.json,
    processed.h5ad, etc.) are exempt — those are written by the common
    report helper, not by the skill script directly.
    """
    errors: list[str] = []
    contract = skill_dir / "references" / "output_contract.md"
    if not contract.exists():
        return []  # missing-file already flagged by _check_references

    contract_text = _HTML_COMMENT_RE.sub("", contract.read_text(encoding="utf-8"))
    # Strip code-fence content?  No — code fences in output_contract.md are
    # the canonical "Output Structure" tree; the paths there ARE the claims.
    referenced_paths = {
        match.group(0).strip("`")
        for match in _OUTPUT_CONTRACT_PATH_RE.finditer(contract_text)
    }
    if not referenced_paths:
        return []  # nothing to validate

    script_name = sidecar.get("script") or ""
    script_path = skill_dir / script_name if script_name else None
    if not (script_path and script_path.exists()):
        return []  # script not co-located — skip rather than false-fail

    script_text = script_path.read_text(encoding="utf-8")
    haystack = script_text
    # Also scan _lib/<X>.py — but ONLY the modules the script actually
    # imports.  Otherwise spatial-domains' contract gets validated against
    # _lib/communication.py, _lib/cnv.py, etc., and the lint passes for
    # filenames that no spatial-domains output ever writes.  Walk up to the
    # `skills/<domain>/` boundary so two-deep trees (singlecell/scrna/X)
    # find _lib at the domain root.
    imported_lib_names = set(_LIB_IMPORT_RE.findall(script_text))
    for parent in (skill_dir, *skill_dir.parents):
        lib_dir = parent / "_lib"
        if lib_dir.is_dir():
            for lib_py in sorted(lib_dir.glob("*.py")):
                if lib_py.stem in imported_lib_names:
                    haystack += "\n" + lib_py.read_text(encoding="utf-8")
        if parent.name == "skills":
            break

    for path in sorted(referenced_paths):
        basename = path.rsplit("/", 1)[-1]
        if basename in _FRAMEWORK_FILES:
            continue
        if basename not in haystack:
            errors.append(
                f"output_contract.md: '{path}' not referenced in "
                f"{script_name} (or sibling _lib/*.py)"
            )
    return errors


def _check_parameters_md_fresh(skill_dir: Path, params: dict, *, source: str) -> list[str]:
    """Flag a stale `references/parameters.md` (only when the file exists).

    `params` carries `allowed_extra_flags` plus the hints, keyed for `source`
    (`param_hints` for v1, `hints` for v2). Both tracks share this check so a
    migrated skill's generated doc is kept fresh exactly like a v1 sidecar.
    """
    params_md = skill_dir / "references" / "parameters.md"
    if not params_md.exists():
        return []
    expected = render_parameters_md(params, source=source)
    if params_md.read_text(encoding="utf-8") != expected:
        return [
            "references/parameters.md: stale — regenerate with "
            "scripts/generate_parameters_md.py"
        ]
    return []


def _check_references(skill_dir: Path, sidecar: dict) -> list[str]:
    errors: list[str] = []
    refs = skill_dir / "references"
    for name in REQUIRED_REFERENCES:
        if not (refs / name).exists():
            errors.append(f"references/{name}: missing")
    errors.extend(_check_parameters_md_fresh(skill_dir, sidecar, source="v1"))
    return errors


# v2 narrative SKILL.md drops the hand-written "Inputs & Outputs" fact section
# (those facts live in skill.yaml.interface; ADR 0037).
_V2_REQUIRED_SECTIONS = tuple(s for s in REQUIRED_SECTIONS if s != "## Inputs & Outputs")


def _lint_v2(skill_dir: Path) -> list[str]:
    """Lint a v2 skill (skill.yaml present, ADR 0037).

    Validates the machine contract against the schema, then runs the
    narrative/script checks that still apply, including references/parameters.md
    freshness (generator is now dual-track). A schema-invalid skill stops early
    (the manifest can't be trusted). Deferred until the narrative migration
    lands: requires->deps.python audit, and v2 frontmatter-key rules.
    """
    from omicsclaw.skill.schema import load_skill_yaml, validate_skill_yaml

    sy = skill_dir / "skill.yaml"
    schema_errors = validate_skill_yaml(sy)
    if schema_errors:
        return [f"skill.yaml: {e}" for e in schema_errors]

    manifest = load_skill_yaml(sy)
    errors: list[str] = []

    # references/parameters.md must be regenerated from this manifest when
    # present (generator is dual-track; ADR 0037). File-exists-guarded so a
    # transitional v2 skill without the rendered doc is tolerated.
    errors.extend(
        _check_parameters_md_fresh(
            skill_dir,
            _params_dump_with_effective_flags(
                manifest.interface.parameters.model_dump(),
                skill_dir,
                manifest.runtime.entry,
                manifest.type,
            ),
            source="v2",
        )
    )

    # runtime.entry must exist (Codex cross-validation): the leaf flag/output
    # checks silently no-op when the script is absent, so a valid skill.yaml
    # pointing at a missing script would otherwise lint green. `draft` skills may
    # not have a script yet; consensus shims are covered by _check_consensus_shim.
    if manifest.type != "consensus" and manifest.lifecycle.status != "draft":
        if not (skill_dir / manifest.runtime.entry).exists():
            errors.append(
                f"runtime.entry: script '{manifest.runtime.entry}' not found in skill dir "
                "(set lifecycle.status: draft if intentional)"
            )

    # Parity with the v1 "Skip when" contract (Codex cross-validation): the v1
    # description lint forces a Skip clause, so v2 forces >=1 skip rule here
    # (lint-level, not parse-level, so the schema stays flexible).
    if not manifest.summary.skip_when:
        errors.append(
            "skill.yaml: summary.skip_when must declare >=1 rule "
            "(parity with the v1 'Skip when' description contract)"
        )

    # Narrative SKILL.md is still the human card in v2 (header generated from
    # skill.yaml). Lint its sections/length when present; tolerate its absence
    # during transition.
    parsed = _parse_skill_md(skill_dir)
    body = parsed[1] if parsed else ""
    if body:
        line_count = len(body.splitlines())
        if line_count > MAX_BODY_LINES:
            errors.append(f"SKILL.md body: exceeds {MAX_BODY_LINES} lines (found {line_count})")
        body_lines = [ln.lstrip() for ln in body.splitlines()]
        for section in _V2_REQUIRED_SECTIONS:
            if not any(line.startswith(section) for line in body_lines):
                errors.append(f"SKILL.md body: missing required section '{section}'")

    # Synthesize a sidecar-equivalent so the existing script-anchored checks
    # (driven by script + allowed_extra_flags + type) run unchanged.
    synth = {
        "script": manifest.runtime.entry,
        "allowed_extra_flags": list(manifest.interface.parameters.allowed_extra_flags),
        "type": manifest.type,
    }
    if body:
        errors.extend(_check_gotchas_anchors(skill_dir, body, synth))
    if manifest.type == "consensus":
        errors.extend(_check_consensus_shim(skill_dir, synth))
    else:
        flag_errors = _check_allowed_extra_flags(skill_dir, synth)
        errors.extend(
            e.replace("parameters.yaml:", "skill.yaml (interface.parameters):")
            for e in flag_errors
        )
        errors.extend(_check_output_contract_paths(skill_dir, synth))
    # Applies to every type: a per-method hint must not name a flag the gate drops.
    errors.extend(
        _check_hint_flags_accepted(skill_dir, synth, manifest.interface.parameters.hints)
    )
    # P5: a corpus-derived skill must never ship a fabricated numeric default.
    errors.extend(
        _check_corpus_source_refs(
            skill_dir,
            manifest.provenance.origin,
            manifest.provenance.source_ref,
            manifest.interface.parameters.hints,
        )
    )
    return errors


def lint_skill(skill_dir: Path) -> list[str]:
    """Return a list of lint errors for one skill directory.

    Empty list = clean.  v2 (skill.yaml present) lints via the schema + v2 path;
    v1 (parameters.yaml, no skill.yaml) keeps the legacy contract byte-unchanged;
    pre-migration skills (neither sidecar) always return [].
    """
    if (skill_dir / "skill.yaml").exists():
        return _lint_v2(skill_dir)

    parsed = _parse_skill_md(skill_dir)
    if parsed is None:
        return [f"{skill_dir}: SKILL.md missing or unparseable"]
    frontmatter, body = parsed

    sidecar_path = skill_dir / "parameters.yaml"
    if not sidecar_path.exists():
        return []  # legacy skill — defer until migrated

    try:
        sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"parameters.yaml: invalid YAML ({exc})"]

    errors: list[str] = []
    # Type-agnostic checks (apply to every skill type).
    errors.extend(_check_description(frontmatter.get("description", "")))
    errors.extend(_check_body(body))
    errors.extend(_check_frontmatter_keys(frontmatter))
    errors.extend(_check_sidecar(sidecar))
    errors.extend(_check_references(skill_dir, sidecar))
    errors.extend(_check_gotchas_anchors(skill_dir, body, sidecar))
    errors.extend(_check_requires_complete(skill_dir))

    # Type-specific profile (ADR 0016/0030).  `consensus` shims delegate their
    # argparse + outputs to the shared consensus runtime, so the leaf flag-match
    # and output-contract substring checks would false-fail; validate the shim
    # wiring instead.  `leaf`, the reserved `workflow`, and `knowledge`/`adapter`
    # keep the full leaf contract unchanged.
    if _skill_type(sidecar) == "consensus":
        errors.extend(_check_consensus_shim(skill_dir, sidecar))
    else:
        errors.extend(_check_allowed_extra_flags(skill_dir, sidecar))
        errors.extend(_check_output_contract_paths(skill_dir, sidecar))
    return errors


def discover_skills(skills_root: Path) -> list[Path]:
    """Every directory containing a SKILL.md or a skill.yaml, recursively."""
    dirs = {p.parent for p in skills_root.rglob("SKILL.md")}
    dirs |= {p.parent for p in skills_root.rglob("skill.yaml")}
    return sorted(dirs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("skill_dir", nargs="?", type=Path)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="(reserved — currently same as default)")
    args = parser.parse_args()

    if args.all == bool(args.skill_dir):
        parser.error("provide either <skill_dir> or --all")

    from omicsclaw.skill.registry import SKILLS_DIR

    targets = discover_skills(SKILLS_DIR) if args.all else [args.skill_dir]

    total_errors = 0
    for skill_dir in targets:
        errors = lint_skill(skill_dir)
        if errors:
            print(f"FAIL {skill_dir}")
            for err in errors:
                print(f"  - {err}")
            total_errors += len(errors)
        else:
            print(f"ok   {skill_dir}")

    if total_errors:
        print(f"\n{total_errors} error(s) across {len(targets)} skill(s)")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
