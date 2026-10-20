"""Workspace-scoped project settings and project context instructions.

Persists project metadata in ``<workspace>/.omicsclaw/project.json`` and
resolves ``## Project Context`` instructions for :mod:`omicsclaw.entry.assembly`.
Only ``instructions`` (and optional ``<workspace>/.omicsclaw/instructions.md``)
enter the system prompt; ``name`` and ``description`` are strictly UI-facing
project metadata and never leak into prompt assembly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

__all__ = [
    "MAX_PROJECT_NAME_LENGTH",
    "PROJECT_CONTEXT_PREAMBLE",
    "PROJECT_INSTRUCTIONS_MD_REL_PATH",
    "PROJECT_SETTINGS_REL_PATH",
    "ProjectSettings",
    "load_project_settings",
    "resolve_project_instructions",
    "save_project_settings",
]

PROJECT_SETTINGS_REL_PATH: Final = Path(".omicsclaw") / "project.json"
PROJECT_INSTRUCTIONS_MD_REL_PATH: Final = Path(".omicsclaw") / "instructions.md"
MAX_PROJECT_NAME_LENGTH: Final = 255

PROJECT_CONTEXT_PREAMBLE: Final = (
    "The following context has been provided for this project. "
    "Use it to inform your work:\n\n"
)


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    """Workspace-level project metadata and context instructions.

    ``name`` and ``description`` help users identify and organize projects in
    the desktop UI; they are never included in the model's system prompt.
    ``instructions`` holds project-specific conventions, background, and rules
    that are rendered under ``## Project Context``.
    """

    name: str = ""
    description: str = ""
    instructions: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "updated_at": self.updated_at,
        }


def _clean_str(value: Any, *, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned


def _strip_preamble(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith(PROJECT_CONTEXT_PREAMBLE.strip()):
        stripped = stripped[len(PROJECT_CONTEXT_PREAMBLE.strip()) :].strip()
    return stripped


def load_project_settings(workspace: Path | str | None) -> ProjectSettings:
    """Load :class:`ProjectSettings` from ``<workspace>/.omicsclaw/project.json``.

    Returns an empty :class:`ProjectSettings` when the file is absent or cannot
    be parsed. Supports ``"context"`` as a fallback key for ``"instructions"``.
    """
    if workspace is None:
        return ProjectSettings()
    try:
        root = Path(workspace)
        settings_path = root / PROJECT_SETTINGS_REL_PATH
        if not settings_path.is_file():
            return ProjectSettings()
        raw_bytes = settings_path.read_text(encoding="utf-8")
        parsed = json.loads(raw_bytes)
    except (OSError, ValueError, TypeError):
        return ProjectSettings()

    if not isinstance(parsed, Mapping):
        return ProjectSettings()

    raw_instructions = parsed.get("instructions")
    if not isinstance(raw_instructions, str):
        raw_instructions = parsed.get("context", "")

    return ProjectSettings(
        name=_clean_str(parsed.get("name"), max_length=MAX_PROJECT_NAME_LENGTH),
        description=_clean_str(parsed.get("description")),
        instructions=_strip_preamble(_clean_str(raw_instructions)),
        updated_at=_clean_str(parsed.get("updated_at")),
    )


def save_project_settings(
    workspace: Path | str,
    *,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
) -> ProjectSettings:
    """Atomically persist project settings to ``<workspace>/.omicsclaw/project.json``.

    Unspecified fields (``None``) retain their existing persisted values.
    """
    root = Path(workspace)
    current = load_project_settings(root)

    next_name = (
        _clean_str(name, max_length=MAX_PROJECT_NAME_LENGTH)
        if name is not None
        else current.name
    )
    next_description = (
        _clean_str(description) if description is not None else current.description
    )
    next_instructions = (
        _strip_preamble(_clean_str(instructions))
        if instructions is not None
        else current.instructions
    )
    updated_at = datetime.now(timezone.utc).isoformat()

    updated = ProjectSettings(
        name=next_name,
        description=next_description,
        instructions=next_instructions,
        updated_at=updated_at,
    )

    target_dir = root / PROJECT_SETTINGS_REL_PATH.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = root / PROJECT_SETTINGS_REL_PATH

    payload = json.dumps(updated.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".project-", suffix=".json.tmp", dir=str(target_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target_file)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return updated


def resolve_project_instructions(
    workspace: Path | str | None,
    override: str | None = None,
    *,
    include_preamble: bool = True,
) -> str:
    """Resolve the active project instructions for prompt assembly.

    Combines ``override`` (when provided by the turn's session options) or
    ``load_project_settings(workspace).instructions`` with an optional
    ``<workspace>/.omicsclaw/instructions.md`` file.

    Never reads or includes ``name`` or ``description``. Returns ``""`` when
    no project instructions are configured so :class:`PromptAssembler` omits
    the ``## Project Context`` section completely.
    """
    parts: list[str] = []

    if override is not None and isinstance(override, str) and override.strip():
        cleaned_override = _strip_preamble(override)
        if cleaned_override:
            parts.append(cleaned_override)
    elif workspace is not None:
        settings = load_project_settings(workspace)
        if settings.instructions:
            parts.append(settings.instructions)

    if workspace is not None:
        try:
            md_path = Path(workspace) / PROJECT_INSTRUCTIONS_MD_REL_PATH
            if md_path.is_file():
                md_body = _strip_preamble(md_path.read_text(encoding="utf-8"))
                if md_body and md_body not in parts:
                    parts.append(md_body)
        except OSError:
            pass

    combined = "\n\n".join(part for part in parts if part).strip()
    if not combined:
        return ""
    if not include_preamble:
        return combined
    return f"{PROJECT_CONTEXT_PREAMBLE}{combined}"
