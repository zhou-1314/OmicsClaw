from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from omicsclaw.autonomous.budget import MiniAgentBudget
from omicsclaw.autonomous.contracts import (
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
)
from omicsclaw.autonomous.replay import emit_replay_script
from omicsclaw.autonomous.runner import write_run_records
from omicsclaw.autonomous.workspace import create_workspace
from omicsclaw.skill.authoring import SkillAuthoringRequest, author_skill
from omicsclaw.skill.schema import load_skill_yaml
from omicsclaw.skill.evaluation_run import (
    EvaluationArtifactStore,
    EvaluationResultStore,
)
from omicsclaw.skill.evaluation_dataset import digest_dataset_tree
from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance
from omicsclaw.skill.registry import SKILLS_DIR, registry
from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs


def _successful_autonomous_csv_run(tmp_path: Path):
    output_root = tmp_path / "output"
    input_path = tmp_path / "counts.csv"
    input_path.write_text("gene,S1,S2\nA,1,2\nB,3,4\n", encoding="utf-8")
    run_request = AutonomousRunRequest(
        goal="summarize a tiny transcript count matrix",
        output_root=output_root,
        input_paths=[input_path],
    )
    workspace = create_workspace(run_request)
    emit_replay_script(
        workspace.root,
        [
            "from pathlib import Path\n"
            "import json\n"
            "import pandas as pd\n"
            f"INPUT_CSV = {str(input_path)!r}\n"
            "frame = pd.read_csv(INPUT_CSV)\n"
            "out_dir = Path.cwd()\n"
            "frame.describe().to_csv(out_dir / 'summary.csv')\n"
            "(out_dir / 'report.md').write_text('source report\\n', encoding='utf-8')\n"
            "(out_dir / 'semantic_summary.json').write_text(\n"
            "    json.dumps({'gene_count': 2, 'genes': ['A', 'B']}), encoding='utf-8'\n"
            ")\n"
            "ReturnAnswer('summarized transcript counts')"
        ],
        [str(input_path)],
        MiniAgentBudget(),
        replay_workspace=workspace.root / "replay",
    )
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
        metadata={
            "answer": "summarized transcript counts",
            "computed_results": "two genes summarized",
        },
    )
    write_run_records(workspace, request=run_request, result=result)
    return output_root, workspace


def test_run_authoring_resolves_claimed_run_without_public_source_path(
    tmp_path: Path,
) -> None:
    output_root, workspace = _successful_autonomous_csv_run(tmp_path)
    request = SkillAuthoringRequest.from_mapping(
        {
            "request": "Create a reusable bulk RNA count summary skill.",
            "domain": "bulkrna",
            "skill_name": "bulkrna-run-derived-summary",
            "source": {"kind": "run", "run_id": workspace.run_id},
            "create_tests": False,
            "compute_resources": {
                "cpu_cores": 1,
                "memory_mib": 256,
                "gpu_devices": 0,
                "threads": 1,
                "temporary_disk_mib": 16,
            },
        }
    )

    result = author_skill(
        request,
        output_root=output_root,
        skills_root=tmp_path / "skills",
    )

    assert result.demo_gate_verdict == "earned"
    skill_dir = Path(result.skill_dir)
    assert skill_dir == (
        tmp_path
        / "skills"
        / "bulkrna"
        / "run-derived"
        / "bulkrna-run-derived-summary"
    )
    spec = json.loads((skill_dir / "scaffold_spec.json").read_text(encoding="utf-8"))
    assert spec["source"] == {"kind": "run", "run_id": workspace.run_id}
    assert "source_analysis_dir" not in spec
    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    assert manifest.provenance.source_ref == f"run:{workspace.run_id}"
    assert manifest.resources.compute is not None
    assert manifest.resources.compute.memory_mib == 256
    assert manifest.deps.python == ["pandas"]
    assert manifest.validation.protocols[0].runner == "shared_runner"
    assert manifest.validation.protocols[0].repeats == 2
    bundled_input = skill_dir / "data" / "demo_input.csv"
    assert manifest.validation.protocols[0].dataset_ref is not None
    assert manifest.validation.protocols[0].dataset_ref.path == (
        "skills/bulkrna/run-derived/bulkrna-run-derived-summary/"
        "data/demo_input.csv"
    )
    assert manifest.validation.protocols[0].dataset_ref.content_sha256 == (
        digest_dataset_tree(bundled_input)
    )
    assert bundled_input.read_text(encoding="utf-8") == "gene,S1,S2\nA,1,2\nB,3,4\n"
    script = (skill_dir / "bulkrna_run_derived_summary.py").read_text(encoding="utf-8")
    assert str(tmp_path) not in script
    assert 'Path(__file__).resolve().parent / "data" / "demo_input.csv"' in script

    run_output = tmp_path / "promoted-output"
    subprocess.run(
        [sys.executable, str(skill_dir / "bulkrna_run_derived_summary.py"), "--demo", "--output", str(run_output)],
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        cwd=skill_dir,
    )
    envelope = json.loads((run_output / "result.json").read_text(encoding="utf-8"))
    assert envelope["data"]["semantic_summary"] == {
        "gene_count": 2,
        "genes": ["A", "B"],
    }
    assert not (run_output / "README.md").exists()
    assert (run_output / "report.md").read_text(encoding="utf-8") == "source report\n"
    assert not (skill_dir / "summary.csv").exists()
    assert not (skill_dir / "semantic_summary.json").exists()


def test_agent_skill_authoring_schema_exposes_run_id_not_workspace_path() -> None:
    spec = next(
        item
        for item in build_bot_tool_specs(BotToolContext(skill_names=()))
        if item.name == "create_omics_skill"
    )

    properties = spec.parameters["properties"]
    assert "source_analysis_dir" not in properties
    assert properties["source"]["properties"]["kind"]["enum"] == ["intent", "run"]
    assert properties["source"]["properties"]["run_id"]["pattern"] == "^[0-9a-f]{32}$"
    assert "source" in spec.parameters["required"]


@pytest.mark.asyncio
async def test_agent_skill_authoring_rejects_workspace_path_injection() -> None:
    from omicsclaw.runtime.agent import state as _state  # noqa: F401
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_create_omics_skill,
    )

    result = await execute_create_omics_skill(
        {
            "request": "Create a reusable bulk RNA skill.",
            "domain": "bulkrna",
            "source": {"kind": "intent"},
            "source_analysis_dir": "/tmp/attacker-selected-run",
        }
    )

    assert result.startswith("Error creating OmicsClaw skill:")
    assert "unknown fields" in result
    assert "source_analysis_dir" in result


def test_real_shared_runner_evaluation_activates_run_derived_skill(
    tmp_path: Path,
) -> None:
    output_root, workspace = _successful_autonomous_csv_run(tmp_path)
    skills_root = tmp_path / "skills"
    skill_id = "bulkrna-run-derived-evaluation"
    authored = author_skill(
        SkillAuthoringRequest.from_mapping(
            {
                "request": "Create a reusable bulk RNA count summary skill.",
                "domain": "bulkrna",
                "skill_name": skill_id,
                "source": {"kind": "run", "run_id": workspace.run_id},
                "create_tests": False,
                "compute_resources": {
                    "cpu_cores": 1,
                    "memory_mib": 256,
                    "gpu_devices": 0,
                    "threads": 1,
                    "temporary_disk_mib": 16,
                },
            }
        ),
        output_root=output_root,
        skills_root=skills_root,
    )
    assert authored.demo_gate_verdict == "earned"

    results_path = tmp_path / "evaluations.jsonl"
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        evaluation_store=EvaluationResultStore(results_path),
        evaluation_artifact_store=EvaluationArtifactStore(tmp_path / "artifacts"),
    )
    try:
        registry.reload(skills_root)
        proposal = governance.prepare_activation(skill_id)
        assert proposal is not None
        assert proposal.status == "pending"
        assert len(proposal.support_evaluation_result_ids) == 2

        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="reviewed exact source Run and two real shared-runner outputs",
        )
        activated = load_skill_yaml(Path(authored.skill_dir) / "skill.yaml")
        assert activated.lifecycle.status == "mvp"
        assert activated.validation.level == "demo-validated"
        view = governance.experience_view(skill_id)
        assert view is not None
        assert view["validation_state"] == "current"
        assert view["stability"]["demo"]["runs"] == 2
        assert view["stability"]["demo"]["success_rate"] == 1.0
    finally:
        registry.reload(SKILLS_DIR)
