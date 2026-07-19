"""Tests for omicsclaw.autoagent.hard_gates."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from omicsclaw.autoagent.hard_gates import (
    gate_artifacts_present,
    gate_cell_retention,
    gate_fallback_recorded,
    gate_no_crash,
    gate_no_empty_output,
    run_hard_gates,
)
from omicsclaw.autoagent.trace import (
    DataShapeTrace,
    ExecutionTrace,
    MethodTrace,
    RunTrace,
    TraceCollector,
)


def _trial_authority(skill_name: str):
    from omicsclaw.autoagent.authority import TrialSkillAuthority

    primary = (
        None
        if skill_name.startswith("bulkrna")
        else "raw_counts.h5ad"
        if skill_name in {"spatial-raw-processing", "spatial-raw-fastq-processing"}
        else "processed.h5ad"
    )
    revision = "sha256:" + "c" * 64
    return TrialSkillAuthority(
        requested_skill_name=skill_name,
        canonical_skill_id=(
            "spatial-raw-processing"
            if skill_name == "spatial-raw-fastq-processing"
            else skill_name
        ),
        skill_version="1.0.0",
        manifest_hash=revision,
        source_hash=revision,
        primary_anndata_path=primary,
        skills_root="/test/skills",
    )


def _make_trace(**kwargs) -> RunTrace:
    """Build a RunTrace with overrides."""
    return RunTrace(
        trial_id=kwargs.get("trial_id", 0),
        skill_name=kwargs.get("skill_name", "sc-preprocessing"),
        method=kwargs.get("method", "scanpy"),
        authority=kwargs.get(
            "authority",
            _trial_authority(kwargs.get("skill_name", "sc-preprocessing")),
        ),
        execution=kwargs.get("execution", ExecutionTrace()),
        data_shape=kwargs.get("data_shape", DataShapeTrace()),
        method_trace=kwargs.get("method_trace", MethodTrace()),
    )


def _write_child_receipt(output_dir, trace: RunTrace) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    authority = trace.authority
    assert authority is not None
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "skill": authority.canonical_skill_id,
                "version": authority.skill_version,
                "completed_at": "2026-07-17T00:00:00+00:00",
                "input_checksum": "",
                "summary": {},
                "data": {},
                "status": "ok",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / OUTPUT_CLAIM_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claim_id": "f" * 32,
                "owner": f"skill:{authority.canonical_skill_id}",
                "claimed_at": "2026-07-17T00:00:00+00:00",
                "audit_identity": {
                    "skill_id": authority.canonical_skill_id,
                    "skill_version": authority.skill_version,
                    "skill_hash": authority.manifest_hash,
                    "source_hash": authority.source_hash,
                    "environment_id": "env:" + "f" * 20,
                },
                "runtime_source": "base",
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Individual gate tests
# ---------------------------------------------------------------------------


class TestGateNoCrash:
    def test_pass(self):
        trace = _make_trace(execution=ExecutionTrace(exit_code=0))
        result = gate_no_crash(trace)
        assert result.passed is True
        assert result.name == "no_crash"

    def test_fail(self):
        trace = _make_trace(execution=ExecutionTrace(exit_code=1))
        result = gate_no_crash(trace)
        assert result.passed is False
        assert "exit code 1" in result.message


class TestGateArtifactsPresent:
    def test_pass_default(self, tmp_path):
        (tmp_path / "result.json").write_text("{}")
        (tmp_path / "processed.h5ad").write_text("")
        trace = _make_trace(skill_name="sc-preprocessing")
        result = gate_artifacts_present(trace, tmp_path)
        assert result.passed is True

    def test_fail_missing_result_json(self, tmp_path):
        trace = _make_trace(skill_name="sc-preprocessing")
        result = gate_artifacts_present(trace, tmp_path)
        assert result.passed is False
        assert "result.json" in result.message

    def test_custom_required_files(self, tmp_path):
        (tmp_path / "output.csv").write_text("a,b\n1,2")
        trace = _make_trace(skill_name="bulkrna-de")
        result = gate_artifacts_present(
            trace, tmp_path, required_files=["output.csv"]
        )
        assert result.passed is True

    def test_non_adata_skill(self, tmp_path):
        """Registry Skills without saves_h5ad only need result.json."""
        (tmp_path / "result.json").write_text("{}")
        trace = _make_trace(skill_name="bulkrna-de")
        result = gate_artifacts_present(trace, tmp_path)
        assert result.passed is True

    @pytest.mark.parametrize(
        ("skill_name", "primary_name"),
        [
            ("spatial-deconv", "processed.h5ad"),
            ("spatial-integrate", "processed.h5ad"),
            ("spatial-raw-processing", "raw_counts.h5ad"),
        ],
    )
    def test_registry_declared_anndata_is_required(
        self,
        tmp_path,
        skill_name,
        primary_name,
    ):
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")

        result = gate_artifacts_present(
            _make_trace(skill_name=skill_name),
            tmp_path,
        )

        assert result.passed is False
        assert result.value == [primary_name]

    def test_claim_aliases_do_not_satisfy_artifact_gate(self, tmp_path):
        from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

        claim = tmp_path / OUTPUT_CLAIM_FILENAME
        claim.write_text("{}\n", encoding="utf-8")
        (tmp_path / "result.json").hardlink_to(claim)
        (tmp_path / "processed.h5ad").hardlink_to(claim)

        result = gate_artifacts_present(
            _make_trace(skill_name="sc-preprocessing"),
            tmp_path,
        )

        assert result.passed is False
        assert set(result.value) == {"result.json", "processed.h5ad"}

    def test_escaping_symlink_does_not_satisfy_artifact_gate(self, tmp_path):
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")
        external = tmp_path.parent / "external.h5ad"
        external.write_text("external", encoding="utf-8")
        (tmp_path / "processed.h5ad").symlink_to(external)

        result = gate_artifacts_present(
            _make_trace(skill_name="sc-preprocessing"),
            tmp_path,
        )

        assert result.passed is False
        assert result.value == ["processed.h5ad"]


class TestGateCellRetention:
    def test_pass_high_retention(self):
        trace = _make_trace(
            data_shape=DataShapeTrace(
                n_obs_before=5000, n_obs_after=4800, cell_retention_rate=0.96,
            )
        )
        result = gate_cell_retention(trace)
        assert result.passed is True
        assert "4800/5000" in result.message

    def test_fail_low_retention(self):
        trace = _make_trace(
            data_shape=DataShapeTrace(
                n_obs_before=5000, n_obs_after=100, cell_retention_rate=0.02,
            )
        )
        result = gate_cell_retention(trace)
        assert result.passed is False
        assert "collapsed" in result.message

    def test_skip_no_before_count(self):
        trace = _make_trace(
            data_shape=DataShapeTrace(n_obs_before=0, n_obs_after=500)
        )
        result = gate_cell_retention(trace)
        assert result.passed is True
        assert "Skipped" in result.message

    def test_custom_threshold(self):
        trace = _make_trace(
            data_shape=DataShapeTrace(
                n_obs_before=1000, n_obs_after=80, cell_retention_rate=0.08,
            )
        )
        # 8% > 5% default → pass
        result = gate_cell_retention(trace, min_retention=0.05)
        assert result.passed is True
        # 8% < 10% custom → fail
        result = gate_cell_retention(trace, min_retention=0.10)
        assert result.passed is False

    def test_legacy_sc_preprocessing_summary_does_not_skip(self, tmp_path):
        result_payload = {
            "skill": "sc-preprocessing",
            "summary": {
                "n_cells": 4250,
                "n_genes": 18000,
                "n_hvg": 2000,
                "n_cells_before_filter": 5000,
                "n_genes_before_filter": 20000,
                "cells_retained_pct": 85.0,
            },
            "data": {"effective_params": {"method": "scanpy"}},
        }
        (tmp_path / "result.json").write_text(json.dumps(result_payload))

        trace = TraceCollector.collect(
            trial_id=1,
            skill_name="sc-preprocessing",
            method="scanpy",
            execution=SimpleNamespace(
                exit_code=0,
                authority=_trial_authority("sc-preprocessing"),
            ),
            output_dir=tmp_path,
        )
        result = gate_cell_retention(trace)

        assert result.passed is True
        assert "Skipped" not in result.message
        assert "4250/5000" in result.message


class TestGateNoEmptyOutput:
    def test_pass(self):
        trace = _make_trace(
            data_shape=DataShapeTrace(n_obs_after=100, n_vars_after=50)
        )
        result = gate_no_empty_output(trace)
        assert result.passed is True

    def test_fail_zero_cells(self):
        trace = _make_trace(
            execution=ExecutionTrace(exit_code=1),
            data_shape=DataShapeTrace(n_obs_after=0, n_vars_after=50),
        )
        result = gate_no_empty_output(trace)
        assert result.passed is False
        assert "Empty" in result.message

    def test_skip_unknown_shape(self):
        trace = _make_trace(
            data_shape=DataShapeTrace(n_obs_after=0, n_vars_after=0)
        )
        # exit_code=0 but no shape info → skip (don't fail)
        result = gate_no_empty_output(trace)
        assert result.passed is True
        assert "Skipped" in result.message

    def test_fail_known_empty_anndata(self, tmp_path):
        import anndata as ad
        import numpy as np

        ad.AnnData(X=np.empty((0, 0))).write_h5ad(tmp_path / "processed.h5ad")
        trace = TraceCollector.collect(
            trial_id=1,
            skill_name="sc-preprocessing",
            method="scanpy",
            execution=SimpleNamespace(
                exit_code=0,
                authority=_trial_authority("sc-preprocessing"),
            ),
            output_dir=tmp_path,
        )

        result = gate_no_empty_output(trace)

        assert result.passed is False
        assert result.value == {"n_obs": 0, "n_vars": 0}

    def test_fail_known_empty_declared_primary_anndata(self, tmp_path):
        import anndata as ad
        import numpy as np

        ad.AnnData(X=np.empty((0, 0))).write_h5ad(tmp_path / "raw_counts.h5ad")
        trace = TraceCollector.collect(
            trial_id=1,
            skill_name="spatial-raw-processing",
            method="st-pipeline",
            execution=SimpleNamespace(
                exit_code=0,
                authority=_trial_authority("spatial-raw-processing"),
            ),
            output_dir=tmp_path,
        )

        result = gate_no_empty_output(trace)

        assert result.passed is False
        assert result.value == {"n_obs": 0, "n_vars": 0}


class TestGateFallbackRecorded:
    def test_pass_no_fallback(self):
        trace = _make_trace(method_trace=MethodTrace(fallback_used=False))
        result = gate_fallback_recorded(trace)
        assert result.passed is True

    def test_pass_with_reason(self):
        trace = _make_trace(
            method_trace=MethodTrace(
                requested_method="scanvi",
                executed_method="scvi",
                fallback_used=True,
                fallback_reason="no labels",
            )
        )
        result = gate_fallback_recorded(trace)
        assert result.passed is True
        assert "scanvi" in result.message

    def test_fail_silent_fallback(self):
        trace = _make_trace(
            method_trace=MethodTrace(
                requested_method="scanvi",
                executed_method="scvi",
                fallback_used=True,
                fallback_reason=None,
            )
        )
        result = gate_fallback_recorded(trace)
        assert result.passed is False
        assert "Silent" in result.message


# ---------------------------------------------------------------------------
# Aggregated gate runner
# ---------------------------------------------------------------------------


class TestRunHardGates:
    def test_forged_authority_and_plain_result_do_not_bind_child_receipt(
        self,
        tmp_path,
    ):
        (tmp_path / "result.json").write_text(
            json.dumps(
                {
                    "skill": "bulkrna-de",
                    "version": "1.0.0",
                    "completed_at": "2026-07-17T00:00:00+00:00",
                    "input_checksum": "",
                    "summary": {},
                    "data": {},
                    "status": "ok",
                }
            ),
            encoding="utf-8",
        )
        trace = _make_trace(
            skill_name="bulkrna-de",
            execution=ExecutionTrace(exit_code=0),
        )

        verdict = run_hard_gates(trace, tmp_path)

        assert verdict.all_passed is False
        assert any(
            gate.name == "receipt_bound" and not gate.passed
            for gate in verdict.results
        )

    def test_missing_trial_authority_fails_closed(self, tmp_path):
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")
        trace = RunTrace(
            trial_id=1,
            skill_name="test-skill",
            method="default",
            execution=ExecutionTrace(exit_code=0),
        )

        verdict = run_hard_gates(trace, tmp_path)

        assert verdict.all_passed is False
        assert any(
            gate.name == "authority_bound" and not gate.passed
            for gate in verdict.results
        )

    def test_all_pass(self, tmp_path):
        (tmp_path / "processed.h5ad").write_text("")
        trace = _make_trace(
            execution=ExecutionTrace(exit_code=0),
            data_shape=DataShapeTrace(
                n_obs_before=1000,
                n_obs_after=900,
                n_vars_after=500,
                cell_retention_rate=0.9,
            ),
        )
        _write_child_receipt(tmp_path, trace)
        verdict = run_hard_gates(trace, tmp_path)
        assert verdict.all_passed is True
        assert len(verdict.results) == 7
        assert len(verdict.failed_gates) == 0
        assert "All 7 hard gates passed" in verdict.summary()

    def test_default_gate_set_verifies_child_receipt_once(
        self,
        monkeypatch,
        tmp_path,
    ):
        from omicsclaw.autoagent import hard_gates as hard_gates_module

        (tmp_path / "processed.h5ad").write_text("")
        trace = _make_trace(
            execution=ExecutionTrace(exit_code=0),
            data_shape=DataShapeTrace(
                n_obs_before=100,
                n_obs_after=90,
                n_vars_after=50,
                cell_retention_rate=0.9,
            ),
        )
        _write_child_receipt(tmp_path, trace)
        real_verify = hard_gates_module.verify_child_trial_receipt
        calls = 0

        def counting_verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_verify(*args, **kwargs)

        monkeypatch.setattr(
            hard_gates_module,
            "verify_child_trial_receipt",
            counting_verify,
        )

        verdict = run_hard_gates(trace, tmp_path)

        assert verdict.all_passed is True
        assert calls == 1

    def test_crash_fails(self, tmp_path):
        trace = _make_trace(execution=ExecutionTrace(exit_code=1))
        verdict = run_hard_gates(trace, tmp_path)
        assert verdict.all_passed is False
        assert any(g.name == "no_crash" and not g.passed for g in verdict.results)

    def test_subset_gates(self, tmp_path):
        trace = _make_trace(execution=ExecutionTrace(exit_code=0))
        _write_child_receipt(tmp_path, trace)
        verdict = run_hard_gates(
            trace, tmp_path, gates=["no_crash"]
        )
        assert [gate.name for gate in verdict.results] == [
            "receipt_bound",
            "no_crash",
        ]
        assert verdict.all_passed is True

    def test_unknown_gate_name_fails_closed(self, tmp_path):
        trace = _make_trace(execution=ExecutionTrace(exit_code=0))

        verdict = run_hard_gates(
            trace,
            tmp_path,
            gates=["not-a-real-gate"],
        )

        assert verdict.all_passed is False
        assert any(
            gate.name == "not-a-real-gate" and not gate.passed
            for gate in verdict.results
        )

    def test_diagnostic_output(self, tmp_path):
        trace = _make_trace(
            execution=ExecutionTrace(exit_code=1),
            data_shape=DataShapeTrace(
                n_obs_before=1000,
                n_obs_after=10,
                cell_retention_rate=0.01,
            ),
        )
        verdict = run_hard_gates(trace, tmp_path)
        diag = verdict.to_diagnostic()
        assert "[FAIL]" in diag
        assert "no_crash" in diag

    def test_to_dict(self, tmp_path):
        (tmp_path / "result.json").write_text("{}")
        trace = _make_trace(
            skill_name="bulkrna-de",
            execution=ExecutionTrace(exit_code=0),
        )
        verdict = run_hard_gates(trace, tmp_path)
        d = verdict.to_dict()
        assert "all_passed" in d
        assert isinstance(d["results"], list)
        assert all("name" in r for r in d["results"])
