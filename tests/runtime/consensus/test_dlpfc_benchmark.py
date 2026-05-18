"""DLPFC hero benchmark tests.

The full network-attached benchmark is opt-in. We always run an offline
``--dry-run`` smoke that asserts the script parses and reads
``expected_metrics.json`` cleanly. The real network run is exercised when
``RUN_DLPFC_BENCHMARK=1`` is set in the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BENCH_DIR = Path(__file__).resolve().parents[3] / "examples" / "consensus_benchmark"
_RUNNER = _BENCH_DIR / "run_dlpfc_151673.py"
_EXPECTED = _BENCH_DIR / "expected_metrics.json"


def test_expected_metrics_json_is_well_formed() -> None:
    """ADR 0011 task-targeted panel schema:

    - ``hard_metrics``: list of entries, each with ``name``, ``rule``,
      ``noise_floor``, optional ``min_absolute``, optional ``applies_to``
      in ``{"all", "spatial_only"}``.
    - ``report_only_metrics``: list of metric names computed but not gated.
    - ``pass_rule``: currently always ``"all_hard_pass"``.
    """
    data = json.loads(_EXPECTED.read_text())
    assert "members_required" in data
    assert "operator" in data
    assert set(data["members_required"]) >= {"banksy", "graphst", "sedr", "leiden", "spagcn"}

    assert "hard_metrics" in data and isinstance(data["hard_metrics"], list)
    hard_names = {entry["name"] for entry in data["hard_metrics"]}
    # Task-targeted base panel; spatial path adds MLAMI.
    assert {"ARI", "AMI", "V_measure"}.issubset(hard_names)
    assert "MLAMI" in hard_names, "spatial path must include MLAMI as a hard metric"

    for entry in data["hard_metrics"]:
        assert entry["rule"] == "noise_floor"
        assert 0.0 < float(entry["noise_floor"]) < 0.2
        assert "applies_to" in entry and entry["applies_to"] in {"all", "spatial_only"}

    assert "report_only_metrics" in data and isinstance(data["report_only_metrics"], list)
    # H + C must be present for over-merge/over-split diagnosis.
    assert {"Homogeneity", "Completeness"}.issubset(set(data["report_only_metrics"]))

    assert data.get("pass_rule") == "all_hard_pass"


def test_dry_run_succeeds_without_network() -> None:
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    assert "[dry-run]" in proc.stdout
    assert "expected metrics" in proc.stdout


@pytest.mark.skipif(
    os.environ.get("RUN_DLPFC_BENCHMARK") != "1",
    reason="Set RUN_DLPFC_BENCHMARK=1 to run the network-attached DLPFC benchmark",
)
def test_dlpfc_151673_full_benchmark(tmp_path: Path) -> None:
    """Network-attached: fetch DLPFC 151673, run consensus, assert ARI floor."""
    proc = subprocess.run(
        [
            sys.executable,
            str(_RUNNER),
            "--output-dir",
            str(tmp_path / "bench"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"DLPFC benchmark failed:\n{proc.stderr}\n{proc.stdout}")
    summary_path = tmp_path / "bench" / "benchmark_result.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        assert summary["passed"], f"benchmark failures: {summary.get('failures')}"
