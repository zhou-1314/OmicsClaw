"""Canonical discovery and physical location rules for OmicsClaw Skills.

The inventory is the one filesystem-facing Module that understands how a
scientific Domain, a storage Collection, and a Skill directory compose.  It
does not own Skill metadata, lifecycle, routing, publication, or governance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml


CURATED_COLLECTION = "curated"
RUN_DERIVED_COLLECTION = "run-derived"
USER_INSTALLED_COLLECTION = "user-installed"
RUN_DERIVED_DIRNAME = "run-derived"
USER_SKILLS_DIRNAME = "user"


@dataclass(frozen=True, slots=True)
class SkillLocation:
    """One validated physical Skill location below a canonical Skills root."""

    skills_root: Path
    skill_dir: Path
    relative_path: Path
    domain: str
    collection: str
    canonical_id: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SkillInventory:
    """Deterministically ordered formal Skill locations."""

    skills_root: Path
    locations: tuple[SkillLocation, ...]


def _manifest_identity(skill_dir: Path) -> tuple[str, str]:
    manifest_path = skill_dir / "skill.yaml"
    if manifest_path.is_file():
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"skill.yaml must be a mapping: {manifest_path}")
        return (
            str(raw.get("id") or raw.get("name") or skill_dir.name).strip(),
            str(raw.get("domain") or "").strip().lower(),
        )
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                raw = yaml.safe_load(parts[1]) or {}
                if isinstance(raw, dict):
                    metadata = raw.get("metadata") or {}
                    omicsclaw = (
                        metadata.get("omicsclaw") or {}
                        if isinstance(metadata, dict)
                        else {}
                    )
                    return (
                        str(raw.get("name") or skill_dir.name).strip(),
                        str(omicsclaw.get("domain") or "").strip().lower(),
                    )
    parameters_path = skill_dir / "parameters.yaml"
    if parameters_path.is_file():
        raw = yaml.safe_load(parameters_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"parameters.yaml must be a mapping: {parameters_path}"
            )
        return (
            skill_dir.name,
            str(raw.get("domain") or "").strip().lower(),
        )
    return skill_dir.name, ""


def _top_level_runtime_scripts(skill_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in skill_dir.glob("*.py")
            if path.name != "__init__.py" and not path.name.startswith("test_")
        )
    )


def _looks_like_skill_dir(skill_dir: Path) -> bool:
    if (skill_dir / "skill.yaml").is_file() or (skill_dir / "SKILL.md").is_file():
        return True
    expected = skill_dir / f"{skill_dir.name.replace('-', '_')}.py"
    return expected.is_file() or len(_top_level_runtime_scripts(skill_dir)) == 1


def _is_enabled(skill_dir: Path) -> bool:
    state_path = skill_dir / ".omicsclaw-extension-state.json"
    if not state_path.is_file():
        return True
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return True
    return bool(raw.get("enabled", True)) if isinstance(raw, dict) else True


def _sorted_child_dirs(parent: Path) -> tuple[Path, ...]:
    children: list[Path] = []
    for path in parent.iterdir():
        if path.name.startswith((".", "__", "_")):
            continue
        if path.is_symlink():
            raise ValueError(
                f"Skill inventory path must not be a symbolic link: {path}"
            )
        if path.is_dir():
            children.append(path)
    return tuple(
        sorted(children, key=lambda path: (path.name.casefold(), path.name))
    )


def _is_internal(relative_parts: tuple[str, ...]) -> bool:
    return any(part.startswith((".", "__", "_")) for part in relative_parts)


def _is_nested_orchestrator(relative_parts: tuple[str, ...]) -> bool:
    return (
        bool(relative_parts)
        and relative_parts[0] != "orchestrator"
        and "orchestrator" in relative_parts[1:]
    )


def _candidate_skill_dirs(root: Path) -> set[Path]:
    """Return Skill-shaped directories admitted by the closed layout grammar.

    Discovery stops below a recognized Skill. This is essential because a
    Skill may own arbitrary ``tests/`` and helper packages that can themselves
    contain manifests or a single top-level Python module.
    """

    candidates: set[Path] = set()
    for domain_path in _sorted_child_dirs(root):
        domain_parts = domain_path.relative_to(root).parts
        if _is_internal(domain_parts):
            continue
        if _looks_like_skill_dir(domain_path):
            candidates.add(domain_path.resolve())
            # The historical top-level orchestrator is both a runnable Skill
            # and the container for orchestrator sub-Skills.
            if domain_path.name != "orchestrator":
                continue

        for child in _sorted_child_dirs(domain_path):
            child_parts = child.relative_to(root).parts
            if _is_internal(child_parts) or _is_nested_orchestrator(child_parts):
                continue
            if _looks_like_skill_dir(child):
                candidates.add(child.resolve())
                continue

            for grandchild in _sorted_child_dirs(child):
                grandchild_parts = grandchild.relative_to(root).parts
                if _is_internal(grandchild_parts) or _is_nested_orchestrator(
                    grandchild_parts
                ):
                    continue
                if _looks_like_skill_dir(grandchild):
                    candidates.add(grandchild.resolve())
    return candidates


def discover_skill_inventory(skills_root: str | Path) -> SkillInventory:
    """Discover formal Skills using the closed domain-first layout grammar."""

    root = Path(skills_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"skills root is not a directory: {root}")

    skill_dirs = _candidate_skill_dirs(root)
    manifest_dirs = {
        marker.parent.resolve()
        for name in ("skill.yaml", "SKILL.md")
        for marker in root.rglob(name)
        if not _is_internal(marker.parent.relative_to(root).parts)
        and not _is_nested_orchestrator(marker.parent.relative_to(root).parts)
    }
    unclaimed_manifest_dirs = {
        manifest_dir
        for manifest_dir in manifest_dirs
        if not any(
            manifest_dir == skill_dir or skill_dir in manifest_dir.parents
            for skill_dir in skill_dirs
        )
    }
    if unclaimed_manifest_dirs:
        missing = ", ".join(
            path.relative_to(root).as_posix()
            for path in sorted(
                unclaimed_manifest_dirs,
                key=lambda path: path.relative_to(root).as_posix(),
            )
        )
        raise ValueError(f"manifest inventory is incomplete: missing={missing}")
    locations: list[SkillLocation] = []
    identities: dict[str, Path] = {}
    for skill_dir in sorted(
        skill_dirs,
        key=lambda path: path.relative_to(root).as_posix(),
    ):
        relative = skill_dir.relative_to(root)
        parts = relative.parts
        if not parts or _is_internal(parts):
            continue
        if len(parts) == 3 and parts[1] == RUN_DERIVED_DIRNAME:
            collection = RUN_DERIVED_COLLECTION
            domain = parts[0]
        elif len(parts) == 2 and parts[0] == USER_SKILLS_DIRNAME:
            collection = USER_INSTALLED_COLLECTION
            domain = ""
        elif len(parts) in {1, 2, 3}:
            collection = CURATED_COLLECTION
            domain = parts[0]
        else:
            raise ValueError(
                "unsupported Skill layout below canonical root: "
                f"{relative.as_posix()}"
            )
        canonical_id, manifest_domain = _manifest_identity(skill_dir)
        if collection == USER_INSTALLED_COLLECTION:
            domain = manifest_domain
        if (
            collection == RUN_DERIVED_COLLECTION
            and manifest_domain
            and manifest_domain != domain
        ):
            raise ValueError(
                f"Skill path domain '{domain}' does not match manifest domain "
                f"'{manifest_domain}': {relative.as_posix()}"
            )
        existing = identities.get(canonical_id)
        if existing is not None:
            raise ValueError(
                f"duplicate registry identity '{canonical_id}': duplicate Skill "
                f"identity '{canonical_id}' at "
                f"{existing.relative_to(root).as_posix()} and "
                f"{relative.as_posix()}"
            )
        identities[canonical_id] = skill_dir
        locations.append(
            SkillLocation(
                skills_root=root,
                skill_dir=skill_dir,
                relative_path=relative,
                domain=domain,
                collection=collection,
                canonical_id=canonical_id,
                enabled=_is_enabled(skill_dir),
            )
        )
    return SkillInventory(skills_root=root, locations=tuple(locations))


__all__ = [
    "CURATED_COLLECTION",
    "RUN_DERIVED_COLLECTION",
    "RUN_DERIVED_DIRNAME",
    "SkillInventory",
    "SkillLocation",
    "USER_INSTALLED_COLLECTION",
    "discover_skill_inventory",
]
