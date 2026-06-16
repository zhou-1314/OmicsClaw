#!/usr/bin/env python3
"""Generate skills/catalog.json from SKILL.md YAML frontmatter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

OMICSCLAW_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = OMICSCLAW_DIR / "skills"


def parse_yaml_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md file via ``yaml.safe_load``.

    Uses the same parser as ``omicsclaw.skill.lazy_metadata`` so the catalog
    cannot diverge from the runtime registry on nested structures, folded
    scalars, or list/dict shapes.
    """
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


_SKILL_TYPES = ("leaf", "workflow", "knowledge", "adapter")
_VALIDATION_LEVELS = (
    "smoke-only", "demo-validated", "fixture-validated", "benchmarked", "production",
)


def _sidecar_enum(skill_dir: Path, key: str, allowed: tuple[str, ...], default: str) -> str:
    """Read an optional enum field from parameters.yaml, clamped to ``allowed``.

    A missing/blank/unknown value falls back to ``default`` so the catalog matches
    ``LazySkillMetadata`` exactly (ADR 0030).
    """
    sidecar = skill_dir / "parameters.yaml"
    if not sidecar.exists():
        return default
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return default
    value = (data or {}).get(key) if isinstance(data, dict) else None
    return value if value in allowed else default


def sidecar_list(skill_dir: Path, key: str) -> list | None:
    """Read an optional list field from parameters.yaml (the v2 sidecar).

    Returns ``None`` when the sidecar is absent/unreadable or the key is unset, so
    callers can fall back to legacy frontmatter — mirroring how
    ``LazySkillMetadata._load_basic`` lets the sidecar win per-field and the
    frontmatter fill the gaps (ADR 0030).
    """
    sidecar = skill_dir / "parameters.yaml"
    if not sidecar.exists():
        return None
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    value = (data or {}).get(key) if isinstance(data, dict) else None
    if isinstance(value, str):
        return [value]
    return value if isinstance(value, list) else None


def sidecar_type(skill_dir: Path) -> str:
    """Declared skill `type` (ADR 0030); ``leaf`` when unset/unknown."""
    return _sidecar_enum(skill_dir, "type", _SKILL_TYPES, "leaf")


def sidecar_validation_level(skill_dir: Path) -> str:
    """Validation maturity (ADR 0030); ``smoke-only`` when unset/unknown."""
    return _sidecar_enum(skill_dir, "validation_level", _VALIDATION_LEVELS, "smoke-only")


def build_cli_alias_map() -> dict[str, str]:
    """Build {absolute_skill_dir_path: canonical_cli_alias} from the Omics registry."""
    if str(OMICSCLAW_DIR) not in sys.path:
        sys.path.insert(0, str(OMICSCLAW_DIR))
    from omicsclaw.skill.registry import registry

    registry.load_all()
    alias_map: dict[str, str] = {}
    for _alias, info in registry.skills.items():
        script = info.get("script")
        if isinstance(script, Path):
            canonical = info.get("canonical_name") or info.get("alias") or _alias
            alias_map[str(script.parent.resolve())] = str(canonical)
    return alias_map


def generate_catalog() -> dict:
    """Scan skills/ and build the catalog."""
    alias_map = build_cli_alias_map()
    skills = []
    for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        # Use the path RELATIVE to SKILLS_DIR for the hidden/dunder filter,
        # otherwise a worktree checked out at `.worktrees/<branch>` triggers
        # the `.startswith(".")` rule on its own parent directory and the
        # generator silently emits an empty catalog.
        rel_parts = skill_dir.relative_to(SKILLS_DIR).parts
        if any(part.startswith((".", "__")) for part in rel_parts):
            continue

        fm = parse_yaml_frontmatter(skill_md.read_text())
        name = fm.get("name", skill_dir.name)

        has_script = any(skill_dir.glob("*.py"))
        has_tests = (skill_dir / "tests").exists() and any((skill_dir / "tests").glob("test_*.py"))
        skill_type = sidecar_type(skill_dir)
        # Workflow shims forward to the shared `runtime/consensus/run` parser,
        # which has no `--demo` (consensus runs on real preprocessed multi-sample
        # data).  Advertising `oc run <shim> --demo` would point at a command that
        # exits with an argparse error, so they declare no demo.
        has_demo = has_script and skill_type != "workflow"

        # Sidecar wins, legacy frontmatter fills — same precedence as the runtime
        # registry (LazySkillMetadata). v2 skills declare trigger_keywords at the
        # top level of parameters.yaml; v1 skills carry them under
        # metadata.omicsclaw in SKILL.md frontmatter. (codex review [P2])
        trigger_kw = sidecar_list(skill_dir, "trigger_keywords")
        if trigger_kw is None:
            trigger_kw = []
            metadata = fm.get("metadata", {})
            if isinstance(metadata, dict):
                sc_meta = metadata.get("omicsclaw", {}) or metadata.get("spatialclaw", {})
                if isinstance(sc_meta, dict):
                    trigger_kw = sc_meta.get("trigger_keywords", [])
            if isinstance(trigger_kw, str):
                trigger_kw = [trigger_kw]

        cli_alias = alias_map.get(str(skill_dir.resolve()))
        entry = {
            "name": name,
            "cli_alias": cli_alias,
            "type": skill_type,
            "description": fm.get("description", ""),
            "version": fm.get("version", "0.1.0"),
            "status": "mvp" if has_script else "planned",
            "validation_level": sidecar_validation_level(skill_dir),
            "has_script": has_script,
            "has_tests": has_tests,
            "has_demo": has_demo,
            "demo_command": (
                f"python omicsclaw.py run {cli_alias or skill_dir.name} --demo"
                if has_demo else None
            ),
            "tags": fm.get("tags", []),
            "trigger_keywords": trigger_kw if isinstance(trigger_kw, list) else [],
        }
        skills.append(entry)

    catalog = {
        "version": "1.0.0",
        "generated_by": "scripts/generate_catalog.py",
        "skill_count": len(skills),
        "skills": skills,
    }
    return catalog


def main():
    parser = argparse.ArgumentParser(description="Generate skills/catalog.json from SKILL.md frontmatter")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="Write catalog.json (default behavior)")
    group.add_argument("--check", action="store_true", help="Exit 1 if catalog.json is out of date")
    args = parser.parse_args()

    catalog = generate_catalog()
    out_path = SKILLS_DIR / "catalog.json"
    expected = json.dumps(catalog, indent=2)

    if args.check:
        current = out_path.read_text() if out_path.exists() else ""
        if current.rstrip() != expected.rstrip():
            print(
                "ERROR: skills/catalog.json is out of date.\n"
                "       Run: python scripts/generate_catalog.py --apply",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"skills/catalog.json is up to date ({catalog['skill_count']} skills).")
        return

    out_path.write_text(expected)
    print(f"Generated {out_path} with {catalog['skill_count']} skills")


if __name__ == "__main__":
    main()
