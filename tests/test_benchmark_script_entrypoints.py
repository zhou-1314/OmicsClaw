from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script_name",
    (
        "analyze_benchmark_campaign.py",
        "run_three_suite_skill_lifecycle_benchmark.py",
    ),
)
def test_benchmark_script_direct_help_from_repository_root(script_name: str) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(repository_root / "scripts" / script_name), "--help"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
