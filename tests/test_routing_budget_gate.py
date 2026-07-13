from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_routing_budget_gate_uses_committed_ceiling_and_passes() -> None:
    ceiling = ROOT / "tests" / "fixtures" / "routing_budget" / "ceiling.json"
    assert ceiling.exists()

    result = subprocess.run(
        [sys.executable, "scripts/check_routing_budget.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/fixtures/routing_budget/ceiling.json" in result.stdout
