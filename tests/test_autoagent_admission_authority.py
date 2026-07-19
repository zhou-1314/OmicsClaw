from __future__ import annotations

from pathlib import Path
import hashlib
import json


def _trial_authority(skill_name: str):
    from omicsclaw.autoagent.authority import TrialSkillAuthority

    revision = "sha256:" + "a" * 64
    return TrialSkillAuthority(
        requested_skill_name=skill_name,
        canonical_skill_id=skill_name,
        skill_version="1.0.0",
        manifest_hash=revision,
        source_hash=revision,
        primary_anndata_path=None,
        skills_root="/tmp/skills",
    )


def test_parameter_candidate_must_pass_hard_gates_before_judgment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.hard_gates import (
        GateResult,
        HardGateVerdict,
    )
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "parameter-gates",
        search_space=SearchSpace(
            skill_name="test-skill",
            method="method",
            tunable=[
                ParameterDef(
                    name="alpha",
                    param_type="float",
                    default=1.0,
                    low=0.1,
                    high=5.0,
                    cli_flag="--alpha",
                )
            ],
        ),
        evaluator=Evaluator(metrics, skill_name="test-skill"),
        metrics=metrics,
        max_trials=2,
    )

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        output = loop.output_root / f"trial_{trial_id:04d}"
        output.mkdir()
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=float(trial_id + 1),
            raw_metrics={"score": float(trial_id + 1)},
            status="pending",
            reasoning=description,
            output_dir=str(output),
            authority=_trial_authority("test-skill"),
        )

    gate_calls: list[int] = []

    def fake_gates(trace, output_dir):
        gate_calls.append(int(trace.trial_id))
        if int(trace.trial_id) == 0:
            return HardGateVerdict(
                all_passed=True,
                results=[GateResult("receipt_bound", True, "bound")],
            )
        return HardGateVerdict(
            all_passed=False,
            results=[GateResult("cell_retention", False, "collapsed")],
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(
        loop,
        "_ask_llm",
        lambda _directive: {
            "params": {"alpha": 2.0},
            "reasoning": "try a higher value",
        },
    )
    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.run_hard_gates",
        fake_gates,
    )

    result = loop.run()

    assert gate_calls == [0, 1]
    assert result.best_trial is not None
    assert result.best_trial.trial_id == 0
    candidate = loop.ledger.all_trials()[1]
    assert candidate.status == "crash"
    assert candidate.composite_score == float("-inf")
    assert candidate.hard_gate_verdict["all_passed"] is False


def test_default_skill_surface_is_target_local_and_minimal(tmp_path: Path) -> None:
    from omicsclaw.autoagent import _build_target_skill_surface

    target_dir = tmp_path / "skills" / "singlecell" / "target-skill"
    target_dir.mkdir(parents=True)
    script = target_dir / "target_skill.py"
    script.write_text("def main():\n    return 0\n", encoding="utf-8")
    (target_dir / "SKILL.md").write_text("# Target\n", encoding="utf-8")
    unrelated = tmp_path / "skills" / "spatial" / "other-skill" / "other.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("BROKEN = True\n", encoding="utf-8")

    surface = _build_target_skill_surface(
        project_root=tmp_path,
        skill_name="target-skill",
        skill_info={"script": script, "alias": "target-skill"},
        surface_level=2,
    )

    assert surface.explicit_files == [
        "skills/singlecell/target-skill/SKILL.md",
        "skills/singlecell/target-skill/target_skill.py",
    ]
    assert surface.is_editable(script)
    assert not surface.is_editable(unrelated)


def test_default_skill_surface_rejects_non_python_or_aliased_entry(
    tmp_path: Path,
) -> None:
    import pytest

    from omicsclaw.autoagent import _build_target_skill_surface

    skill_dir = tmp_path / "skills" / "genomics" / "target-skill"
    skill_dir.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    alias = skill_dir / "target.py"
    alias.symlink_to(outside)

    with pytest.raises(ValueError, match="plain target-local Python"):
        _build_target_skill_surface(
            project_root=tmp_path,
            skill_name="target-skill",
            skill_info={"script": alias, "alias": "target-skill"},
            surface_level=2,
        )


def test_hard_gate_receipt_and_ledger_persist_exact_evidence_bytes(
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.hard_gates import run_hard_gates
    from omicsclaw.autoagent.trace import ExecutionTrace, RunTrace
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    authority = _trial_authority("test-skill")
    result_bytes = json.dumps(
        {
            "skill": "test-skill",
            "version": "1.0.0",
            "completed_at": "2026-07-17T00:00:00+00:00",
            "input_checksum": "",
            "summary": {},
            "data": {},
            "status": "ok",
        },
        separators=(",", ":"),
    ).encode()
    claim_bytes = json.dumps(
        {
            "schema_version": 1,
            "claim_id": "b" * 32,
            "owner": "skill:test-skill",
            "claimed_at": "2026-07-17T00:00:00+00:00",
            "audit_identity": {
                "skill_id": authority.canonical_skill_id,
                "skill_version": authority.skill_version,
                "skill_hash": authority.manifest_hash,
                "source_hash": authority.source_hash,
                "environment_id": "env:" + "b" * 20,
            },
            "runtime_source": "base",
        },
        separators=(",", ":"),
    ).encode()
    (tmp_path / "result.json").write_bytes(result_bytes)
    (tmp_path / OUTPUT_CLAIM_FILENAME).write_bytes(claim_bytes)
    trace = RunTrace(
        trial_id=1,
        skill_name="test-skill",
        method="method",
        authority=authority,
        execution=ExecutionTrace(exit_code=0),
    )

    verdict = run_hard_gates(trace, tmp_path)

    assert verdict.all_passed is True
    assert verdict.receipt["claim_id"] == "b" * 32
    assert verdict.receipt["claim_sha256"] == (
        "sha256:" + hashlib.sha256(claim_bytes).hexdigest()
    )
    assert verdict.receipt["result_sha256"] == (
        "sha256:" + hashlib.sha256(result_bytes).hexdigest()
    )

    trace.receipt = dict(verdict.receipt)
    trace.hard_gate_verdict = verdict.to_dict()
    trace_path = trace.save(tmp_path)
    loaded_trace = RunTrace.load(trace_path)
    assert loaded_trace.receipt == verdict.receipt
    assert loaded_trace.hard_gate_verdict["all_passed"] is True

    record = TrialRecord(
        trial_id=1,
        params={},
        composite_score=1.0,
        authority=authority,
        receipt=dict(verdict.receipt),
        hard_gate_verdict=verdict.to_dict(),
    )
    loaded_record = TrialRecord.from_dict(record.to_dict())
    assert loaded_record.receipt == verdict.receipt
    assert loaded_record.hard_gate_verdict["all_passed"] is True
