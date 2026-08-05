"""OmicBench preprocessing adapters for governed sc-preprocessing protocols."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path


_CASES = {
    "A02_normalize_log": {
        "input": "environment/data/A01_qc_oracle.h5ad",
        "hvg_flavor": "seurat",
    },
    "A03_hvg": {
        "input": "environment/data/A02_normalize_oracle.h5ad",
        "hvg_flavor": "seurat_v3",
    },
}


def _revision_from_result(result) -> dict[str, str]:
    identity = getattr(result, "audit_identity", None)
    if identity is None:
        return {}
    return {
        "skill_id": identity.skill_id,
        "version": identity.skill_version,
        "manifest_hash": identity.skill_hash,
        "source_hash": identity.source_hash,
    }


def _write_result(
    result_path: Path,
    *,
    outcome: str,
    reason_code: str,
    metrics: dict[str, float] | None = None,
    skill_revision: dict[str, str] | None = None,
    evidence_refs: list[str] | None = None,
) -> None:
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome": outcome,
                "reason_code": reason_code,
                "metrics": metrics or {},
                "skill_revision": skill_revision or {},
                "evidence_refs": evidence_refs or [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _axis_identity(path: Path) -> tuple[tuple[int, int], tuple[str, ...], tuple[str, ...]]:
    import anndata as ad

    adata = ad.read_h5ad(path, backed="r")
    try:
        return (
            (int(adata.n_obs), int(adata.n_vars)),
            tuple(str(value) for value in adata.obs_names),
            tuple(str(value) for value in adata.var_names),
        )
    finally:
        adata.file.close()


def main() -> int:
    dataset = Path(os.environ["OMICSCLAW_EVALUATION_DATASET"]).resolve()
    output = Path(os.environ["OMICSCLAW_EVALUATION_OUTPUT"]).resolve()
    result_path = Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).resolve()
    try:
        task = tomllib.loads((dataset / "task.toml").read_text(encoding="utf-8"))
        case_id = str(task["task"]["id"])
        case = _CASES[case_id]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        _write_result(result_path, outcome="failed", reason_code="dataset_invalid")
        return 1
    source = dataset / str(case["input"])
    rubric_path = dataset / "tests" / "rubric.json"
    grader_dir = dataset / "tests"
    if not source.is_file() or not rubric_path.is_file():
        _write_result(result_path, outcome="failed", reason_code="dataset_invalid")
        return 1

    staged_input = output / "input.h5ad"
    run_output = output / "skill-run"
    shutil.copyfile(source, staged_input)
    input_identity = _axis_identity(staged_input)

    # The Skill receives only a staged input file. The benchmark root and
    # grader-only oracle are restored to this evaluator after execution.
    hidden_environment = {}
    for name in (
        "OMICSCLAW_EVALUATION_DATASET",
        "OMICSCLAW_EVALUATION_OUTPUT",
        "OMICSCLAW_EVALUATION_RESULT",
        "OMICSCLAW_EVALUATION_SKILL_REVISION",
    ):
        if name in os.environ:
            hidden_environment[name] = os.environ.pop(name)

    try:
        from omicsclaw.skill.runner import run_skill

        run_result = run_skill(
            "sc-preprocessing",
            input_path=str(staged_input),
            output_dir=str(run_output),
            extra_args=[
                "--method",
                "scanpy",
                "--min-genes",
                "0",
                "--min-cells",
                "0",
                "--max-mt-pct",
                "100",
                "--n-top-hvg",
                "2000",
                "--n-pcs",
                "50",
                "--no-remove-doublets",
                "--preserve-var-names",
                "--normalization-target-sum",
                "10000",
                "--scanpy-hvg-flavor",
                str(case["hvg_flavor"]),
            ],
        )
    finally:
        os.environ.update(hidden_environment)

    observed_revision = _revision_from_result(run_result)
    expected_revision = json.loads(
        os.environ.get("OMICSCLAW_EVALUATION_SKILL_REVISION", "{}")
    )
    if observed_revision != expected_revision:
        _write_result(
            result_path,
            outcome="failed",
            reason_code="revision_mismatch",
            skill_revision=observed_revision,
        )
        return 1
    if not run_result.success:
        _write_result(
            result_path,
            outcome="failed",
            reason_code="skill_failed",
            skill_revision=observed_revision,
        )
        return 1

    processed = run_output / "processed.h5ad"
    try:
        skill_result = json.loads(
            (run_output / "result.json").read_text(encoding="utf-8")
        )
        effective = skill_result["data"]["effective_params"]
        parameter_contract_holds = (
            effective["method"] == "scanpy"
            and effective["scanpy_hvg_flavor"] == case["hvg_flavor"]
            and float(effective["normalization_target_sum"]) == 10000.0
            and bool(effective["preserve_var_names"])
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        parameter_contract_holds = False
    if (
        not processed.is_file()
        or not parameter_contract_holds
        or _axis_identity(processed) != input_identity
    ):
        _write_result(
            result_path,
            outcome="failed",
            reason_code="semantic_contract_failed",
            skill_revision=observed_revision,
        )
        return 1
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    sys.path.insert(0, str(grader_dir))
    try:
        from grader import grade

        oracle = rubric.get("oracle")
        oracle_path = str((grader_dir / oracle).resolve()) if oracle else None
        grade_result = grade(
            final_adata_path=str(processed),
            checks=rubric["checks"],
            oracle_path=oracle_path,
            task_id=rubric["task_id"],
        )
    finally:
        sys.path.remove(str(grader_dir))

    report = {
        "task_id": grade_result.task_id,
        "passed": grade_result.passed,
        "score": grade_result.score,
        "checks": grade_result.rubric,
        "failure_mode": grade_result.failure_mode.value,
    }
    report_bytes = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
    (output / "omicbench_report.json").write_bytes(report_bytes)
    report_digest = "sha256:" + hashlib.sha256(report_bytes).hexdigest()
    passed_checks = sum(bool(value) for value in grade_result.rubric.values())
    metrics = {
        "score": float(grade_result.score),
        "checks_passed": float(passed_checks),
        "checks_total": float(len(grade_result.rubric)),
    }
    _write_result(
        result_path,
        outcome="succeeded" if grade_result.passed else "failed",
        reason_code="none" if grade_result.passed else "omicbench_failed",
        metrics=metrics,
        skill_revision=observed_revision,
        evidence_refs=[report_digest],
    )
    return 0 if grade_result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
