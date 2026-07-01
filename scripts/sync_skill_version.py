#!/usr/bin/env python3
"""Sync each v2 skill script's ``SKILL_VERSION`` to its ``skill.yaml.version`` (ADR 0037).

Only v2 skills (with a ``skill.yaml``) are processed; v1 skills are skipped.
A script with no ``SKILL_VERSION`` constant (e.g. a consensus shim) is a no-op.

Usage:
    python scripts/sync_skill_version.py <skill_dir>          # write
    python scripts/sync_skill_version.py --all                # all v2 skills
    python scripts/sync_skill_version.py <skill_dir> --check  # CI: diff-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.registry import SKILLS_DIR  # noqa: E402
from omicsclaw.skill.skill_version import read_script_version, sync_script_version  # noqa: E402


def _entry_script(skill_dir: Path):
    """Return (script_path, target_version, is_leaf) for a v2 skill, or (None, None, False)."""
    skill_yaml = skill_dir / "skill.yaml"
    if not skill_yaml.exists():
        return None, None, False
    from omicsclaw.skill.schema import load_skill_yaml, validate_skill_yaml

    errors = validate_skill_yaml(skill_yaml)
    if errors:
        raise ValueError("; ".join(errors))
    manifest = load_skill_yaml(skill_yaml)
    script = skill_dir / manifest.runtime.entry
    # A consensus shim delegates to the shared consensus runtime and carries no
    # own SKILL_VERSION; every other type (leaf, and the reserved workflow) must.
    return (script if script.exists() else None), manifest.version, manifest.type != "consensus"


def write_or_check(skill_dir: Path, *, check: bool) -> int:
    try:
        script, target, is_leaf = _entry_script(skill_dir)
    except ValueError as exc:
        print(f"FAIL {skill_dir}: invalid skill.yaml ({exc})")
        return 1
    if script is None:
        return 0  # v1 skill or no entry script — nothing to sync

    text = script.read_text(encoding="utf-8")
    current = read_script_version(text)
    if current is None:
        # A leaf skill's entry MUST declare SKILL_VERSION (it is emitted into
        # result.json); only consensus shims may omit it.
        if is_leaf:
            print(f"FAIL {skill_dir}: leaf entry script declares no SKILL_VERSION "
                  f"(add SKILL_VERSION = \"{target}\")")
            return 1
        return 0

    new_text, changed = sync_script_version(text, target)
    if not changed:
        return 0
    if check:
        print(f"FAIL {skill_dir}: SKILL_VERSION {current!r} != skill.yaml.version {target!r}")
        return 1
    script.write_text(new_text, encoding="utf-8")
    print(f"synced {script.relative_to(_ROOT) if _ROOT in script.parents else script}: {current} -> {target}")
    return 0


def discover_v2_skills(skills_root: Path) -> list[Path]:
    return sorted(p.parent for p in skills_root.rglob("skill.yaml"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("skill_dir", nargs="?", type=Path)
    parser.add_argument("--all", action="store_true", help="Process every v2 skill under skills/")
    parser.add_argument("--check", action="store_true", help="Diff-only mode for CI")
    args = parser.parse_args()

    if args.all == bool(args.skill_dir):
        parser.error("provide either <skill_dir> or --all (not both, not neither)")

    targets = discover_v2_skills(SKILLS_DIR) if args.all else [args.skill_dir]
    failures = sum(write_or_check(sd, check=args.check) for sd in targets)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
