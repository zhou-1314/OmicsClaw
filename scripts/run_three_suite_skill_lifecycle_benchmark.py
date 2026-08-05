#!/usr/bin/env python3
"""Run the fixed three-suite per-Skill conformance pilot.

This is deliberately not an Agent Campaign. It executes the currently declared
OmicBench A02/A03, scAgentBench PAGA, and BiomniBench-DA 12-2 protocols through
``SkillEvolutionGovernance.evaluate`` and retains their bounded evidence stores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from omicsclaw.skill.evaluation_run import (  # noqa: E402
    EvaluationArtifactStore,
    EvaluationResultStore,
)
from omicsclaw.skill.evolution import (  # noqa: E402
    EvolutionProposalStore,
    SkillHealthLedger,
)
from omicsclaw.skill.evolution_governance import (  # noqa: E402
    SkillEvolutionGovernance,
)


@dataclass(frozen=True, slots=True)
class _Suite:
    suite_id: str
    output_name: str
    skill_id: str
    suite_case_count: int
    expected_protocol_ids: tuple[str, ...]
    case_ids: Callable[[Path], list[str]]


def _omicbench_cases(root: Path) -> list[str]:
    return sorted(
        path.name
        for path in (root / "data/benchmarks/omicbench").glob("omicbench-*")
        if path.is_dir()
    )


def _scagentbench_cases(root: Path) -> list[str]:
    path = (
        root
        / "data/benchmarks/scagent-bench/datasets_for_bioagent_benchmark"
        / "input_prompt/main/prompt_gradient.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("scAgentBench main prompt inventory must be an object")
    return sorted(str(case_id) for case_id in value)


def _biomnibench_cases(root: Path) -> list[str]:
    return sorted(
        path.name
        for path in (root / "data/benchmarks/biomnibench-da").glob("da-*")
        if path.is_dir()
    )


_SUITES = (
    _Suite(
        suite_id="omicbench",
        output_name="omicbench",
        skill_id="sc-preprocessing",
        suite_case_count=44,
        expected_protocol_ids=(
            "omicbench-a02-normalize-log-v1",
            "omicbench-a03-hvg-v1",
        ),
        case_ids=_omicbench_cases,
    ),
    _Suite(
        suite_id="scagentbench-main",
        output_name="scagentbench-paga",
        skill_id="sc-pseudotime",
        suite_case_count=50,
        expected_protocol_ids=("scagentbench-paga-main-v1",),
        case_ids=_scagentbench_cases,
    ),
    _Suite(
        suite_id="biomnibench-da-public",
        output_name="biomnibench-da12-2",
        skill_id="bulkrna-enrichment",
        suite_case_count=50,
        expected_protocol_ids=(
            "biomnibench-da12-2-deterministic-preflight-v1",
        ),
        case_ids=_biomnibench_cases,
    ),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _case_list_digest(case_ids: Sequence[str]) -> str:
    payload = "".join(f"{case_id}\n" for case_id in sorted(case_ids)).encode()
    return _sha256_bytes(payload)


def _artifact_bundle_ref(evidence_refs: Sequence[str]) -> str:
    refs = [
        reference
        for reference in evidence_refs
        if reference.startswith("evaluation-artifact:sha256:")
    ]
    if len(refs) != 1:
        raise RuntimeError("protocol result must bind exactly one artifact bundle")
    return refs[0]


def _run_suite(repository_root: Path, output_root: Path, suite: _Suite) -> dict[str, Any]:
    case_ids = suite.case_ids(repository_root)
    if len(case_ids) != suite.suite_case_count or len(case_ids) != len(set(case_ids)):
        raise RuntimeError(f"{suite.suite_id} case inventory does not match its denominator")

    suite_root = output_root / suite.output_name
    suite_root.mkdir(parents=True)
    result_store = EvaluationResultStore(suite_root / "evaluations.jsonl")
    artifact_store = EvaluationArtifactStore(suite_root / "artifacts")
    governance = SkillEvolutionGovernance(
        skills_root=repository_root / "skills",
        ledger=SkillHealthLedger(suite_root / "events.jsonl"),
        proposals=EvolutionProposalStore(suite_root / "proposals.jsonl"),
        evaluation_store=result_store,
        evaluation_artifact_store=artifact_store,
    )
    results = governance.evaluate(suite.skill_id)
    protocol_ids = tuple(sorted(result.protocol_id for result in results))
    expected = tuple(sorted(suite.expected_protocol_ids))
    if protocol_ids != expected:
        raise RuntimeError(
            f"{suite.skill_id} protocol set changed: expected {expected}, got {protocol_ids}"
        )
    experience = governance.experience_view(suite.skill_id)
    if experience is None:
        raise RuntimeError(f"missing Experience View for {suite.skill_id}")
    revision = experience["skill_revision"]

    result_rows: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda item: item.protocol_id):
        bundle_ref = _artifact_bundle_ref(result.evidence_refs)
        bundle = artifact_store.read_json(bundle_ref)
        result_rows.append(
            {
                "artifact_bundle_ref": bundle_ref,
                "artifact_count": len(bundle.get("artifacts", [])),
                "dataset_digest": result.dataset_digest,
                "duration_seconds": result.duration_seconds,
                "environment_id": result.environment_id,
                "evaluation_id": result.evaluation_id,
                "kind": result.kind,
                "metrics": dict(result.metrics),
                "occurred_at": result.occurred_at,
                "outcome": result.outcome,
                "protocol_digest": result.protocol_digest,
                "protocol_id": result.protocol_id,
                "reason_code": result.reason_code,
                "result_id": result.result_id,
                "skill_revision": revision,
                "unresolved_evidence_refs": list(
                    bundle.get("unresolved_evidence_refs", [])
                ),
            }
        )

    return {
        "case_list_sha256": _case_list_digest(case_ids),
        "directory": suite.output_name,
        "evaluation_store_sha256": _sha256_file(result_store.path),
        "executed_case_count": len(results),
        "experience": {
            key: experience[key]
            for key in (
                "declared_validation_level",
                "evidence_supported_validation_level",
                "effective_validation_level",
                "validation_state",
            )
        },
        "results": result_rows,
        "suite_case_count": suite.suite_case_count,
        "suite_id": suite.suite_id,
    }


def _file_inventory(output_root: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(output_root.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        payload = path.read_bytes()
        inventory.append(
            {
                "path": path.relative_to(output_root).as_posix(),
                "sha256": _sha256_bytes(payload),
                "size_bytes": len(payload),
            }
        )
    return inventory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fixed three-suite per-Skill lifecycle conformance pilot."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Fresh output directory for evaluation stores and MANIFEST.json",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository_root = args.repository_root.resolve()
    output_root = args.output.resolve()
    if output_root.exists():
        raise FileExistsError(f"benchmark output already exists: {output_root}")
    output_root.mkdir(parents=True)

    suites = [_run_suite(repository_root, output_root, suite) for suite in _SUITES]
    manifest = {
        "artifact_root": str(output_root.relative_to(repository_root)),
        "files": _file_inventory(output_root),
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "limitations": {
            "agent_campaign_run": False,
            "biomnibench_official_llm_judge": {
                "reason": (
                    "judge credentials unavailable; deterministic preflight only"
                ),
                "run": False,
                "score": None,
            },
            "coverage": {
                "biomnibench_da_public": "1/50",
                "omicbench": "2/44",
                "scagentbench_main": "1/50",
            },
            "evidence_plane": "per-skill-conformance",
            "full_suite_run": False,
            "suite_native_scores_pooled": False,
        },
        "operation": "governed-per-skill-conformance-rerun",
        "schema_version": 1,
        "scientific_logic_rerun": True,
        "suites": suites,
    }
    manifest_path = output_root / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"manifest={manifest_path}")
    print(f"manifest_sha256={_sha256_file(manifest_path)}")
    print(f"files={len(manifest['files'])}")
    print(f"results={sum(len(suite['results']) for suite in suites)}")
    return 0 if all(
        result["outcome"] == "succeeded"
        for suite in suites
        for result in suite["results"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
