import json
import os
import subprocess
import sys
from pathlib import Path
import time

import pytest

from omicsclaw.skill.registry import OmicsRegistry
from omicsclaw.skill.scaffolder import (
    create_skill_scaffold,
    find_latest_autonomous_analysis,
    infer_skill_name,
)
from omicsclaw.skill.schema import validate_skill_yaml


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from skill_lint import lint_skill  # noqa: E402


def test_skill_scaffolder_import_does_not_require_package_file():
    code = """
import importlib
import omicsclaw

omicsclaw.__file__ = None
scaffolder = importlib.import_module("omicsclaw.skill.scaffolder")
assert scaffolder.OMICSCLAW_DIR.name == "OmicsClaw"
assert scaffolder.SKILLS_DIR.name == "skills"
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_infer_skill_name_falls_back_to_request_tokens():
    assert infer_skill_name("Create a CellCharter spatial domains skill", "spatial") == (
        "cellcharter-spatial-domains"
    )


def test_create_skill_scaffold_creates_registry_loadable_skill(tmp_path: Path):
    result = create_skill_scaffold(
        request="Create a reusable kinase activity skill for phosphoproteomics.",
        domain="proteomics",
        skill_name="proteomics-kinase-activity",
        summary="Kinase activity inference scaffold for phosphoproteomics matrices.",
        methods=["ksea"],
        trigger_keywords=["kinase activity", "ksea"],
        skills_root=tmp_path,
    )

    skill_dir = Path(result.skill_dir)
    assert skill_dir.exists()
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "proteomics_kinase_activity.py").exists()
    assert (skill_dir / "tests" / "test_proteomics_kinase_activity.py").exists()
    assert (skill_dir / "scaffold_spec.json").exists()
    assert (skill_dir / "manifest.json").exists()
    assert (skill_dir / "completion_report.json").exists()
    # v2 layout (ADR 0037): every scaffold is BORN v2 — a skill.yaml machine
    # contract + 4 references, and NO legacy parameters.yaml sidecar (that would
    # break the 0-parameters.yaml repo invariant).  The existence checks pair
    # with lint == [] so both v2 shape AND v2 content are validated; a v2
    # skill.yaml routes `lint_skill` through the schema-validated `_lint_v2` path.
    assert (skill_dir / "skill.yaml").exists()
    assert not (skill_dir / "parameters.yaml").exists()
    assert validate_skill_yaml(skill_dir / "skill.yaml") == []
    assert (skill_dir / "references" / "methodology.md").exists()
    assert (skill_dir / "references" / "output_contract.md").exists()
    assert (skill_dir / "references" / "parameters.md").exists()
    assert (skill_dir / "references" / "r_visualization.md").exists()
    assert result.completion["status"] == "complete"
    assert result.completion["completed"] is True
    assert lint_skill(skill_dir) == []

    registry = OmicsRegistry()
    registry.load_all(tmp_path)

    info = registry.skills.get("proteomics-kinase-activity")
    assert info is not None
    assert info["domain"] == "proteomics"
    assert info["script"].name == "proteomics_kinase_activity.py"


def test_create_skill_scaffold_can_promote_autonomous_analysis(tmp_path: Path):
    output_root = tmp_path / "output"
    source_dir = output_root / "peak-detection__20260331_055254__f8024e5c"
    repro_dir = source_dir / "reproducibility"
    repro_dir.mkdir(parents=True)

    notebook = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Plan\n"]},
            {
                "cell_type": "code",
                "metadata": {},
                "outputs": [],
                "execution_count": 1,
                "source": [
                    'ANALYSIS_GOAL = "detect peaks"\n',
                    'ANALYSIS_CONTEXT = ""\n',
                    'WEB_CONTEXT = "docs"\n',
                    'INPUT_FILE = ""\n',
                    f'AUTONOMOUS_OUTPUT_DIR = "{source_dir}"\n',
                    "def _blocked(*args, **kwargs):\n    raise RuntimeError('x')\n",
                ],
            },
            {
                "cell_type": "code",
                "metadata": {},
                "outputs": [],
                "execution_count": 2,
                "source": [
                    "from pathlib import Path\n",
                    "out = Path(AUTONOMOUS_OUTPUT_DIR) / 'detected.txt'\n",
                    "out.write_text('ok', encoding='utf-8')\n",
                ],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    (repro_dir / "analysis_notebook.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    (source_dir / "analysis_plan.md").write_text("1. detect peaks\n", encoding="utf-8")
    (source_dir / "result_summary.md").write_text("# success\n", encoding="utf-8")
    (source_dir / "web_sources.md").write_text("source docs\n", encoding="utf-8")
    (source_dir / "capability_decision.json").write_text(
        json.dumps({"domain": "orchestrator"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = create_skill_scaffold(
        request="Package the successful peak detection run as a skill.",
        domain="",
        skill_name="peak-detection-skill",
        summary="Reusable peak detection skill.",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )

    skill_dir = Path(result.skill_dir)
    script_text = (skill_dir / "peak_detection_skill.py").read_text(encoding="utf-8")
    assert "Promoted OmicsClaw skill" in script_text
    assert "out = Path(AUTONOMOUS_OUTPUT_DIR) / 'detected.txt'" in script_text
    assert (skill_dir / "references" / "source_analysis_notebook.ipynb").exists()
    assert (skill_dir / "references" / "source_result_summary.md").exists()
    # v2 layout co-exists with the source_* promotion artifacts.  Same
    # existence-then-lint pairing as the default-scaffold test.
    assert (skill_dir / "skill.yaml").exists()
    assert not (skill_dir / "parameters.yaml").exists()
    assert validate_skill_yaml(skill_dir / "skill.yaml") == []
    assert (skill_dir / "references" / "methodology.md").exists()
    assert (skill_dir / "references" / "output_contract.md").exists()
    assert (skill_dir / "references" / "parameters.md").exists()
    assert (skill_dir / "references" / "r_visualization.md").exists()
    assert (skill_dir / "manifest.json").exists()
    assert (skill_dir / "completion_report.json").exists()
    assert result.completion["status"] == "complete"
    assert result.completion["completed"] is True
    assert lint_skill(skill_dir) == []


def _build_real_mini_agent_run(
    tmp_path: Path,
    *,
    accepted_cells: list[str] | None = None,
    real_input: bool = False,
    project_id: str = "",
):
    """Materialise a real Autonomous Code Mini-Agent workspace on disk.

    Uses the actual producer helpers (``create_workspace`` / ``emit_replay_script``
    / ``write_run_records``) so this test tracks the real write-side contract
    instead of a hand-rolled notebook bundle (which masked the promotion bug).
    ``real_input`` writes a tiny valid .h5ad (for execution tests); ``project_id``
    nests the run under a project dir (ADR 0035).
    """
    from omicsclaw.autonomous.budget import MiniAgentBudget
    from omicsclaw.autonomous.contracts import (
        AutonomousRunRequest,
        AutonomousRunResult,
        AutonomousRunStatus,
    )
    from omicsclaw.autonomous.replay import emit_replay_script
    from omicsclaw.autonomous.runner import write_run_records
    from omicsclaw.autonomous.workspace import create_workspace

    output_root = tmp_path / "output"
    input_file = tmp_path / "data.h5ad"
    if real_input:
        import anndata as ad
        import numpy as np

        ad.AnnData(np.zeros((5, 3), dtype="float32")).write_h5ad(input_file)
    else:
        input_file.write_text("fake", encoding="utf-8")

    request = AutonomousRunRequest(
        goal="cluster the cells and summarize markers",
        output_root=str(output_root),
        input_paths=[str(input_file)],
        project_id=project_id,
    )
    workspace = create_workspace(request)
    cells = accepted_cells if accepted_cells is not None else [
        "res = oc.run('sc-preprocessing', adata)\nadata = res.adata",
        "import scanpy as sc\nsc.tl.leiden(adata)\nReturnAnswer('2 clusters')",
    ]
    emit_replay_script(
        workspace.root,
        cells,
        [str(input_file)],
        MiniAgentBudget(),
        replay_workspace=workspace.root / "replay",
    )
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
        metadata={"answer": "2 clusters", "computed_results": "leiden -> 2 clusters"},
    )
    write_run_records(workspace, request=request, result=result)
    return output_root, workspace.root


def test_create_skill_scaffold_can_promote_mini_agent_analysis(tmp_path: Path):
    """Promote a REAL mini-agent run (no notebook; code in ``analysis.py``)."""
    output_root, source_dir = _build_real_mini_agent_run(tmp_path)

    result = create_skill_scaffold(
        request="Package the clustering run as a reusable skill.",
        domain="singlecell",
        skill_name="mini-promote-skill",
        summary="Reusable clustering skill.",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )

    skill_dir = Path(result.skill_dir)
    script_text = (skill_dir / "mini_promote_skill.py").read_text(encoding="utf-8")
    assert "Promoted OmicsClaw skill" in script_text
    # The mini-agent's accepted code (from analysis.py) made it into the draft.
    assert "sc.tl.leiden(adata)" in script_text
    assert "oc.run('sc-preprocessing', adata)" in script_text
    # Real source artifacts are copied as references (notebook absent → skipped).
    assert (skill_dir / "references" / "source_result_summary.md").exists()
    assert (skill_dir / "references" / "source_manifest.json").exists()
    assert not (skill_dir / "references" / "source_analysis_notebook.ipynb").exists()
    assert result.completion["completed"] is True
    assert lint_skill(skill_dir) == []

    # ITEM 2: the promotion path seeds deps.python from the RENDERED script's
    # real import surface — the mini-agent facade bootstrap imports anndata +
    # matplotlib, and the accepted cells import scanpy — so a promoted skill
    # under skills/ starts clean against audit_skill_requires.
    from audit_skill_requires import skill_import_surface

    from omicsclaw.skill.schema import load_skill_yaml

    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    deps = manifest.deps.python
    assert deps == sorted(set(deps)), f"deps.python must be sorted + deduped: {deps}"
    assert {"anndata", "matplotlib"}.issubset(deps), deps
    assert "python" not in deps and "sys" not in deps, f"stdlib leaked into deps: {deps}"
    # The audit's computed import surface must be a subset of the seeded deps so
    # `audit_skill_requires --check` reports nothing missing.
    core, optional, _lib, _notes = skill_import_surface(skill_dir, [manifest.runtime.entry])
    assert set(core) | set(optional) <= set(deps), (core, optional, deps)


def test_find_latest_autonomous_analysis_discovers_mini_agent_run(tmp_path: Path):
    """``promote_from_latest`` must discover a real mini-agent run."""
    output_root, source_dir = _build_real_mini_agent_run(tmp_path)
    latest = find_latest_autonomous_analysis(output_root=output_root)
    assert latest is not None
    assert latest.resolve() == source_dir.resolve()


def test_promoted_mini_agent_skill_runs(tmp_path: Path):
    """The promoted mini-agent skill must actually RUN (no NameError: 'oc').

    Executes the generated script end-to-end against a tiny real .h5ad with
    facade-only accepted code, so it exercises the bootstrap, not just file text.
    """
    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=["n = int(adata.n_obs)", "show()\nReturnAnswer('cells=' + str(n))"],
    )
    result = create_skill_scaffold(
        request="Package the run as a reusable skill.",
        domain="singlecell",
        skill_name="run-promote-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )
    script = Path(result.skill_dir) / "run_promote_skill.py"
    run_out = tmp_path / "run_out"
    proc = subprocess.run(
        [sys.executable, str(script), "--demo", "--output", str(run_out)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "NameError" not in proc.stderr
    assert (run_out / "result.json").exists()
    assert (run_out / "answer.txt").read_text(encoding="utf-8").strip() == "cells=5"


def test_find_latest_discovers_project_nested_mini_agent_run(tmp_path: Path):
    """ADR 0035: a run nested under a project dir must still be discoverable."""
    output_root, source_dir = _build_real_mini_agent_run(tmp_path, project_id="thread-xyz")
    assert source_dir.parent != output_root  # sanity: really nested under a project
    latest = find_latest_autonomous_analysis(output_root=output_root)
    assert latest is not None
    assert latest.resolve() == source_dir.resolve()


def test_find_latest_ignores_non_autonomous_dir(tmp_path: Path):
    """A non-autonomous dir with result_summary.md + analysis.py is not promoted."""
    output_root = tmp_path / "output"
    decoy = output_root / "some-skill-output"
    decoy.mkdir(parents=True)
    (decoy / "result_summary.md").write_text("summary\n", encoding="utf-8")
    (decoy / "analysis.py").write_text("print('x')\n", encoding="utf-8")
    assert find_latest_autonomous_analysis(output_root=output_root) is None


def test_create_skill_scaffold_rejects_incomplete_autonomous_analysis(tmp_path: Path):
    source_dir = tmp_path / "output" / "incomplete-analysis"
    repro_dir = source_dir / "reproducibility"
    repro_dir.mkdir(parents=True)
    (repro_dir / "analysis_notebook.ipynb").write_text("{}", encoding="utf-8")
    (source_dir / "analysis_plan.md").write_text("plan\n", encoding="utf-8")
    (source_dir / "result_summary.md").write_text("summary\n", encoding="utf-8")
    (source_dir / "completion_report.json").write_text(
        json.dumps({"completed": False, "status": "failed"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not promotable"):
        create_skill_scaffold(
            request="Promote the failed analysis.",
            domain="orchestrator",
            skill_name="failed-analysis-skill",
            skills_root=tmp_path / "skills",
            source_analysis_dir=source_dir,
        )


def test_find_latest_autonomous_analysis_returns_newest(tmp_path: Path):
    output_root = tmp_path / "output"
    older = output_root / "older"
    newer = output_root / "newer"
    for path in (older, newer):
        (path / "reproducibility").mkdir(parents=True)
        (path / "reproducibility" / "analysis_notebook.ipynb").write_text("{}", encoding="utf-8")
        (path / "analysis_plan.md").write_text("plan\n", encoding="utf-8")
        (path / "result_summary.md").write_text("summary\n", encoding="utf-8")

    future_ts = time.time() + 60
    for target in (
        newer,
        newer / "reproducibility" / "analysis_notebook.ipynb",
        newer / "analysis_plan.md",
        newer / "result_summary.md",
    ):
        os.utime(target, (future_ts, future_ts))
    latest = find_latest_autonomous_analysis(output_root=output_root)
    assert latest == newer


def test_find_latest_autonomous_analysis_skips_incomplete_completion_reports(tmp_path: Path):
    output_root = tmp_path / "output"
    incomplete = output_root / "incomplete"
    complete = output_root / "complete"
    for path in (incomplete, complete):
        (path / "reproducibility").mkdir(parents=True)
        (path / "reproducibility" / "analysis_notebook.ipynb").write_text("{}", encoding="utf-8")
        (path / "analysis_plan.md").write_text("plan\n", encoding="utf-8")
        (path / "result_summary.md").write_text("summary\n", encoding="utf-8")
    (incomplete / "completion_report.json").write_text(
        json.dumps({"completed": False, "status": "failed"}),
        encoding="utf-8",
    )
    (complete / "completion_report.json").write_text(
        json.dumps({"completed": True, "status": "complete"}),
        encoding="utf-8",
    )

    assert find_latest_autonomous_analysis(output_root=output_root) == complete
