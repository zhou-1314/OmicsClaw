#!/usr/bin/env python3
"""Regenerate the v2 narrative ``SKILL.md`` from ``skill.yaml`` (ADR 0037).

Only v2 skills (those with a ``skill.yaml``) are processed; v1 skills are
skipped (their SKILL.md is still hand-authored under the legacy contract). The
generated header + ``## Inputs & Outputs`` summary come one-way from
``skill.yaml``; the narrative body is preserved.

Usage:
    python scripts/generate_skill_md.py <skill_dir>          # write
    python scripts/generate_skill_md.py --all                # all v2 skills
    python scripts/generate_skill_md.py <skill_dir> --check  # CI: diff-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.registry import SKILLS_DIR  # noqa: E402
from omicsclaw.skill.skill_md import render_skill_md  # noqa: E402


def render_for_skill(skill_dir: Path) -> str | None:
    """Return the regenerated SKILL.md for a v2 skill, or None if not v2.

    Raises ``ValueError`` if a present ``skill.yaml`` fails schema validation
    (a bad manifest must fail loud, never silently skipped).
    """
    skill_yaml = skill_dir / "skill.yaml"
    if not skill_yaml.exists():
        return None  # v1 skill — SKILL.md still hand-authored

    from omicsclaw.skill.schema import load_skill_yaml, validate_skill_yaml

    errors = validate_skill_yaml(skill_yaml)
    if errors:
        raise ValueError("; ".join(errors))
    manifest = load_skill_yaml(skill_yaml)

    skill_md = skill_dir / "SKILL.md"
    existing = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    return render_skill_md(manifest, existing)


def write_or_check(skill_dir: Path, *, check: bool) -> int:
    try:
        rendered = render_for_skill(skill_dir)
    except ValueError as exc:
        print(f"FAIL {skill_dir}: invalid skill.yaml ({exc})")
        return 1
    if rendered is None:
        print(f"skip: {skill_dir} has no skill.yaml (v1)")
        return 0

    target = skill_dir / "SKILL.md"
    if check:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if existing != rendered:
            print(f"FAIL {skill_dir}: SKILL.md is stale")
            return 1
        print(f"ok   {skill_dir}")
        return 0

    target.write_text(rendered, encoding="utf-8")
    try:
        display = target.resolve().relative_to(_ROOT)
    except ValueError:
        display = target
    print(f"wrote {display}")
    return 0


def discover_v2_skills(skills_root: Path) -> list[Path]:
    """Return every directory under ``skills_root`` that has a ``skill.yaml``."""
    return sorted(p.parent for p in skills_root.rglob("skill.yaml"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("skill_dir", nargs="?", type=Path, help="Skill directory")
    parser.add_argument("--all", action="store_true", help="Process every v2 skill under skills/")
    parser.add_argument("--check", action="store_true", help="Diff-only mode for CI")
    args = parser.parse_args()

    if args.all == bool(args.skill_dir):
        parser.error("provide either <skill_dir> or --all (not both, not neither)")

    targets = discover_v2_skills(SKILLS_DIR) if args.all else [args.skill_dir]

    failures = 0
    for skill_dir in targets:
        failures += write_or_check(skill_dir, check=args.check)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
