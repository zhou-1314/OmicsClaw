"""DLPFC hero benchmark CI hook.

The full network-attached benchmark is gated. We always run an offline
``--dry-run`` smoke that asserts the script parses and reads
``expected_metrics.json`` cleanly. The real network run is exercised when
``RUN_DLPFC_BENCHMARK=1`` (set in PR CI for the consensus-runtime job).
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
    data = json.loads(_EXPECTED.read_text())
    assert "consensus_ari_noise_floor" in data
    assert "consensus_ari_min_absolute" in data
    assert "members_required" in data
    assert "operator" in data
    assert set(data["members_required"]) >= {"banksy", "graphst", "sedr", "leiden", "spagcn"}
    assert 0 < float(data["consensus_ari_noise_floor"]) < 0.2


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
