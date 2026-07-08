import json
import os
import subprocess
import sys
from pathlib import Path
import time

import pytest

from omicsclaw.skill.registry import OmicsRegistry
from omicsclaw.skill.scaffolder import (
    _demo_gate_skip_reason,
    _strip_redundant_pathlib_import,
    create_skill_scaffold,
    find_latest_autonomous_analysis,
    infer_skill_name,
)
from omicsclaw.skill.schema import load_skill_yaml, validate_skill_yaml
from omicsclaw.common.report import SCAFFOLD_STATUS, validate_result_envelope


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


# --- P1 --demo smoke gate: skip-vs-reject classification --------------------


def test_demo_gate_skip_reason_recognizes_missing_dependency():
    traceback_text = "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'x'\n"
    assert _demo_gate_skip_reason(traceback_text) is not None


def test_demo_gate_skip_reason_recognizes_stale_demo_input():
    """MF6: a promoted skill's original input can go stale between the source
    run and promotion (its autonomous-analysis workspace may already be
    cleaned up) — that is a missing-input environment limitation, not a bug
    in the promoted code."""
    traceback_text = (
        "Traceback (most recent call last):\n  File x\n"
        "FileNotFoundError: [Errno 2] No such file or directory: 'x'\n"
    )
    assert _demo_gate_skip_reason(traceback_text) is not None


def test_demo_gate_skip_reason_rejects_unrelated_message_mentioning_importerror():
    """A genuine bug whose OWN message happens to contain the word
    "ImportError" must not be misclassified as an environment limitation —
    only the actual exception TYPE at the start of a traceback line counts
    (an anchored regex, not a bare substring match), otherwise a broken
    promoted skill could slip into the catalog as merely `skipped`."""
    traceback_text = "Traceback (most recent call last):\n  File x\nRuntimeError: ImportError: synthetic hard bug\n"
    assert _demo_gate_skip_reason(traceback_text) is None


# --- _strip_redundant_pathlib_import: AST-based, not a blind text pass ------


def test_strip_redundant_pathlib_import_preserves_string_literals():
    """A regex/text pass would also match this text inside a string literal;
    the AST-based implementation must not corrupt it."""
    code = "msg = 'remember: from pathlib import Path'\nprint(msg)\n"
    assert _strip_redundant_pathlib_import(code) == code


def test_strip_redundant_pathlib_import_drops_real_import():
    code = "from pathlib import Path\nout = Path('x')\n"
    stripped = _strip_redundant_pathlib_import(code)
    assert "from pathlib import Path" not in stripped
    assert "out = Path('x')" in stripped


def test_strip_redundant_pathlib_import_leaves_multi_name_import_alone():
    """`from pathlib import Path, PurePath` must be left untouched — dropping
    the whole line would also remove `PurePath`, which the code may need."""
    code = "from pathlib import Path, PurePath\nout = Path('x')\n"
    assert _strip_redundant_pathlib_import(code) == code


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

    # Acquisition P0 contract: a fresh scaffold is BORN unproven — `draft`
    # persists under to_yaml(exclude_defaults) since it isn't the schema
    # default (`mvp`), and origin records how the skill came to exist.
    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    assert manifest.lifecycle.status == "draft"
    assert manifest.provenance.origin == "scaffolded"

    registry = OmicsRegistry()
    registry.load_all(tmp_path)

    info = registry.skills.get("proteomics-kinase-activity")
    assert info is not None
    assert info["domain"] == "proteomics"
    assert info["script"].name == "proteomics_kinase_activity.py"


def test_scaffold_placeholder_script_runs_and_signals_unimplemented(tmp_path: Path):
    """The placeholder script must actually run --demo and its result.json must
    validate against the envelope contract while still signalling `scaffold`
    (unimplemented) — this is the only signal the P1 --demo smoke gate has to
    tell a fresh placeholder apart from a real/promoted body."""
    result = create_skill_scaffold(
        request="Detect spatial domains for a new demo assay.",
        domain="spatial",
        skill_name="placeholder-check-skill",
        skills_root=tmp_path,
    )
    script = Path(result.skill_dir) / "placeholder_check_skill.py"
    run_out = tmp_path / "run_out"
    proc = subprocess.run(
        [sys.executable, str(script), "--demo", "--output", str(run_out)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    envelope = json.loads((run_out / "result.json").read_text(encoding="utf-8"))
    assert validate_result_envelope(envelope) == []
    assert envelope["status"] == SCAFFOLD_STATUS


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
        # `if adata is not None` guards the same way `SkillHandleResult.__bool__`
        # invites (`if res: ...`): sc-preprocessing's own preflight can block
        # pending a confirmation no headless replay can give, leaving
        # `res.adata` None. A real accepted-and-shipped mini-agent cell
        # checks before consuming a nested oc.run() result; this fixture now
        # matches that so the promoted script is actually demo-runnable.
        "import scanpy as sc\nif adata is not None:\n    sc.tl.leiden(adata)\nReturnAnswer('2 clusters')",
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
    """Promote a REAL mini-agent run (no notebook; code in ``analysis.py``).

    ``real_input=True``: the P1 --demo smoke gate now actually executes the
    promoted script during ``create_skill_scaffold``, so its input must be a
    loadable .h5ad — a real promotion's input is always the original run's
    genuinely-successful file, this fixture just mirrors that. The default
    cells' ``oc.run('sc-preprocessing', adata)`` call can't actually complete
    standalone (that skill's own preflight guard blocks pending interactive
    confirmation no headless replay can give), which is why the second cell
    guards with ``if adata is not None`` before consuming the result — real
    accepted-and-shipped mini-agent code checks a nested call's outcome the
    same way (``SkillHandleResult.__bool__``) rather than assuming success.
    """
    output_root, source_dir = _build_real_mini_agent_run(tmp_path, real_input=True)

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

    # Acquisition P0 contract: a promoted body that ran to completion earns a
    # real `ok` status through the shared write_result_json/mark_result_status
    # helpers, not the old ad-hoc "promoted_from_autonomous_analysis" string
    # (which validate_result_envelope would have rejected as off-taxonomy).
    envelope = json.loads((run_out / "result.json").read_text(encoding="utf-8"))
    assert validate_result_envelope(envelope) == []
    assert envelope["status"] == "ok"


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
