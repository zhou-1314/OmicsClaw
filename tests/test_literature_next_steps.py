"""Literature report "Next Steps" routes ALL 6 omics domains (audit E-(1) medium).

The report used to hardcode only spatial + single-cell entry skills, so a bulk
RNA-seq / genomics / proteomics / metabolomics paper got the wrong routing. Now
the detected technology maps to its domain's entry skill.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "literature"))

from core.extractor import extract_technology, infer_domain  # noqa: E402


# ---- technology detection across domains ----


def test_extract_technology_keeps_single_cell_precedence():
    # A single-cell platform mention must NOT be misread as bulk.
    assert extract_technology("We used 10x Genomics Chromium for scRNA-seq") == "10x Genomics"
    assert extract_technology("single-cell RNA-seq of tumor cells") == "single-cell RNA-seq"


def test_extract_technology_detects_other_domains():
    assert extract_technology("bulk RNA-seq of 12 samples") == "Bulk RNA-seq"
    assert extract_technology("whole-genome sequencing (WGS) variant calling") == "WGS/WES"
    assert extract_technology("ATAC-seq chromatin accessibility") == "ATAC-seq"
    assert extract_technology("TMT-based mass spectrometry proteomics") == "Mass spectrometry"
    assert extract_technology("untargeted metabolome profiling by LC-MS") == "Metabolomics"


def test_extract_technology_unknown_stays_unknown():
    assert extract_technology("a paper with no recognizable assay") == "unknown"


def test_extract_technology_lcms_metabolomics_not_proteomics():
    # codex must-fix #1: LC-MS/MS with a metabolomics anchor must NOT be proteomics.
    assert extract_technology("LC-MS/MS-based metabolomics of serum") == "Metabolomics"
    assert extract_technology("untargeted lipidomics by LC-MS") == "Metabolomics"
    # proteomics anchors still detected
    assert extract_technology("TMT-labeled shotgun proteomics") == "Mass spectrometry"
    assert extract_technology("peptide identification by tandem mass spectrometry") == "Mass spectrometry"


def test_extract_technology_short_aliases_are_word_bounded():
    # codex must-fix #2: short aliases must not substring-match ordinary words.
    assert extract_technology("western blot validation of protein expression") == "unknown"
    assert extract_technology("we showed that the gene is expressed") == "unknown"
    # real WGS/WES mentions still detected
    assert extract_technology("WES of 50 tumors") == "WGS/WES"
    assert extract_technology("whole-genome sequencing cohort") == "WGS/WES"


# ---- domain inference ----


def test_infer_domain_maps_each_family():
    assert infer_domain("Visium") == "spatial"
    assert infer_domain("10x Genomics") == "singlecell"
    assert infer_domain("single-cell RNA-seq") == "singlecell"
    assert infer_domain("Bulk RNA-seq") == "bulkrna"
    assert infer_domain("WGS/WES") == "genomics"
    assert infer_domain("ATAC-seq") == "genomics"
    assert infer_domain("Mass spectrometry") == "proteomics"
    assert infer_domain("Metabolomics") == "metabolomics"
    assert infer_domain("unknown") is None


# ---- the report emits a domain-aware entry skill ----


def _gen_report(tmp_path: Path, technology: str) -> str:
    from literature_parse import generate_report  # noqa: E402

    metadata = {
        "organism": "human",
        "tissue": "brain",
        "technology": technology,
        "geo_accessions": {"gse": [], "gsm": []},
    }
    generate_report(tmp_path, metadata, [], no_download=True)
    return (tmp_path / "report.md").read_text(encoding="utf-8")


def test_report_next_steps_routes_proteomics(tmp_path):
    assert "proteomics-identification" in _gen_report(tmp_path, "Mass spectrometry")


def test_report_next_steps_routes_bulkrna(tmp_path):
    assert "bulkrna-qc" in _gen_report(tmp_path, "Bulk RNA-seq")


def test_report_next_steps_routes_spatial(tmp_path):
    assert "spatial-preprocess" in _gen_report(tmp_path, "Visium")


def test_report_next_steps_unknown_lists_general_options(tmp_path):
    # Unknown technology → keep the general starting points (don't mis-route).
    report = _gen_report(tmp_path, "unknown")
    assert "spatial-preprocess" in report and "sc-preprocessing" in report
