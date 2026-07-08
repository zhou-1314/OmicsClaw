#!/usr/bin/env python3
"""Rewrite every ``skills/**/skill.yaml`` in canonical (de-ceremonied) form.

Each manifest is loaded through the schema and re-serialised via
``SkillManifest.to_yaml()``, which omits fields equal to their default (the
governance/security/mcp blocks when unset, ``type: leaf``,
``runtime.language: python``, empty lists, …). The parsed model is unchanged —
``parse(new) == parse(old)`` — so only redundant default lines are dropped; no
signal is lost. This keeps the authored surface minimal and, run with
``--check``, guards against skill.yaml drifting away from canonical form (the
way ADR 0041's manual ``allowed_extra_flags`` pruning once did).

Usage:
  python scripts/canonicalize_skill_yaml.py            # rewrite in place
  python scripts/canonicalize_skill_yaml.py --check    # report non-canonical files; exit 1 if any
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omicsclaw.skill.schema import load_skill_yaml  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report drift, do not write; exit 1 if any")
    ap.add_argument("--root", default="skills", help="skills root (default: skills)")
    args = ap.parse_args()

    files = sorted(glob.glob(f"{args.root}/**/skill.yaml", recursive=True))
    if not files:
        print(f"no skill.yaml under {args.root}/", file=sys.stderr)
        return 1

    changed: list[str] = []
    for p in files:
        path = Path(p)
        current = path.read_text(encoding="utf-8")
        canonical = load_skill_yaml(path).to_yaml()
        if current != canonical:
            changed.append(p)
            if not args.check:
                path.write_text(canonical, encoding="utf-8")

    if args.check:
        if changed:
            print(f"{len(changed)}/{len(files)} skill.yaml NOT canonical:")
            for p in changed:
                print("  ", p)
            print("\nRun: python scripts/canonicalize_skill_yaml.py", file=sys.stderr)
            return 1
        print(f"all {len(files)} skill.yaml canonical")
        return 0

    print(f"canonicalized {len(changed)}/{len(files)} skill.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
