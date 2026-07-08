#!/usr/bin/env python3
"""Generate skills/catalog.json from the dual-track skill metadata reader.

Metadata is sourced through ``omicsclaw.skill.lazy_metadata.LazySkillMetadata``,
which prefers a v2 ``skill.yaml`` (ADR 0037) and falls back to v1 (SKILL.md
frontmatter + parameters.yaml). This keeps the catalog from diverging from the
runtime registry regardless of which contract a skill uses, and discovers
v2-only skills (skill.yaml without SKILL.md).
"""

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

    Retained as a tested utility; the catalog itself now reads metadata through
    LazySkillMetadata so it cannot diverge from the runtime registry.
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


def _iter_skill_dirs() -> list[Path]:
    """Skill dirs under SKILLS_DIR — v1 (SKILL.md) or v2 (skill.yaml) — sorted by path.

    Sorting by directory path matches the previous ``sorted(rglob('SKILL.md'))``
    order for v1 skills (identical suffix), so the catalog ordering is stable.
    """
    dirs: set[Path] = set()
    for marker in ("SKILL.md", "skill.yaml"):
        for path in SKILLS_DIR.rglob(marker):
            dirs.add(path.parent)
    result: list[Path] = []
    for skill_dir in sorted(dirs):
        rel_parts = skill_dir.relative_to(SKILLS_DIR).parts
        if any(part.startswith((".", "__")) for part in rel_parts):
            continue
        result.append(skill_dir)
    return result


def generate_catalog() -> dict:
    """Scan skills/ and build the catalog via the dual-track metadata reader."""
    if str(OMICSCLAW_DIR) not in sys.path:
        sys.path.insert(0, str(OMICSCLAW_DIR))
    from omicsclaw.skill.lazy_metadata import LazySkillMetadata

    alias_map = build_cli_alias_map()
    skills = []
    for skill_dir in _iter_skill_dirs():
        lazy = LazySkillMetadata(skill_dir)
        name = lazy.name or skill_dir.name

        has_script = any(skill_dir.glob("*.py"))
        has_tests = (skill_dir / "tests").exists() and any((skill_dir / "tests").glob("test_*.py"))
        skill_type = lazy.type
        # Consensus shims forward to the shared runtime/consensus/run parser, which
        # has no `--demo`; advertising `oc run <shim> --demo` would point at an
        # argparse error, so they declare no demo.
        has_demo = has_script and skill_type != "consensus"

        cli_alias = alias_map.get(str(skill_dir.resolve()))
        entry = {
            "name": name,
            "cli_alias": cli_alias,
            "type": skill_type,
            "description": lazy.description,
            "version": lazy.version or "0.1.0",
            "status": lazy.lifecycle_status,
            "origin": lazy.origin,
            "validation_level": lazy.validation_level,
            "has_script": has_script,
            "has_tests": has_tests,
            "has_demo": has_demo,
            "demo_command": (
                f"python omicsclaw.py run {cli_alias or skill_dir.name} --demo"
                if has_demo else None
            ),
            "tags": lazy.tags,
            "trigger_keywords": lazy.trigger_keywords or [],
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
    parser = argparse.ArgumentParser(description="Generate skills/catalog.json (dual-track v1/v2)")
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
