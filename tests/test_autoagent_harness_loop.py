"""Tests for omicsclaw.autoagent.harness_loop.

Uses mocked LLM calls and skill execution to verify the loop logic
without running real bioinformatics pipelines.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from omicsclaw.autoagent.edit_surface import EditSurface
from omicsclaw.autoagent.evaluator import EvaluationResult, Evaluator
from omicsclaw.autoagent.harness_loop import HarnessLoop
from omicsclaw.autoagent.harness_workspace import HarnessWorkspace, PromotionResult
from omicsclaw.autoagent.metrics_registry import MetricDef
from omicsclaw.autoagent.runner import TrialExecution
from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace
from omicsclaw.autoagent.trace import TraceCollector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_source_repository(source_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "OmicsClaw Test"],
        cwd=source_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "omicsclaw-test@local"],
        cwd=source_root,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "test source baseline"],
        cwd=source_root,
        check=True,
    )


def _make_surface(tmp_path: Path) -> EditSurface:
    """Create a test surface with a simple editable file."""
    skill_dir = tmp_path / "project" / "skills" / "test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "test.py").write_text(
        "def filter_cells(adata, min_genes=200):\n"
        "    return adata[adata.obs['n_genes'] >= min_genes]\n"
    )
    (skill_dir / "SKILL.md").write_text("# Test Skill\nmin_genes: 200\n")
    _init_source_repository(tmp_path / "project")
    return EditSurface(
        max_level=2,
        project_root=tmp_path / "project",
        explicit_files=[
            "skills/test/test.py",
            "skills/test/SKILL.md",
        ],
    )


def _make_search_space() -> SearchSpace:
    return SearchSpace(
        skill_name="test-skill",
        method="scanpy",
        tunable=[
            ParameterDef(
                name="min_genes", param_type="int",
                default=200, low=50, high=1000,
                cli_flag="--min-genes",
            ),
        ],
        fixed={},
    )


def _make_evaluator() -> Evaluator:
    metrics = {
        "quality": MetricDef(
            source="result.json:summary.quality",
            direction="maximize",
            weight=1.0,
        ),
    }
    return Evaluator(metrics, skill_name="test-skill")


def _trial_authority():
    from omicsclaw.autoagent.authority import TrialSkillAuthority

    revision = "sha256:" + "d" * 64
    return TrialSkillAuthority(
        requested_skill_name="test-skill",
        canonical_skill_id="test-skill",
        skill_version="1.0.0",
        manifest_hash=revision,
        source_hash=revision,
        primary_anndata_path=None,
        skills_root="/test/skills",
    )


def _write_mock_child_receipt(output_dir: Path, summary: dict) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    authority = _trial_authority()
    result = {
        "skill": "test-skill",
        "version": "1.0.0",
        "completed_at": "2026-07-17T00:00:00+00:00",
        "input_checksum": "",
        "summary": summary,
        "data": {"effective_params": {"min_genes": 200}},
        "status": "ok",
    }
    (output_dir / "result.json").write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    (output_dir / OUTPUT_CLAIM_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claim_id": "d" * 32,
                "owner": "skill:test-skill",
                "claimed_at": "2026-07-17T00:00:00+00:00",
                "audit_identity": {
                    "skill_id": authority.canonical_skill_id,
                    "skill_version": authority.skill_version,
                    "skill_hash": authority.manifest_hash,
                    "source_hash": authority.source_hash,
                    "environment_id": "env:" + "d" * 20,
                },
                "runtime_source": "base",
            }
        ),
        encoding="utf-8",
    )


def _make_trial_execution(
    output_dir: Path,
    success: bool = True,
    score: float = 0.5,
) -> TrialExecution:
    """Create a fake TrialExecution with result.json on disk."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if success:
        _write_mock_child_receipt(
            output_dir,
            {"quality": score, "n_cells": 100, "n_genes": 50},
        )
    return TrialExecution(
        success=success,
        output_dir=str(output_dir),
        duration_seconds=5.0,
        exit_code=0 if success else 1,
        stderr="" if success else "Error occurred",
        authority=_trial_authority(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHarnessLoopBaseline:
    """Tests for baseline execution."""

    def test_baseline_runs_from_clean_commit_worktree_without_ignored_snapshot_state(
        self,
        tmp_path,
    ):
        surface = _make_surface(tmp_path)
        source_cache = surface.project_root / "runtime.cache"
        source_cache.write_bytes(b"uncommitted ignored runtime state")
        (surface.project_root / ".gitignore").write_text(
            "runtime.cache\n",
            encoding="utf-8",
        )
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=1,
        )
        observed_baseline_root: list[Path] = []

        def mock_execute(*args, **kwargs):
            baseline_root = Path(kwargs["project_root"])
            observed_baseline_root.append(baseline_root)
            assert baseline_root.name == "iter_0000"
            assert baseline_root != surface.project_root
            assert not (baseline_root / "runtime.cache").exists()
            return _make_trial_execution(
                Path(kwargs.get("output_dir") or args[2]),
                success=True,
                score=0.5,
            )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            mock_execute,
        ), mock_patch.object(
            loop,
            "_call_llm",
            return_value=json.dumps(
                {"converged": True, "reasoning": "Baseline is sufficient."}
            ),
        ):
            result = loop.run()

        assert result.success is True
        assert len(observed_baseline_root) == 1
        assert not observed_baseline_root[0].exists()
        assert not (loop.output_root / "sandbox_repo" / "runtime.cache").exists()

    def test_baseline_rejects_ignored_state_created_during_execution(
        self,
        tmp_path,
    ):
        surface = _make_surface(tmp_path)
        (surface.project_root / ".gitignore").write_text(
            "runtime.cache\n",
            encoding="utf-8",
        )
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=1,
        )

        def mock_execute(*args, **kwargs):
            baseline_root = Path(kwargs["project_root"])
            (baseline_root / "runtime.cache").write_text(
                "runtime-owned baseline state\n",
                encoding="utf-8",
            )
            return _make_trial_execution(
                Path(kwargs.get("output_dir") or args[2]),
                success=True,
                score=0.5,
            )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            mock_execute,
        ), mock_patch.object(
            loop,
            "_call_llm",
            side_effect=AssertionError("a contaminated baseline must not evolve"),
        ):
            result = loop.run()

        assert result.success is False
        assert "not clean" in (result.error_message or "").lower()

    def test_failed_execution_never_reads_or_writes_trial_leaf(self, tmp_path):
        surface = _make_surface(tmp_path)
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
        )
        trial_leaf = loop.output_root / "trial_0000"

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            return_value=TrialExecution(
                success=False,
                output_dir=str(trial_leaf),
                duration_seconds=1.0,
                exit_code=1,
                stderr="child failed",
            ),
        ), mock_patch.object(
            TraceCollector,
            "collect",
            side_effect=AssertionError("failed execution must not read artifacts"),
        ):
            record, trace = loop._run_and_trace(
                trial_id=0,
                params={"min_genes": 200},
                project_root=surface.project_root,
            )

        assert record.status == "crash"
        assert trace.execution.exit_code == 1
        assert trace.execution.stderr == "child failed"
        assert not trial_leaf.exists()

    def test_failed_execution_ledger_record_keeps_only_verified_authority(
        self,
        tmp_path,
    ):
        surface = _make_surface(tmp_path)
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
        )
        execution = TrialExecution(
            success=False,
            output_dir=str(loop.output_root / "trial_0000"),
            duration_seconds=1.0,
            exit_code=1,
            stderr="skill failed",
            authority=_trial_authority(),
        )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            return_value=execution,
        ):
            record, trace = loop._run_and_trace(
                trial_id=0,
                params={},
                project_root=surface.project_root,
            )

        assert trace.authority == execution.authority
        assert record.authority == execution.authority

    def test_success_without_verified_authority_is_not_scored(self, tmp_path):
        surface = _make_surface(tmp_path)
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
        )

        def mock_execute(*_args, **kwargs):
            output = Path(kwargs["output_dir"])
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.json").write_text(
                json.dumps({"summary": {"quality": 0.9}}),
                encoding="utf-8",
            )
            return TrialExecution(
                success=True,
                output_dir=str(output),
                duration_seconds=1.0,
                exit_code=0,
            )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            mock_execute,
        ), mock_patch.object(
            TraceCollector,
            "collect",
            side_effect=AssertionError(
                "unbound success must not read or persist trial artifacts"
            ),
        ), mock_patch.object(
            loop.evaluator,
            "evaluate",
            side_effect=AssertionError("unbound execution must not be scored"),
        ):
            record, trace = loop._run_and_trace(
                trial_id=0,
                params={},
                project_root=surface.project_root,
            )

        assert record.status == "crash"
        assert record.composite_score == float("-inf")
        assert "authority" in record.error_output.lower()
        assert trace.authority is None
        assert not (Path(record.output_dir) / "run_trace.json").exists()

    def test_scored_trace_and_ledger_durably_preserve_evidence_authority(
        self,
        tmp_path,
    ):
        surface = _make_surface(tmp_path)
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
        )
        authority = _trial_authority()

        def mock_execute(*_args, **kwargs):
            return _make_trial_execution(
                Path(kwargs["output_dir"]),
                success=True,
                score=0.83,
            )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            mock_execute,
        ), mock_patch.object(
            loop.evaluator,
            "evaluate",
            return_value=EvaluationResult(
                composite_score=0.83,
                raw_metrics={"derived_quality": 0.83},
                success=True,
            ),
        ):
            record, trace = loop._run_and_trace(
                trial_id=0,
                params={"min_genes": 200},
                project_root=surface.project_root,
            )

        persisted_trace = trace.load(Path(record.output_dir) / "run_trace.json")
        assert trace.quality.quality_metrics == {"derived_quality": 0.83}
        assert persisted_trace.quality.quality_metrics == {"derived_quality": 0.83}
        assert record.authority == authority

        record.status = "baseline"
        loop.ledger.append(record)
        reloaded = type(loop.ledger)(loop.ledger.path)
        assert reloaded.latest().authority == authority

    def test_evaluation_failure_keeps_durable_execution_diagnostics(self, tmp_path):
        surface = _make_surface(tmp_path)
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
        )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            side_effect=lambda *_args, **kwargs: _make_trial_execution(
                Path(kwargs["output_dir"]),
                success=True,
                score=0.5,
            ),
        ), mock_patch.object(
            loop.evaluator,
            "evaluate",
            side_effect=RuntimeError("metric backend failed"),
        ):
            record, trace = loop._run_and_trace(
                trial_id=0,
                params={},
                project_root=surface.project_root,
            )

        persisted_trace = trace.load(Path(record.output_dir) / "run_trace.json")
        assert record.status == "crash"
        assert record.error_output == "Evaluation error: metric backend failed"
        assert record.authority == _trial_authority()
        assert persisted_trace.execution.exit_code == 0
        assert persisted_trace.authority == _trial_authority()

    def test_baseline_crash_stops(self, tmp_path):
        """If baseline crashes, loop stops immediately."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=5,
        )

        # Mock execute_trial to crash
        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            return _make_trial_execution(Path(od), success=False)

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ):
            result = loop.run()

        assert not result.success
        assert "Baseline crashed" in result.error_message

    def test_baseline_hard_gate_failure_is_terminal_admission_failure(
        self,
        tmp_path,
    ):
        from omicsclaw.autoagent.experiment_ledger import TrialRecord
        from omicsclaw.autoagent.hard_gates import GateResult, HardGateVerdict
        from omicsclaw.autoagent.trace import ExecutionTrace, RunTrace

        surface = _make_surface(tmp_path)
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=tmp_path / "output",
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=1,
        )
        output = loop.output_root / "trial_0000"
        record = TrialRecord(
            trial_id=0,
            params={"min_genes": 200},
            composite_score=0.9,
            status="pending",
            output_dir=str(output),
        )
        trace = RunTrace(
            trial_id=0,
            skill_name="test-skill",
            method="scanpy",
            authority=_trial_authority(),
            execution=ExecutionTrace(exit_code=0),
        )
        failed = HardGateVerdict(
            all_passed=False,
            results=[
                GateResult(
                    name="receipt_bound",
                    passed=False,
                    message="missing child receipt",
                )
            ],
        )

        with mock_patch.object(
            loop,
            "_run_and_trace",
            return_value=(record, trace),
        ), mock_patch(
            "omicsclaw.autoagent.harness_loop.run_hard_gates",
            return_value=failed,
        ) as gates, mock_patch.object(
            loop,
            "_call_llm",
            side_effect=AssertionError("LLM must not run after baseline rejection"),
        ):
            result = loop.run()

        assert result.success is False
        assert result.converged is False
        assert "Baseline hard gates failed" in (result.error_message or "")
        gates.assert_called_once_with(trace, output)

    def test_baseline_nonfinite_score_stops(self, tmp_path):
        """If baseline has no readable metrics, loop stops."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
        )

        # Mock execute_trial to succeed but produce no metrics
        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            od = Path(od)
            od.mkdir(parents=True, exist_ok=True)
            # Valid child receipt without the expected metric.
            _write_mock_child_receipt(od, {"unrelated": 42})
            return TrialExecution(
                success=True, output_dir=str(od),
                duration_seconds=1.0, exit_code=0,
                authority=_trial_authority(),
            )

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ):
            result = loop.run()

        assert not result.success
        assert "metrics" in result.error_message.lower()


class TestHarnessLoopEvolution:
    """Tests for the evolution iterations."""

    def test_trials_run_in_isolated_worktree(self, tmp_path):
        """Patched trials execute in a sandbox while the source tree stays clean."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=1,
            auto_promote=True,
        )

        source_file = tmp_path / "project" / "skills" / "test" / "test.py"
        observed = {"sandbox_checked": False}
        call_count = [0]

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_trial_execution(Path(od), success=True, score=0.5)

            sandbox_root = Path(kwargs["project_root"])
            sandbox_file = sandbox_root / "skills" / "test" / "test.py"
            assert sandbox_root != surface.project_root
            assert "min_genes=200" in source_file.read_text()
            assert "min_genes=300" in sandbox_file.read_text()
            observed["sandbox_checked"] = True
            return _make_trial_execution(Path(od), success=True, score=0.8)

        llm_response = json.dumps({
            "patch_plan": {
                "target_files": ["skills/test/test.py"],
                "description": "Improve threshold",
            },
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=200",
                    "new_code": "min_genes=300",
                }],
            }],
            "reasoning": "Higher threshold for better filtering.",
        })

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(loop, "_call_llm", return_value=llm_response):
            result = loop.run()

        assert result.success
        assert observed["sandbox_checked"] is True
        assert "min_genes=300" in source_file.read_text()
        assert result.promotion["status"] == "applied"

    def test_accepts_improving_patch(self, tmp_path):
        """A patch that improves quality is accepted."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=2,
            auto_promote=True,
        )

        call_count = [0]

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            call_count[0] += 1
            # Baseline score=0.5, patched score=0.8
            score = 0.5 if call_count[0] == 1 else 0.8
            return _make_trial_execution(Path(od), success=True, score=score)

        # LLM returns a valid patch
        llm_response = json.dumps({
            "patch_plan": {
                "target_files": ["skills/test/test.py"],
                "description": "Improve threshold",
            },
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=200",
                    "new_code": "min_genes=300",
                }],
            }],
            "reasoning": "Higher threshold for better filtering.",
        })

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(loop, "_call_llm", return_value=llm_response):
            result = loop.run()

        assert result.success
        assert result.patches_accepted >= 1
        assert result.improvement_pct > 0
        assert result.accepted_patches
        accepted = result.accepted_patches[0]
        assert accepted.commit_hash
        assert Path(accepted.artifact_path).exists()
        assert Path(accepted.manifest_path).exists()
        assert result.best_trial.code_state["commit_hash"] == accepted.commit_hash
        summary = json.loads((output_root / "harness_summary.json").read_text())
        assert summary["accepted_patch_commits"] == [accepted.commit_hash]
        assert summary["promotion"]["status"] == "applied"

    def test_reverts_failing_patch(self, tmp_path):
        """A patch that crashes is reverted and recorded in failure bank."""
        from omicsclaw.autoagent.hard_gates import run_hard_gates as real_hard_gates

        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=2,
        )

        call_count = [0]

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_trial_execution(Path(od), success=True, score=0.5)
            # Patched version crashes
            return _make_trial_execution(Path(od), success=False)

        llm_response = json.dumps({
            "patch_plan": {"target_files": ["skills/test/test.py"]},
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=200",
                    "new_code": "min_genes=-1",
                }],
            }],
            "reasoning": "Try negative threshold.",
        })

        def reject_failed_trace_artifact_reads(trace, output_dir):
            assert trace.execution.exit_code == 0, (
                "a failed execution must bypass artifact hard gates"
            )
            return real_hard_gates(trace, output_dir)

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(
            loop, "_call_llm", return_value=llm_response
        ), mock_patch(
            "omicsclaw.autoagent.harness_loop.run_hard_gates",
            side_effect=reject_failed_trace_artifact_reads,
        ):
            result = loop.run()

        assert result.patches_rejected >= 1
        # File should be reverted
        content = (
            tmp_path / "project" / "skills" / "test" / "test.py"
        ).read_text()
        assert "min_genes=200" in content

        # Failure recorded
        assert len(loop.failure_bank) >= 1

    def test_terminal_crash_cleans_control_witness_before_promotion(self, tmp_path):
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"
        source_file = surface.project_root / "skills" / "test" / "test.py"
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=4,
            auto_promote=True,
        )
        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            output_dir = Path(kwargs.get("output_dir") or args[2])
            if call_count == 1:
                return _make_trial_execution(output_dir, success=True, score=0.5)
            if call_count == 2:
                return _make_trial_execution(output_dir, success=True, score=0.8)
            if call_count == 5:
                (output_root / "sandbox_repo" / ".git" / "info" / "attributes").write_text(
                    "skills/test/test.py text eol=lf\n",
                    encoding="utf-8",
                )
                shutil.rmtree(Path(kwargs["project_root"]))
            return _make_trial_execution(output_dir, success=False)

        accepted_patch = json.dumps({
            "patch_plan": {"target_files": ["skills/test/test.py"]},
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=200",
                    "new_code": "min_genes=300",
                }],
            }],
            "reasoning": "Establish one accepted candidate.",
        })
        crashing_patch = json.dumps({
            "patch_plan": {"target_files": ["skills/test/test.py"]},
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=300",
                    "new_code": "min_genes=301",
                }],
            }],
            "reasoning": "Candidate crashes before admission.",
        })

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            mock_execute,
        ), mock_patch.object(
            loop,
            "_call_llm",
            side_effect=[accepted_patch, crashing_patch, crashing_patch, crashing_patch],
        ), mock_patch.object(
            loop,
            "_promote_workspace",
            wraps=loop._promote_workspace,
        ) as promote:
            with pytest.raises(RuntimeError, match="worktree cleanup"):
                loop.run()

        assert promote.call_count == 0
        assert (output_root / "git_control_compromised.json").is_file()
        assert "min_genes=200" in source_file.read_text(encoding="utf-8")

    def test_terminal_cleanup_failure_blocks_promotion_and_latches_git_authority(
        self,
        tmp_path,
    ):
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"
        source_file = surface.project_root / "skills" / "test" / "test.py"
        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=4,
            auto_promote=True,
        )
        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            output_dir = Path(kwargs.get("output_dir") or args[2])
            if call_count == 1:
                return _make_trial_execution(output_dir, success=True, score=0.5)
            if call_count == 2:
                return _make_trial_execution(output_dir, success=True, score=0.8)
            return _make_trial_execution(output_dir, success=False)

        accepted_patch = json.dumps({
            "patch_plan": {"target_files": ["skills/test/test.py"]},
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=200",
                    "new_code": "min_genes=300",
                }],
            }],
            "reasoning": "Establish one accepted candidate.",
        })
        crashing_patch = json.dumps({
            "patch_plan": {"target_files": ["skills/test/test.py"]},
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=300",
                    "new_code": "min_genes=301",
                }],
            }],
            "reasoning": "Candidate crashes before admission.",
        })
        real_subprocess_run = subprocess.run
        real_rmtree = shutil.rmtree
        cleanup_remove_failed = False

        def fail_final_worktree_remove(command, *args, **kwargs):
            nonlocal cleanup_remove_failed
            if (
                command[:4] == ["git", "worktree", "remove", "--force"]
                and Path(command[4]).name == "iter_0004"
            ):
                cleanup_remove_failed = True
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="injected worktree cleanup failure",
                )
            if cleanup_remove_failed:
                raise AssertionError("cleanup failure must forbid later Git calls")
            return real_subprocess_run(command, *args, **kwargs)

        def retain_failed_worktree(path, *args, **kwargs):
            if Path(path).name == "iter_0004":
                raise AssertionError("cleanup must not use raw deletion fallback")
            return real_rmtree(path, *args, **kwargs)

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial",
            mock_execute,
        ), mock_patch.object(
            loop,
            "_call_llm",
            side_effect=[accepted_patch, crashing_patch, crashing_patch, crashing_patch],
        ), mock_patch(
            "omicsclaw.autoagent.harness_workspace.subprocess.run",
            side_effect=fail_final_worktree_remove,
        ), mock_patch(
            "omicsclaw.autoagent.harness_workspace.shutil.rmtree",
            side_effect=retain_failed_worktree,
        ), mock_patch.object(
            HarnessWorkspace,
            "promote_accepted_state",
            autospec=True,
            return_value=PromotionResult(status="applied"),
        ) as promote:
            with pytest.raises(RuntimeError, match="worktree cleanup"):
                loop.run()

        assert promote.call_count == 0
        assert "min_genes=200" in source_file.read_text(encoding="utf-8")
        compromise = json.loads(
            (output_root / "git_control_compromised.json").read_text(
                encoding="utf-8"
            )
        )
        assert compromise["reason"] == "worktree_remove_failed"
        assert (output_root / "sandbox_worktrees" / "iter_0004").is_dir()

    def test_convergence_signal(self, tmp_path):
        """LLM can signal convergence to stop the loop."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=5,
        )

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            return _make_trial_execution(Path(od), success=True, score=0.5)

        converge_response = json.dumps({
            "converged": True,
            "reasoning": "No further improvements possible.",
        })

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(
            loop, "_call_llm", return_value=converge_response,
        ):
            result = loop.run()

        assert result.success
        assert result.converged

    def test_validation_failure_recorded(self, tmp_path):
        """Invalid patches emit terminal discard events instead of hanging."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=2,
        )

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            return _make_trial_execution(Path(od), success=True, score=0.5)

        # Patch targets a file outside the surface
        bad_patch = json.dumps({
            "patch_plan": {"target_files": ["omicsclaw/runtime/tool_executor.py"]},
            "diffs": [{
                "file": "omicsclaw/runtime/tool_executor.py",
                "hunks": [{"old_code": "x", "new_code": "y"}],
            }],
            "reasoning": "Try to modify frozen file.",
        })
        events: list[tuple[str, dict[str, object]]] = []

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(loop, "_call_llm", return_value=bad_patch):
            result = loop.run(
                on_event=lambda event_type, data: events.append((event_type, data))
            )

        assert result.patches_rejected >= 1
        assert len(loop.failure_bank) >= 1
        assert len(loop.ledger.all_trials()) == 3

        rejected_complete = [
            data
            for event_type, data in events
            if event_type == "trial_complete" and int(data["trial_id"]) > 0
        ]
        assert len(rejected_complete) == 2
        assert all(data["status"] == "discard" for data in rejected_complete)
        assert all(data["stage"] == "validation" for data in rejected_complete)
        assert all("cannot be edited" in str(data["error"]) for data in rejected_complete)

        rejected_judgment = [
            data
            for event_type, data in events
            if event_type == "trial_judgment" and int(data["trial_id"]) > 0
        ]
        assert len(rejected_judgment) == 2
        assert all(data["decision"] == "discard" for data in rejected_judgment)
        assert all(data["stage"] == "validation" for data in rejected_judgment)

    def test_validation_failure_limit_returns_error(self, tmp_path):
        """Three consecutive validation failures terminate the run as an error."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=3,
        )

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            return _make_trial_execution(Path(od), success=True, score=0.5)

        bad_patch = json.dumps({
            "patch_plan": {"target_files": ["skills/not-real/file.py"]},
            "diffs": [{
                "file": "skills/not-real/file.py",
                "hunks": [{"old_code": "x", "new_code": "y"}],
            }],
            "reasoning": "Target a path that does not exist.",
        })

        events: list[tuple[str, dict[str, object]]] = []

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(loop, "_call_llm", return_value=bad_patch):
            result = loop.run(
                on_event=lambda event_type, data: events.append((event_type, data))
            )

        assert result.success is False
        assert result.error_message == "3 consecutive patch validation failures."
        assert len(loop.ledger.all_trials()) == 4
        assert [event_type for event_type, _data in events].count("error") == 1

    def test_harness_summary_written(self, tmp_path):
        """harness_summary.json is written to output_root."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=1,
        )

        converge = json.dumps({"converged": True, "reasoning": "done"})

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            return _make_trial_execution(Path(od), success=True, score=0.5)

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(loop, "_call_llm", return_value=converge):
            loop.run()

        summary_path = output_root / "harness_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["success"] is True
        assert summary["skill"] == "test-skill"

    def test_emits_frontend_compatible_progress_and_trial_ids(self, tmp_path):
        """Harness SSE payloads include the legacy optimize UI fields."""
        surface = _make_surface(tmp_path)
        output_root = tmp_path / "output"

        loop = HarnessLoop(
            skill_name="test-skill",
            method="scanpy",
            input_path="",
            output_root=output_root,
            surface=surface,
            evaluator=_make_evaluator(),
            search_space=_make_search_space(),
            demo=True,
            max_iterations=2,
        )

        call_count = [0]

        def mock_execute(*args, **kwargs):
            od = kwargs.get("output_dir") or args[2]
            call_count[0] += 1
            score = 0.5 if call_count[0] == 1 else 0.8
            return _make_trial_execution(Path(od), success=True, score=score)

        llm_response = json.dumps({
            "patch_plan": {
                "target_files": ["skills/test/test.py"],
                "description": "Improve threshold",
            },
            "diffs": [{
                "file": "skills/test/test.py",
                "hunks": [{
                    "old_code": "min_genes=200",
                    "new_code": "min_genes=300",
                }],
            }],
            "reasoning": "Higher threshold for better filtering.",
        })

        events: list[tuple[str, dict[str, object]]] = []

        with mock_patch(
            "omicsclaw.autoagent.harness_loop.execute_trial", mock_execute,
        ), mock_patch.object(loop, "_call_llm", return_value=llm_response):
            result = loop.run(on_event=lambda event_type, data: events.append((event_type, data)))

        assert result.success

        progress_events = [data for event_type, data in events if event_type == "progress"]
        assert [int(data["completed"]) for data in progress_events[:3]] == [0, 1, 1]
        assert [int(data["total"]) for data in progress_events[:3]] == [2, 2, 2]

        trial_start = next(data for event_type, data in events if event_type == "trial_start")
        assert trial_start["trial_id"] == 1
        assert trial_start["iteration"] == 1

        trial_complete_events = [data for event_type, data in events if event_type == "trial_complete"]
        assert trial_complete_events[0]["trial_id"] == 0
        assert trial_complete_events[1]["trial_id"] == 1

        trial_judgment = next(data for event_type, data in events if event_type == "trial_judgment")
        assert trial_judgment["trial_id"] == 1

        done_event = next(data for event_type, data in events if event_type == "done")
        assert int(done_event["total_trials"]) >= 1
        assert done_event["best_trial"]["trial_id"] == 1
