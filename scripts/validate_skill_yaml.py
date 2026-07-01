#!/usr/bin/env python3
"""Validate every ``skill.yaml`` (v2 contract, ADR 0037) under skills/.

CI dual-track gate: runs alongside the v1 checks (skill_lint / audit_skill_requires
/ generate_catalog). Exits 0 when all present skill.yaml validate (including the
case where none exist yet — v1-only tree); exits 1 on any schema error.

  python scripts/validate_skill_yaml.py            # validate all skills/**/skill.yaml
  python scripts/validate_skill_yaml.py --check     # same; explicit CI alias
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omicsclaw.skill.schema import validate_skill_yaml  # noqa: E402

SKILLS_ROOT = REPO_ROOT / "skills"


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate all v2 skill.yaml contracts (ADR 0037)")
    ap.add_argument("--check", action="store_true", help="CI alias (validate, nonzero on error)")
    ap.add_argument("--root", default=str(SKILLS_ROOT), help="skills root to scan")
    args = ap.parse_args()

    root = Path(args.root)
    files = sorted(root.rglob("skill.yaml"))
    if not files:
        print("no skill.yaml found (v1-only tree) — dual-track gate passes trivially")
        return 0

    n_ok = n_fail = 0
    for f in files:
        rel = f.relative_to(REPO_ROOT) if REPO_ROOT in f.parents else f
        errs = validate_skill_yaml(f)
        if errs:
            n_fail += 1
            print(f"❌ {rel}")
            for e in errs:
                print(f"     {e}")
        else:
            n_ok += 1
            print(f"✅ {rel}")

    print(f"\n{n_ok} valid, {n_fail} invalid of {len(files)} skill.yaml")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
