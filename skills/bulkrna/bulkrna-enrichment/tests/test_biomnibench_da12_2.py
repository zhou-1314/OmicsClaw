"""Contract tests for the BiomniBench-DA 12-2 deterministic preflight."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from omicsclaw.skill.schema import load_skill_yaml


SKILL_DIR = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = SKILL_DIR.parents[2]
DATASET_DIR = REPOSITORY_ROOT / "data/benchmarks/biomnibench-da/da-12-2"


def _load_adapter():
    adapter_path = SKILL_DIR / "tests/biomnibench_da12_2.py"
    spec = importlib.util.spec_from_file_location("biomnibench_da12_2_test", adapter_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_biomnibench_preflight_protocol_is_content_bound() -> None:
    manifest = load_skill_yaml(SKILL_DIR / "skill.yaml")
    protocol = next(
        item
        for item in manifest.validation.protocols
        if item.id == "biomnibench-da12-2-deterministic-preflight-v1"
    )

    assert protocol.kind == "fixture"
    assert protocol.runner == "command"
    assert protocol.suite_id == "biomnibench-da"
    assert protocol.case_ids == ["da-12-2"]
    assert protocol.dataset_ref is not None
    assert protocol.dataset_ref.path == "data/benchmarks/biomnibench-da/da-12-2"
    assert protocol.dataset_ref.content_sha256 == (
        "sha256:00c42c92536a615b7127e69c4691299e890a23264e503255cd5ba4081248e3e9"
    )


def test_biomnibench_preflight_extracts_the_declared_shared_deg_set() -> None:
    adapter = _load_adapter()
    table, genes = adapter._extract_shared_genes(
        DATASET_DIR / "environment/data/TS7.xlsx"
    )

    assert table.shape == (5314, 7)
    assert len(genes) == 1543
    assert len(genes) == len(set(genes))


def test_biomnibench_preflight_uses_the_supplied_hallmark_definitions() -> None:
    adapter = _load_adapter()
    gene_sets = adapter._read_gmt(DATASET_DIR / "environment/data/GSEA_gmt.gmt")

    assert len(gene_sets) == 50
    assert len(gene_sets["HALLMARK_G2M_CHECKPOINT"]) == 200
    assert len(gene_sets["HALLMARK_G2M_CHECKPOINT"]) == len(
        set(gene_sets["HALLMARK_G2M_CHECKPOINT"])
    )


def test_biomnibench_trace_discloses_background_and_membership_sentinels(
    tmp_path,
) -> None:
    adapter = _load_adapter()
    g2m = pd.Series(
        {
            "term": "HALLMARK_G2M_CHECKPOINT",
            "overlap": "37/200",
            "pvalue": 1.689556e-5,
            "padj": 2.759608e-4,
        }
    )
    top = pd.DataFrame([g2m])
    trace_path = tmp_path / "trace.md"

    adapter._write_trace(
        trace_path,
        source_shape=(5314, 7),
        query_size=1543,
        background_size=4384,
        query_in_background=400,
        pathway_count=50,
        tested_count=49,
        g2m=g2m,
        g2m_rank=3,
        top=top,
    )

    trace = trace_path.read_text(encoding="utf-8")
    assert "# Objective" in trace
    assert "4384" in trace and "400" in trace
    assert "background=background" in trace
    assert "membership sentinels" in trace
    assert "not TS7 effect sizes or p-values" in trace
    assert "# Disclaimer" in trace
