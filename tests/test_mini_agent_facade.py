"""Unit tests for the ADR 0032 ``oc`` skill-handle facade.

Uses a fake ``run_skill`` (no real skill subprocess) but a real AnnData round
trip, so the materialise -> run -> reload -> provenance orchestration is
exercised end to end and fast.
"""

from __future__ import annotations

import json
from pathlib import Path

import anndata
import numpy as np
import pytest

from omicsclaw.autonomous.skill_facade import (
    SkillBudgetError,
    build_facade,
)


def _tiny_adata():
    return anndata.AnnData(X=np.ones((5, 3), dtype="float32"))


def _fake_run_skill_factory(captured: dict):
    """Return a fake run_skill that mimics a skill writing processed.h5ad."""

    class _Result:
        def __init__(self, output_dir, method):
            self.success = True
            self.output_dir = str(output_dir)
            self.method = method
            self.stdout = "ok"
            self.stderr = ""
            self.exit_code = 0

    def _fake(skill, *, input_path, output_dir, extra_args, cancel_event=None):
        captured["skill"] = skill
        captured["input_path"] = input_path
        captured["extra_args"] = list(extra_args)
        out = Path(output_dir)
        (out / "tables").mkdir(parents=True, exist_ok=True)
        (out / "tables" / "summary.csv").write_text("a,b\n1,2\n")
        adata = anndata.read_h5ad(input_path)
        adata.obs["processed"] = 1
        adata.write_h5ad(out / "processed.h5ad")
        method = None
        if "--method" in extra_args:
            method = extra_args[extra_args.index("--method") + 1]
        return _Result(out, method)

    return _fake


def test_facade_materialise_run_reload(tmp_path: Path):
    captured: dict = {}
    facade = build_facade(tmp_path, run_skill=_fake_run_skill_factory(captured))

    res = facade.run("spatial-preprocess", _tiny_adata(), method="scanpy", min_genes=200)

    assert res.success is True
    assert bool(res) is True
    assert res.adata is not None
    assert "processed" in res.adata.obs  # reloaded the skill's output
    assert res.primary_artifact.endswith("processed.h5ad")
    assert "summary.csv" in res.tables

    # flags were derived from method + params (underscores -> hyphens).
    assert "--method" in captured["extra_args"]
    assert captured["extra_args"][captured["extra_args"].index("--method") + 1] == "scanpy"
    assert "--min-genes" in captured["extra_args"]
    assert Path(captured["input_path"]).name == "input.h5ad"


def test_facade_writes_ordered_provenance(tmp_path: Path):
    facade = build_facade(tmp_path, run_skill=_fake_run_skill_factory({}))
    facade.run("spatial-preprocess", _tiny_adata(), method="scanpy")
    facade.run("sc-cluster", _tiny_adata(), method="leiden")

    log = tmp_path / "skill_calls.jsonl"
    assert log.exists()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["skill"] for r in records] == ["spatial-preprocess", "sc-cluster"]
    assert [r["index"] for r in records] == [1, 2]
    assert all(r["status"] == "succeeded" for r in records)


def test_facade_sugar_maps_method_name(tmp_path: Path):
    captured: dict = {}
    facade = build_facade(tmp_path, run_skill=_fake_run_skill_factory(captured))
    facade.spatial_preprocess(_tiny_adata(), method="scanpy")
    assert captured["skill"] == "spatial-preprocess"


def test_facade_enforces_skill_call_budget(tmp_path: Path):
    facade = build_facade(tmp_path, max_skill_calls=1, run_skill=_fake_run_skill_factory({}))
    facade.run("spatial-preprocess", _tiny_adata())
    with pytest.raises(SkillBudgetError):
        facade.run("sc-cluster", _tiny_adata())


def test_facade_zero_skill_call_budget_blocks_first_call(tmp_path: Path):
    facade = build_facade(tmp_path, max_skill_calls=0, run_skill=_fake_run_skill_factory({}))
    with pytest.raises(SkillBudgetError):
        facade.run("spatial-preprocess", _tiny_adata())


def test_facade_requires_data_or_path(tmp_path: Path):
    facade = build_facade(tmp_path, run_skill=_fake_run_skill_factory({}))
    with pytest.raises(ValueError):
        facade.run("spatial-preprocess")
