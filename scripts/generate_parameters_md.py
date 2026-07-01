#!/usr/bin/env python3
"""Render `references/parameters.md` for a skill (dual-track, ADR 0037).

The generated markdown is the human-readable view of the runtime contract —
which CLI flags the skill accepts and which per-method parameter hints exist.
The parameter source is detected per skill:

- **v2** — `skill.yaml.interface.parameters` (preferred when `skill.yaml` is
  present); the SKILL.md frontmatter no longer carries this material.
- **v1** — the flat `parameters.yaml` sidecar (legacy).

A skill with both files renders from `skill.yaml` (v2 wins), matching the
dual-track rule everywhere else.

Usage:
    python scripts/generate_parameters_md.py <skill_dir>          # write
    python scripts/generate_parameters_md.py --all                # all skills
    python scripts/generate_parameters_md.py <skill_dir> --check  # CI: diff-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.parameters_md import (  # noqa: E402
    AUTOGEN_HEADER,
    render_parameters_md,
)
from omicsclaw.skill.registry import SKILLS_DIR  # noqa: E402

__all__ = ["AUTOGEN_HEADER", "render_parameters_md"]


def render_for_skill(skill_dir: Path) -> str | None:
    """Return the rendered markdown for `skill_dir`, or None if no parameters.

    Prefers the v2 `skill.yaml` contract; falls back to the v1
    `parameters.yaml` sidecar. Raises ``ValueError`` if a present `skill.yaml`
    fails schema validation (so a bad manifest fails loud, never silently
    rendered as v1).
    """
    skill_yaml = skill_dir / "skill.yaml"
    if skill_yaml.exists():
        # Lazy/protected import: the v1-only path stays pydantic-free.
        from omicsclaw.skill.schema import load_skill_yaml, validate_skill_yaml

        errors = validate_skill_yaml(skill_yaml)
        if errors:
            raise ValueError("; ".join(errors))
        manifest = load_skill_yaml(skill_yaml)
        params = manifest.interface.parameters.model_dump()
        return render_parameters_md(params, source="v2")

    sidecar_path = skill_dir / "parameters.yaml"
    if not sidecar_path.exists():
        return None
    sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or {}
    return render_parameters_md(sidecar)


def write_or_check(skill_dir: Path, *, check: bool) -> int:
    """Write `references/parameters.md` for `skill_dir`, or compare in --check.

    Returns shell exit code.
    """
    try:
        rendered = render_for_skill(skill_dir)
    except ValueError as exc:
        print(f"FAIL {skill_dir}: invalid skill.yaml ({exc})")
        return 1
    if rendered is None:
        print(f"skip: {skill_dir} has no skill.yaml or parameters.yaml")
        return 0

    target_dir = skill_dir / "references"
    target = target_dir / "parameters.md"

    if check:
        # Diff-only: never touch the tree (so --check is safe under CI/read-only).
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if existing != rendered:
            print(f"FAIL {skill_dir}: parameters.md is stale")
            return 1
        print(f"ok   {skill_dir}")
        return 0

    target_dir.mkdir(exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    try:
        display = target.resolve().relative_to(_ROOT)
    except ValueError:
        display = target
    print(f"wrote {display}")
    return 0


def discover_skill_dirs(skills_root: Path) -> list[Path]:
    """Return every directory under `skills_root` with renderable parameters.

    A directory qualifies if it carries a v2 `skill.yaml` or a v1
    `parameters.yaml` (deduplicated — a migrated skill may briefly have both).
    """
    dirs = {p.parent for p in skills_root.rglob("skill.yaml")}
    dirs |= {p.parent for p in skills_root.rglob("parameters.yaml")}
    return sorted(dirs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("skill_dir", nargs="?", type=Path, help="Skill directory")
    parser.add_argument("--all", action="store_true", help="Process every skill under skills/")
    parser.add_argument("--check", action="store_true", help="Diff-only mode for CI")
    args = parser.parse_args()

    if args.all == bool(args.skill_dir):
        parser.error("provide either <skill_dir> or --all (not both, not neither)")

    targets = discover_skill_dirs(SKILLS_DIR) if args.all else [args.skill_dir]

    failures = 0
    for skill_dir in targets:
        failures += write_or_check(skill_dir, check=args.check)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
