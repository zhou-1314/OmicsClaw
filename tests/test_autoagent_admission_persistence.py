from __future__ import annotations

from pathlib import Path


def test_parameter_admission_reconstruction_failure_is_a_failed_verdict(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A trace collector fault must fail one trial, not crash the whole loop."""

    from omicsclaw.autoagent.authority import TrialSkillAuthority
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import SearchSpace

    metric = MetricDef(
        source="result.json:summary.score",
        direction="maximize",
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "session",
        search_space=SearchSpace(
            skill_name="test-skill",
            method="method",
            tunable=[],
        ),
        evaluator=Evaluator({"score": metric}, skill_name="test-skill"),
        metrics={"score": metric},
        max_trials=1,
    )
    output_dir = loop.output_root / "trial_0000"
    output_dir.mkdir()
    revision = "sha256:" + "a" * 64
    trial = TrialRecord(
        trial_id=0,
        params={},
        composite_score=1.0,
        output_dir=str(output_dir),
        authority=TrialSkillAuthority(
            requested_skill_name="test-skill",
            canonical_skill_id="test-skill",
            skill_version="1.0.0",
            manifest_hash=revision,
            source_hash=revision,
            primary_anndata_path=None,
            skills_root=str(tmp_path / "skills"),
        ),
    )

    def fail_collect(**_kwargs):
        raise RuntimeError("injected trace reconstruction failure")

    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.TraceCollector.collect",
        fail_collect,
    )

    verdict = loop._admit_trial_hard_gates(trial)

    assert verdict is not None
    assert verdict.all_passed is False
    assert trial.status == "crash"
    assert trial.composite_score == float("-inf")
    assert trial.hard_gate_verdict["results"][0]["name"] == "admission_evidence"
    assert "injected trace reconstruction failure" in trial.error_output


def test_harness_admission_trace_persistence_failure_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.hard_gates import GateResult, HardGateVerdict
    from omicsclaw.autoagent.harness_loop import HarnessLoop
    from omicsclaw.autoagent.trace import RunTrace

    trial = TrialRecord(
        trial_id=1,
        params={},
        composite_score=2.0,
        output_dir=str(tmp_path),
    )
    trace = RunTrace(trial_id=1, skill_name="test-skill", method="method")
    admitted = HardGateVerdict(
        all_passed=True,
        results=[GateResult("receipt_bound", True, "bound")],
        receipt={"claim_sha256": "sha256:" + "a" * 64},
    )

    def fail_save(_output_dir: Path) -> Path:
        raise OSError("injected durable trace failure")

    monkeypatch.setattr(trace, "save", fail_save)

    verdict = HarnessLoop._bind_admission_evidence(
        trial,
        trace,
        admitted,
        tmp_path,
    )

    assert verdict.all_passed is False
    assert verdict.failed_gates[-1].name == "durable_admission"
    assert trial.hard_gate_verdict["all_passed"] is False
    assert trial.receipt == admitted.receipt
