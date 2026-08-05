"""Tests for the spatial-cnv skill."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_SCRIPT = Path(__file__).resolve().parent.parent / "spatial_cnv.py"


@pytest.fixture
def tmp_output(tmp_path):
    return tmp_path / "cnv_out"


@pytest.fixture(scope="module")
def demo_output(tmp_path_factory):
    """Run inferCNVpy once when its optional backend is available."""

    pytest.importorskip(
        "infercnvpy",
        reason="spatial-cnv Demo integration requires optional infercnvpy",
    )
    output_dir = tmp_path_factory.mktemp("spatial_cnv_demo")
    result = subprocess.run(
        [sys.executable, str(SKILL_SCRIPT), "--demo", "--output", str(output_dir)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(SKILL_SCRIPT.parent),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    return output_dir


def test_demo_mode(demo_output):
    """spatial-cnv --demo should run without error."""
    assert (demo_output / "report.md").exists()
    assert (demo_output / "result.json").exists()
    assert (demo_output / "processed.h5ad").exists()
    assert (demo_output / "tables" / "cnv_scores.csv").exists()
    assert (demo_output / "tables" / "cnv_run_summary.csv").exists()
    assert (demo_output / "reproducibility" / "commands.sh").exists()


def test_demo_outputs_gallery_contract(demo_output):
    """Demo mode should export gallery manifests, figure data, and the R helper."""
    assert (demo_output / "figures" / "manifest.json").exists()
    assert (demo_output / "figure_data" / "manifest.json").exists()
    assert (demo_output / "figure_data" / "cnv_scores.csv").exists()
    assert (demo_output / "figure_data" / "cnv_run_summary.csv").exists()
    assert (demo_output / "figure_data" / "cnv_spatial_points.csv").exists()
    assert (demo_output / "figure_data" / "cnv_umap_points.csv").exists()
    assert (demo_output / "figure_data" / "cnv_bin_summary.csv").exists()
    assert (demo_output / "tables" / "cnv_bin_summary.csv").exists()
    assert (demo_output / "reproducibility" / "r_visualization.sh").exists()


def test_demo_gallery_manifests_have_roles(demo_output):
    """The standard CNV gallery should emit figure and figure-data manifests."""
    figures_manifest = json.loads(
        (demo_output / "figures" / "manifest.json").read_text()
    )
    figure_data_manifest = json.loads(
        (demo_output / "figure_data" / "manifest.json").read_text()
    )

    assert figures_manifest["recipe_id"] == "standard-spatial-cnv-gallery"
    assert any(plot["role"] == "overview" for plot in figures_manifest["plots"])
    assert any(plot["role"] == "diagnostic" for plot in figures_manifest["plots"])
    assert any(plot["role"] == "supporting" for plot in figures_manifest["plots"])
    assert any(plot["role"] == "uncertainty" for plot in figures_manifest["plots"])
    assert figure_data_manifest["skill"] == "spatial-cnv"
    assert figure_data_manifest["recipe_id"] == "standard-spatial-cnv-gallery"


def test_demo_report_content(demo_output):
    """Report should contain expected sections."""
    report = (demo_output / "report.md").read_text()
    assert "CNV" in report
    assert "Visualization Outputs" in report
    assert "Disclaimer" in report


def test_demo_result_json(demo_output):
    """result.json should contain expected keys."""
    data = json.loads((demo_output / "result.json").read_text())
    assert data["skill"] == "spatial-cnv"
    assert "summary" in data
    assert data["summary"]["n_cells"] > 0


def test_demo_accepts_infercnv_flags(tmp_output):
    """CLI should accept the method-specific inferCNVpy flags exposed in SKILL.md."""
    pytest.importorskip(
        "infercnvpy",
        reason="spatial-cnv Demo integration requires optional infercnvpy",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL_SCRIPT),
            "--demo",
            "--infercnv-lfc-clip",
            "2.5",
            "--infercnv-dynamic-threshold",
            "1.2",
            "--infercnv-exclude-chromosomes",
            "chrX",
            "chrY",
            "--infercnv-chunksize",
            "2000",
            "--infercnv-n-jobs",
            "1",
            "--output",
            str(tmp_output),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(SKILL_SCRIPT.parent),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
