#!/usr/bin/env python3
"""Generate the auditable producer-consumer compatibility graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
OUTPUT_PATH = SKILLS_DIR / "skill_dag.json"


def generate_skill_dag(skills_dir: Path | None = None) -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from omicsclaw.skill.registry import OmicsRegistry
    from omicsclaw.skill.skill_dag import build_skill_dag, load_skill_dag_reviews

    registry = OmicsRegistry()
    effective_skills_dir = skills_dir or SKILLS_DIR
    registry.load_all(effective_skills_dir)
    return build_skill_dag(
        registry,
        reviews=load_skill_dag_reviews(effective_skills_dir / "skill_dag_reviews.yaml"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate skills/skill_dag.json")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="Write skill_dag.json (default)")
    group.add_argument("--check", action="store_true", help="Exit 1 when the artifact is stale")
    args = parser.parse_args()

    graph = generate_skill_dag()
    expected = json.dumps(graph, indent=2)
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current.rstrip() != expected.rstrip():
            print(
                "ERROR: skills/skill_dag.json is out of date.\n"
                "       Run: python scripts/generate_skill_dag.py --apply",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(
            "skills/skill_dag.json is up to date "
            f"({graph['summary']['node_count']} nodes, {graph['summary']['edge_count']} edges)."
        )
        return

    OUTPUT_PATH.write_text(expected + "\n", encoding="utf-8")
    print(
        f"Generated {OUTPUT_PATH} with {graph['summary']['node_count']} nodes and "
        f"{graph['summary']['edge_count']} edges"
    )


if __name__ == "__main__":
    main()
