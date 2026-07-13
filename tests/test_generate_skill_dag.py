from __future__ import annotations

import json

from scripts import generate_skill_dag


def test_generated_skill_dag_is_deterministic_and_fresh():
    graph = generate_skill_dag.generate_skill_dag()
    expected = json.dumps(graph, indent=2)
    current = generate_skill_dag.OUTPUT_PATH.read_text(encoding="utf-8")

    assert graph["schema_version"] == 1
    assert graph["summary"]["node_count"] == 95
    assert graph["summary"]["edge_count"] > 0
    assert current.rstrip() == expected.rstrip(), (
        "skills/skill_dag.json is out of date — run "
        "`python scripts/generate_skill_dag.py --apply`."
    )
