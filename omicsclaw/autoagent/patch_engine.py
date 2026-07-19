"""Patch engine — parse, apply, and revert code patches.

The Meta-Agent outputs structured JSON with ``old_code``/``new_code``
hunks.  The patch engine validates these against the editable surface,
applies them with backup, and can revert on failure.

Patch lifecycle:
1. Parse LLM JSON response → PatchPlan
2. Validate all target files are within editable surface
3. Backup target files
4. Apply hunks via exact string replacement
5. If trial passes → keep changes
6. If trial fails → revert from backup
"""

from __future__ import annotations

import ast
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from omicsclaw.autoagent.edit_surface import (
    EditSurface,
    SurfacePath,
    resolve_path_within_root,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Hunk:
    """A single code replacement within one file."""

    old_code: str
    new_code: str


@dataclass
class FileDiff:
    """All hunks for one file."""

    file: str  # relative path
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class PatchPlan:
    """Complete patch output from the Meta-Agent."""

    target_files: list[str] = field(default_factory=list)
    description: str = ""
    expected_improvements: list[str] = field(default_factory=list)
    rollback_conditions: list[str] = field(default_factory=list)
    diffs: list[FileDiff] = field(default_factory=list)
    reasoning: str = ""
    converged: bool = False

    @property
    def n_hunks(self) -> int:
        return sum(len(d.hunks) for d in self.diffs)

    @property
    def diff_summary(self) -> str:
        """One-line summary of the patch size."""
        files = len(self.diffs)
        hunks = self.n_hunks
        return f"{files} file(s), {hunks} hunk(s)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_files": self.target_files,
            "description": self.description,
            "expected_improvements": self.expected_improvements,
            "rollback_conditions": self.rollback_conditions,
            "diffs": [
                {
                    "file": d.file,
                    "hunks": [{"old_code": h.old_code, "new_code": h.new_code}
                              for h in d.hunks],
                }
                for d in self.diffs
            ],
            "reasoning": self.reasoning,
            "converged": self.converged,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_patch_response(text: str) -> PatchPlan:
    """Parse the Meta-Agent's JSON response into a PatchPlan.

    Handles markdown code fences and extracts the outermost JSON object.
    """
    text = text.strip()

    # Strip markdown fences
    fence_match = re.search(
        r"```(?:json|JSON)?\s*\n(.*?)```", text, re.DOTALL
    )
    if fence_match:
        text = fence_match.group(1).strip()

    # Try direct parse
    data = _parse_json(text)
    if data is None:
        raise ValueError(f"Could not parse LLM response as JSON: {text[:200]}")

    # Check for convergence signal
    if data.get("converged"):
        return PatchPlan(
            converged=True,
            reasoning=data.get("reasoning", "LLM indicated convergence"),
        )

    # Parse patch plan
    plan_data = data.get("patch_plan", {})
    diffs_data = data.get("diffs", [])

    diffs: list[FileDiff] = []
    for diff_entry in diffs_data:
        file_path = diff_entry.get("file", "")
        hunks: list[Hunk] = []
        for hunk_data in diff_entry.get("hunks", []):
            old_code = hunk_data.get("old_code", "")
            new_code = hunk_data.get("new_code", "")
            if old_code and old_code != new_code:
                hunks.append(Hunk(old_code=old_code, new_code=new_code))
        if hunks:
            diffs.append(FileDiff(file=file_path, hunks=hunks))

    return PatchPlan(
        target_files=plan_data.get("target_files", []),
        description=plan_data.get("description", ""),
        expected_improvements=plan_data.get("expected_improvements", []),
        rollback_conditions=plan_data.get("rollback_conditions", []),
        diffs=diffs,
        reasoning=data.get("reasoning", ""),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of validating a patch against the editable surface."""

    valid: bool
    errors: list[str] = field(default_factory=list)

    @property
    def error_summary(self) -> str:
        return "; ".join(self.errors) if self.errors else "OK"


def validate_patch(
    patch: PatchPlan,
    surface: EditSurface,
) -> ValidationResult:
    """Validate target declarations, editable paths, and hunk matches."""
    errors: list[str] = []
    seen_targets: set[str] = set()

    if not patch.diffs:
        errors.append("Patch contains no diffs.")
        return ValidationResult(valid=False, errors=errors)

    declared_targets: set[str] = set()
    if not isinstance(patch.target_files, list):
        errors.append("target_files must be a list of path strings.")
        target_files: list[Any] = []
    else:
        target_files = patch.target_files

    for target_index, target in enumerate(target_files):
        if (
            not isinstance(target, str)
            or not target
            or target.strip() != target
        ):
            errors.append(
                f"target_files[{target_index}] must be a non-empty path string "
                "without surrounding whitespace."
            )
            continue
        try:
            target_path = surface.resolve_editable_path(target)
        except (PermissionError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if target_path.rel_path in declared_targets:
            errors.append(
                "target_files contains duplicate canonical target "
                f"{target_path.rel_path!r}."
            )
            continue
        declared_targets.add(target_path.rel_path)

    for diff_index, diff in enumerate(patch.diffs):
        if (
            not isinstance(diff.file, str)
            or not diff.file
            or diff.file.strip() != diff.file
        ):
            errors.append(
                f"diffs[{diff_index}].file must be a non-empty path string "
                "without surrounding whitespace."
            )
            continue
        try:
            surface_path = surface.resolve_editable_path(diff.file)
        except (PermissionError, ValueError) as exc:
            errors.append(str(exc))
            continue

        if surface_path.rel_path in seen_targets:
            errors.append(
                "Patch contains duplicate canonical target "
                f"{surface_path.rel_path!r}; combine its hunks into one FileDiff."
            )
            continue
        seen_targets.add(surface_path.rel_path)

        if not surface_path.abs_path.exists():
            errors.append(
                f"File {surface_path.rel_path!r} does not exist on disk."
            )
            continue

        try:
            content = surface_path.abs_path.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"Cannot read {surface_path.rel_path!r}: {exc}")
            continue

        for i, hunk in enumerate(diff.hunks):
            try:
                match_position = _resolve_hunk_match(
                    content,
                    hunk.old_code,
                    rel_path=surface_path.rel_path,
                )
            except ValueError as exc:
                errors.append(f"{surface_path.rel_path} hunk #{i}: {exc}")
                continue
            match_positions = [match_position]

            protected_region_error = _validate_skill_md_protected_hunk(
                rel_path=surface_path.rel_path,
                content=content,
                hunk_index=i,
                match_positions=match_positions,
                new_code=hunk.new_code,
            )
            if protected_region_error:
                errors.append(protected_region_error)
                continue

            method_scope_error = _validate_method_scope_hunk(
                surface=surface,
                rel_path=surface_path.rel_path,
                content=content,
                hunk_index=i,
                match_positions=match_positions,
            )
            if method_scope_error:
                errors.append(method_scope_error)

    missing_targets = sorted(seen_targets - declared_targets)
    if missing_targets:
        errors.append(
            "target_files is missing canonical diff target(s): "
            + ", ".join(repr(target) for target in missing_targets)
        )
    extra_targets = sorted(declared_targets - seen_targets)
    if extra_targets:
        errors.append(
            "target_files has canonical target without diff(s): "
            + ", ".join(repr(target) for target in extra_targets)
        )

    return ValidationResult(valid=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


_HunkMatchValidator = Callable[
    [int, str, list[tuple[int, int]], str],
    None,
]


def render_hunks(
    content: str,
    hunks: list[Hunk],
    *,
    rel_path: str,
) -> str:
    """Deterministically render one accepted file from its parent text.

    This is the same exact/whitespace-normalized replacement algorithm used by
    :func:`apply_patch`.  The accepted-commit boundary replays persisted hunks
    through this pure function so a manifest cannot describe different bytes
    from the content-addressed Git commit.
    """

    return _render_hunks(content, hunks, rel_path=rel_path)


def _render_hunks(
    content: str,
    hunks: list[Hunk],
    *,
    rel_path: str,
    match_validator: _HunkMatchValidator | None = None,
) -> str:
    for hunk_index, hunk in enumerate(hunks):
        match_pos = _resolve_hunk_match(
            content,
            hunk.old_code,
            rel_path=rel_path,
        )
        if match_validator is not None:
            match_validator(
                hunk_index,
                content,
                [match_pos],
                hunk.new_code,
            )
        start, end = match_pos
        content = content[:start] + hunk.new_code + content[end:]
    return content


def apply_patch(
    patch: PatchPlan,
    surface: EditSurface,
) -> list[str]:
    """Apply a validated patch to files on disk.

    Returns a list of files that were modified.

    Raises
    ------
    PermissionError
        If a patch targets a file outside the editable surface.
    ValueError
        If a path escapes the project root or a hunk cannot be found.
    """
    modified: list[str] = []
    resolved_diffs: list[tuple[FileDiff, SurfacePath]] = []
    seen_targets: set[str] = set()

    for diff in patch.diffs:
        surface_path = surface.resolve_editable_path(diff.file)
        if surface_path.rel_path in seen_targets:
            raise ValueError(
                "Patch contains duplicate canonical target "
                f"{surface_path.rel_path!r}; combine its hunks into one FileDiff."
            )
        seen_targets.add(surface_path.rel_path)
        resolved_diffs.append((diff, surface_path))

    for diff, surface_path in resolved_diffs:
        file_path = surface_path.abs_path
        content = file_path.read_text(encoding="utf-8")
        original = content

        def validate_protected_match(
            hunk_index: int,
            current_content: str,
            match_positions: list[tuple[int, int]],
            new_code: str,
        ) -> None:
            protected_region_error = _validate_skill_md_protected_hunk(
                rel_path=surface_path.rel_path,
                content=current_content,
                hunk_index=hunk_index,
                match_positions=match_positions,
                new_code=new_code,
            )
            if protected_region_error:
                raise ValueError(protected_region_error)

        content = _render_hunks(
            content,
            diff.hunks,
            rel_path=surface_path.rel_path,
            match_validator=validate_protected_match,
        )

        if content != original:
            # The candidate raw-byte witness and durable PatchPlan replay use
            # UTF-8/LF as the cross-platform output contract.  ``newline=''``
            # disables TextIO's Windows ``\n`` -> ``\r\n`` translation.
            file_path.write_text(content, encoding="utf-8", newline="")
            modified.append(surface_path.rel_path)

    return modified


def revert_files(
    files: list[str],
    project_root: Path,
    backup_dir: Path,
) -> None:
    """Revert files from backup copies."""
    for rel_path in files:
        resolved = resolve_path_within_root(project_root, rel_path)
        backup = resolve_path_within_root(backup_dir, resolved.rel_path).abs_path
        target = resolved.abs_path
        if backup.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(backup), str(target))
            logger.info("Reverted %s from backup", resolved.rel_path)
        else:
            logger.warning("No backup found for %s", resolved.rel_path)


def backup_files(
    files: list[str],
    project_root: Path,
    backup_dir: Path,
) -> None:
    """Create backup copies of files before patching."""
    for rel_path in files:
        resolved = resolve_path_within_root(project_root, rel_path)
        src = resolved.abs_path
        dst = resolve_path_within_root(backup_dir, resolved.rel_path).abs_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(str(src), str(dst))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_FRONTMATTER_OPEN_RE = re.compile(r"\A(?:\ufeff)?---[ \t]*(?:\r?\n|$)")
_FRONTMATTER_CLOSE_RE = re.compile(r"(?m)^---[ \t]*(?:\r?\n|$)")
_MARKDOWN_H2_RE = re.compile(r"(?m)^##[ \t]+[^\r\n]*(?:\r?\n|$)")
_INPUTS_OUTPUTS_H2_RE = re.compile(
    r"(?m)^##[ \t]+Inputs[ \t]*&[ \t]*Outputs[ \t]*(?:\r?\n|$)"
)
_GENERATED_IO_MARKER = "AUTO-GENERATED from skill.yaml (interface)"


def _validate_skill_md_protected_hunk(
    *,
    rel_path: str,
    content: str,
    hunk_index: int,
    match_positions: list[tuple[int, int]],
    new_code: str,
) -> str | None:
    protected_regions = _skill_md_protected_regions(rel_path, content)
    for label, start, end in protected_regions:
        if any(_ranges_intersect(match, (start, end)) for match in match_positions):
            return (
                f"{rel_path} hunk #{hunk_index}: targets protected {label}; "
                "only narrative SKILL.md sections are editable."
            )

    protected_content = _protected_skill_md_content(content, protected_regions)
    for start, end in match_positions:
        candidate = content[:start] + new_code + content[end:]
        candidate_regions = _skill_md_protected_regions(rel_path, candidate)
        if _protected_skill_md_content(candidate, candidate_regions) != protected_content:
            return (
                f"{rel_path} hunk #{hunk_index}: alters protected SKILL.md "
                "structure; YAML frontmatter and AUTO-GENERATED Inputs & "
                "Outputs boundaries must remain byte-identical."
            )
    return None


def _skill_md_protected_regions(
    rel_path: str,
    content: str,
) -> list[tuple[str, int, int]]:
    if Path(rel_path).name != "SKILL.md":
        return []

    regions: list[tuple[str, int, int]] = []
    opening = _FRONTMATTER_OPEN_RE.match(content)
    if opening is not None:
        closing = _FRONTMATTER_CLOSE_RE.search(content, opening.end())
        end = closing.end() if closing is not None else len(content)
        regions.append(("YAML frontmatter", 0, end))

    for heading in _INPUTS_OUTPUTS_H2_RE.finditer(content):
        next_heading = _MARKDOWN_H2_RE.search(content, heading.end())
        end = next_heading.start() if next_heading is not None else len(content)
        if _GENERATED_IO_MARKER in content[heading.end():end]:
            regions.append(
                ("AUTO-GENERATED Inputs & Outputs section", heading.start(), end)
            )
    return regions


def _protected_skill_md_content(
    content: str,
    regions: list[tuple[str, int, int]],
) -> list[tuple[str, str]]:
    return [(label, content[start:end]) for label, start, end in regions]


def _validate_method_scope_hunk(
    *,
    surface: EditSurface,
    rel_path: str,
    content: str,
    hunk_index: int,
    match_positions: list[tuple[int, int]],
) -> str | None:
    method_focus = surface.metadata.get("method_focus")
    if not isinstance(method_focus, dict):
        return None

    blocked_map = method_focus.get("blocked_functions")
    if not isinstance(blocked_map, dict):
        return None

    blocked_functions = blocked_map.get(rel_path)
    if not blocked_functions:
        return None

    blocked_regions = _resolve_python_function_regions(content, blocked_functions)
    if not blocked_regions:
        return None

    if any(
        not _region_intersects_any(match, blocked_regions)
        for match in match_positions
    ):
        return None

    blocked_names = sorted({
        name
        for name, start, end in blocked_regions
        for match in match_positions
        if _ranges_intersect(match, (start, end))
    })
    focus_method = str(method_focus.get("method", "") or "current").strip()
    blocked_text = ", ".join(blocked_names) if blocked_names else "non-target method code"
    return (
        f"{rel_path} hunk #{hunk_index}: targets non-target method code "
        f"({blocked_text}) while optimizing method '{focus_method}'."
    )


def _parse_json(text: str) -> dict[str, Any] | None:
    """Parse JSON, handling edge cases.

    Delegates to the shared :func:`~omicsclaw.autoagent.llm_client.parse_json_from_llm`
    implementation.
    """
    from omicsclaw.autoagent.llm_client import parse_json_from_llm

    return parse_json_from_llm(text)


def _normalize_ws(s: str) -> str:
    """Normalize whitespace for fuzzy matching."""
    return " ".join(s.split())


def _find_all_occurrences(content: str, old_code: str) -> list[tuple[int, int]]:
    if not old_code:
        return []

    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        index = content.find(old_code, start)
        if index == -1:
            break
        matches.append((index, index + len(old_code)))
        start = index + 1
    return matches


def _resolve_hunk_match(
    content: str,
    old_code: str,
    *,
    rel_path: str,
) -> tuple[int, int]:
    """Resolve one exact or uniquely whitespace-normalized hunk match."""
    matches = _find_all_occurrences(content, old_code)
    match_kind = ""
    if not matches:
        matches = _find_all_normalized(content, old_code)
        match_kind = " whitespace-normalized"

    if not matches:
        raise ValueError(
            f"Hunk old_code not found in {rel_path}: {old_code[:80]!r}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous{match_kind} hunk: old_code appears {len(matches)} "
            f"times in {rel_path}. Provide more surrounding context in "
            f"old_code. (first 80 chars: {old_code[:80]!r})"
        )
    return matches[0]


def _resolve_python_function_regions(
    content: str,
    function_names: list[str],
) -> list[tuple[str, int, int]]:
    if not function_names:
        return []

    try:
        module = ast.parse(content)
    except SyntaxError:
        return []

    line_offsets = [0]
    for line in content.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))

    regions: list[tuple[str, int, int]] = []
    wanted = set(function_names)
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in wanted or node.end_lineno is None:
            continue
        start = line_offsets[node.lineno - 1]
        end = line_offsets[node.end_lineno]
        regions.append((node.name, start, end))
    return regions


def _ranges_intersect(
    left: tuple[int, int],
    right: tuple[int, int],
) -> bool:
    return left[0] < right[1] and left[1] > right[0]


def _region_intersects_any(
    match: tuple[int, int],
    regions: list[tuple[str, int, int]],
) -> bool:
    return any(_ranges_intersect(match, (start, end)) for _name, start, end in regions)


def _find_all_normalized(content: str, old_code: str) -> list[tuple[int, int]]:
    """Find every line-aligned whitespace-normalized old_code occurrence."""
    normalized_old = _normalize_ws(old_code)
    if not normalized_old:
        return []

    lines = content.splitlines(keepends=True)
    old_lines = old_code.strip().splitlines()
    if not old_lines:
        return []

    first_norm = _normalize_ws(old_lines[0])
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line))

    matches: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if first_norm in _normalize_ws(line):
            end_line = i + len(old_lines)
            if end_line > len(lines):
                continue
            candidate = "".join(lines[i:end_line])
            if _normalize_ws(candidate) == normalized_old:
                matches.append((line_offsets[i], line_offsets[end_line]))

    return matches
