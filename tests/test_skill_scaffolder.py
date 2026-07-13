import json
import os
import subprocess
import sys
from pathlib import Path
import time

import pytest

from omicsclaw.skill.registry import OmicsRegistry
from omicsclaw.skill.scaffolder import (
    AutonomousAnalysisBundle,
    _demo_gate_skip_reason,
    _load_autonomous_bundle,
    _strip_redundant_pathlib_import,
    _synthesize_load_when,
    build_acquisition_abstraction,
    create_skill_scaffold,
    find_latest_autonomous_analysis,
    infer_skill_name,
)
from omicsclaw.skill.schema import load_skill_yaml, validate_skill_yaml
from omicsclaw.skill.execution.flag_introspection import derive_accepted_flags
from omicsclaw.skill.lazy_metadata import LazySkillMetadata
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
    in the promoted code. Only recognized when the FileNotFoundError names
    the SPECIFIC known original input path (threaded in as
    ``original_input_file``) — a bare exception-type match is not enough
    (see the rejection test below)."""
    traceback_text = (
        "Traceback (most recent call last):\n  File x\n"
        "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/original-input.h5ad'\n"
    )
    assert _demo_gate_skip_reason(traceback_text, original_input_file="/tmp/original-input.h5ad") is not None


def test_demo_gate_skip_reason_rejects_file_not_found_for_an_unrelated_path():
    """A FileNotFoundError NOT referencing the known original input path is a
    real bug in the promoted body (e.g. a typo'd internal path) — must be
    rejected, not silently tolerated as an environment limitation. This is
    the fix for the pre-existing over-broad classification that let any
    FileNotFoundError, regardless of cause, slip a broken promoted skill
    into the catalog as merely `skipped`."""
    traceback_text = (
        "Traceback (most recent call last):\n  File x\n"
        "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/some/other/typo.csv'\n"
    )
    assert (
        _demo_gate_skip_reason(traceback_text, original_input_file="/tmp/original-input.h5ad")
        is None
    )
    # No known original input at all (a plain scaffold/corpus skill) — any
    # FileNotFoundError is unconditionally rejected.
    assert _demo_gate_skip_reason(traceback_text) is None


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


# --- P3 task-derived load_when synthesis ------------------------------------


def test_load_when_prefers_summary_over_request():
    result = _synthesize_load_when(
        "proteomics",
        "x",
        "Create a reusable kinase activity skill for phosphoproteomics.",
        "Kinase activity inference scaffold for phosphoproteomics matrices.",
    )
    assert result == "the user needs Kinase activity inference scaffold for phosphoproteomics matrices"


def test_load_when_falls_back_to_request_when_summary_empty():
    """Regression guard: an earlier version of this synthesizer tried to
    strip a leading creation VERB from `request` via a finite whitelist, and
    broke on exactly this string (`detect` isn't a "create a skill" verb, and
    no finite whitelist covers every bio-analysis verb) — the fixed design
    uses a verb-agnostic "needs to <sentence>" template instead."""
    result = _synthesize_load_when("spatial", "x", "Detect spatial domains for a new demo assay.", "")
    assert result == "the user needs to Detect spatial domains for a new demo assay"


def test_load_when_handles_a_second_non_creation_verb_request():
    result = _synthesize_load_when("orchestrator", "x", "Promote the failed analysis.", "")
    assert result == "the user needs to Promote the failed analysis"


def test_load_when_strips_leading_article_from_bare_noun_request():
    result = _synthesize_load_when("spatial", "x", "A spatial domain detection skill", "")
    assert result == "the user needs spatial domain detection"


def test_load_when_strips_trailing_skill_suffix():
    result = _synthesize_load_when("spatial", "cellcharter-spatial-domains", "Create a CellCharter spatial domains skill", "")
    assert result == "the user needs to Create a CellCharter spatial domains"


def test_load_when_truncates_to_first_sentence():
    result = _synthesize_load_when(
        "singlecell", "x", "Cluster the cells by cell type. It should also compute markers.", ""
    )
    assert result == "the user needs to Cluster the cells by cell type"


def test_load_when_rejects_a_question_and_falls_back_to_generic():
    """Gluing a question after "needs to" would read WORSE than the generic
    template ("the user needs to Can you build me a peak-calling skill for
    ChIP-seq") — reject it outright rather than salvage it."""
    result = _synthesize_load_when("genomics", "x", "Can you build me a peak-calling skill for ChIP-seq?", "")
    assert result == "the user explicitly asks to create a new genomics skill named 'x'"


def test_load_when_rejects_a_question_separated_by_a_tab():
    """Regression: an earlier version extracted the first word via
    `sentence.split(" ", 1)` — a literal-space split — so a tab between
    "Can" and "you" slipped past the reject guard entirely."""
    result = _synthesize_load_when("genomics", "x", "Can\tyou build me a peak-calling skill for ChIP-seq?", "")
    assert result == "the user explicitly asks to create a new genomics skill named 'x'"


def test_load_when_rejects_a_contraction_starting_with_a_rejected_word():
    """Regression: "I'd"/"I've"/etc. keep the apostrophe INSIDE the word, so
    a plain `.strip("'\\\"")` (which only trims leading/trailing quote
    characters) never reduced "i'd" to "i"."""
    result = _synthesize_load_when("genomics", "x", "I'd like a peak-calling skill for ChIP-seq.", "")
    assert result == "the user explicitly asks to create a new genomics skill named 'x'"


def test_load_when_rejects_additional_question_openers():
    """Regression: when/where/which/who are just as much question openers as
    can/could/would/... but were missing from the reject set."""
    for request in (
        "When should I use peak calling?",
        "Where can I find a spatial domain skill?",
        "Which method detects doublets best?",
        "Who reviews promoted skills?",
    ):
        result = _synthesize_load_when("genomics", "x", request, "")
        assert result == "the user explicitly asks to create a new genomics skill named 'x'", request


def test_load_when_rejects_a_relative_clause_request():
    result = _synthesize_load_when(
        "singlecell", "x", "I need a skill that clusters cells and annotates types.", ""
    )
    assert result == "the user explicitly asks to create a new singlecell skill named 'x'"


def test_load_when_falls_back_to_generic_when_both_empty():
    """Pins the previously-untested cyclical placeholder as a regression
    guard for the truly-no-info case (preserves prior behavior)."""
    result = _synthesize_load_when("bulkrna", "my-skill", "", "")
    assert result == "the user explicitly asks to create a new bulkrna skill named 'my-skill'"


def test_load_when_reverts_when_stripping_a_short_summary_leaves_too_little():
    """"QC skill." stripped down to "QC" loses too much signal to be useful —
    revert to the pre-strip sentence instead."""
    result = _synthesize_load_when("singlecell", "x", "", "QC skill.")
    assert result == "the user needs QC skill"


def test_load_when_caps_pathological_input_length():
    long_request = "Analyze " + " ".join(f"gene{i}" for i in range(50)) + " expression patterns."
    result = _synthesize_load_when("bulkrna", "x", long_request, "")
    assert len(result.split()) <= 24  # "the user needs to" (4 words) + 20-word topic cap


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

    # P3: the real description consumers (catalog/SKILL.md, via LazySkillMetadata
    # -> _reconstruct_description) must reflect the actual capability, not the
    # old cyclical "the user explicitly asks to create a new X skill" text.
    description = LazySkillMetadata(skill_dir).description
    assert "Kinase activity inference" in description
    assert "explicitly asks to create a new" not in description


def test_scaffold_load_when_survives_a_colon_in_the_request(tmp_path: Path):
    """Round-trip through the written skill.yaml (not just the in-memory
    manifest) to catch YAML-escaping bugs a colon in free text could trigger
    (`key: value with: colon` is a classic YAML footgun)."""
    result = create_skill_scaffold(
        request="Create a skill for peak-calling: MACS2 style.",
        domain="genomics",
        skill_name="macs2-peak-calling",
        skills_root=tmp_path,
    )
    skill_dir = Path(result.skill_dir)
    assert validate_skill_yaml(skill_dir / "skill.yaml") == []

    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    assert "peak-calling: MACS2 style" in manifest.summary.load_when

    description = LazySkillMetadata(skill_dir).description
    assert "peak-calling: MACS2 style" in description


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


def test_mini_agent_promotion_bundle_loads_structured_steps_and_skill_calls(tmp_path: Path):
    """P2 generalisation must consume the producer's structured trace instead
    of reverse-engineering every semantic decision from flattened Python.

    ``metadata.steps`` records accepted-cell intent; the append-only JSONL is
    authoritative for executed skill calls and must win over a stale manifest
    copy of the same field.
    """
    _output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        accepted_cells=["ReturnAnswer('done')"],
    )
    manifest_path = source_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"]["steps"] = [
        {"index": 1, "purpose": "preprocess cells", "new_variables": ["qc"]}
    ]
    manifest["metadata"]["skill_calls"] = [{"skill": "stale-manifest-copy"}]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (source_dir / "skill_calls.jsonl").write_text(
        json.dumps(
            {
                "index": 1,
                "skill": "sc-preprocessing",
                "params": {"min_genes": "200"},
                "flags": ["--min-genes", "200"],
                "input_artifact": str(tmp_path / "data.h5ad"),
                "output_dir": str(source_dir / "skill_calls" / "01_sc-preprocessing" / "out"),
                "primary_artifact": str(source_dir / "skill_calls" / "01_sc-preprocessing" / "out" / "processed.h5ad"),
                "status": "succeeded",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    bundle = _load_autonomous_bundle(source_dir)

    assert bundle.steps == manifest["metadata"]["steps"]
    assert [call["skill"] for call in bundle.skill_calls] == ["sc-preprocessing"]
    assert bundle.skill_calls[0]["params"] == {"min_genes": "200"}


def test_promotion_uses_facade_free_structured_abstraction_when_semantics_are_provable(
    tmp_path: Path, monkeypatch
):
    """A call-only mini-agent trace is a workflow, not arbitrary Python.

    Promote it through the stable runner API and retain exact source evidence;
    do not ship the temporary in-kernel facade as a production dependency.
    """
    import omicsclaw.skill.scaffolder as scaffolder_module

    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=[
            "result = oc.run('sc-preprocessing', adata, min_genes=200)\n"
            "adata = result.adata\n"
            "ReturnAnswer('done')"
        ],
    )
    (source_dir / "skill_calls.jsonl").write_text(
        json.dumps(
            {
                "index": 1,
                "skill": "sc-preprocessing",
                "method": None,
                "params": {"min_genes": "200"},
                "flags": ["--min-genes", "200"],
                "input_artifact": str(source_dir / "skill_calls" / "01_sc-preprocessing" / "input.h5ad"),
                "output_dir": str(source_dir / "skill_calls" / "01_sc-preprocessing" / "out"),
                "primary_artifact": str(source_dir / "skill_calls" / "01_sc-preprocessing" / "out" / "processed.h5ad"),
                "status": "succeeded",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scaffolder_module,
        "_run_demo_smoke_gate",
        lambda *args, **kwargs: scaffolder_module._DemoGateOutcome(
            verdict="earned", reason="synthetic structured abstraction gate"
        ),
    )

    result = create_skill_scaffold(
        request="Package the preprocessing workflow as a reusable skill.",
        domain="singlecell",
        skill_name="structured-promote-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )

    skill_dir = Path(result.skill_dir)
    script_text = (skill_dir / "structured_promote_skill.py").read_text(encoding="utf-8")
    assert "from omicsclaw.skill.runner import run_skill" in script_text
    assert "--min-genes" in script_text
    assert "build_facade" not in script_text
    assert "oc.run" not in script_text
    assert "ReturnAnswer" not in script_text
    evidence = json.loads(
        (skill_dir / "references" / "acquisition_abstraction.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["strategy"] == "structured-skill-calls-v1"
    assert evidence["applied"] is True
    assert evidence["facade_free"] is True
    assert evidence["calls"][0]["input_source"] == "input"
    assert evidence["parameters"][0]["flag"] == "--min-genes"


@pytest.mark.parametrize(
    ("code", "records", "expected_reason"),
    [
        (
            "result = oc.run('sc-preprocessing', adata)\n",
            [
                {
                    "skill": "sc-preprocessing",
                    "status": "failed",
                    "params": {},
                }
            ],
            "was not successful",
        ),
        (
            "result = oc.run('sc-preprocessing', adata)\n",
            [
                {
                    "skill": "sc-clustering",
                    "status": "succeeded",
                    "params": {},
                }
            ],
            "source/trace mismatch",
        ),
        (
            "result = oc.run('sc-preprocessing', unknown_data)\n",
            [
                {
                    "skill": "sc-preprocessing",
                    "status": "succeeded",
                    "params": {},
                }
            ],
            "ambiguous input lineage",
        ),
        (
            "result = oc.run('sc-preprocessing', adata, min_genes=[200])\n",
            [
                {
                    "skill": "sc-preprocessing",
                    "status": "succeeded",
                    "params": {"min_genes": [200]},
                }
            ],
            "unsupported name or value type",
        ),
        (
            "result = oc.run('sc-preprocessing', adata)\n",
            [
                {
                    "skill": "sc-preprocessing",
                    "status": "succeeded",
                    "params": {},
                },
                {
                    "skill": "sc-clustering",
                    "status": "succeeded",
                    "params": {},
                },
            ],
            "trace contains 2 calls",
        ),
        (
            "for item in [1]:\n    result = oc.run('sc-preprocessing', adata)\n",
            [
                {
                    "skill": "sc-preprocessing",
                    "status": "succeeded",
                    "params": {},
                }
            ],
            "unsupported non-workflow statement",
        ),
    ],
)
def test_acquisition_abstraction_fails_closed_for_unproven_semantics(
    code: str,
    records: list[dict],
    expected_reason: str,
):
    bundle = AutonomousAnalysisBundle(
        source_dir="/tmp/source",
        notebook_path="",
        analysis_plan="",
        result_summary="",
        web_sources="",
        capability_decision={},
        python_code=code,
        goal="test fail-closed acquisition",
        input_file="/tmp/input.h5ad",
        engine="mini_agent",
        skill_calls=records,
    )

    abstraction = build_acquisition_abstraction(bundle)

    assert abstraction.reusable is False
    assert abstraction.facade_free is False
    assert expected_reason in abstraction.reason


def test_facade_free_acquisition_executes_proven_two_step_lineage(
    tmp_path: Path, monkeypatch
):
    """A second nested skill must consume step 1's produced artifact.

    This proves the structured ``step:N`` lineage end to end through the
    generated entry script, rather than only inspecting the abstraction JSON.
    """
    import importlib.util
    from types import SimpleNamespace

    import omicsclaw.skill.scaffolder as scaffolder_module

    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=[
            "preprocessed = oc.run('sc-preprocessing', adata, min_genes=200)\n"
            "adata = preprocessed.adata\n"
            "clustered = oc.run('sc-clustering', adata, resolution=1.0)\n"
            "adata = clustered.adata\n"
            "ReturnAnswer('done')"
        ],
    )
    trace_records = [
        {
            "index": 1,
            "skill": "sc-preprocessing",
            "params": {"min_genes": 200},
            "flags": ["--min-genes", "200"],
            "input_artifact": "source/input.h5ad",
            "output_dir": "source/step-1",
            "primary_artifact": "source/step-1/processed.h5ad",
            "status": "succeeded",
        },
        {
            "index": 2,
            "skill": "sc-clustering",
            "params": {"resolution": 1.0},
            "flags": ["--resolution", "1.0"],
            "input_artifact": "source/step-1/processed.h5ad",
            "output_dir": "source/step-2",
            "primary_artifact": "source/step-2/processed.h5ad",
            "status": "succeeded",
        },
    ]
    (source_dir / "skill_calls.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in trace_records),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scaffolder_module,
        "_run_demo_smoke_gate",
        lambda *args, **kwargs: scaffolder_module._DemoGateOutcome(
            verdict="earned", reason="synthetic two-step gate"
        ),
    )
    promoted = create_skill_scaffold(
        request="Acquire a reusable preprocessing and clustering workflow.",
        domain="singlecell",
        skill_name="two-step-acquired-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )
    script = Path(promoted.script_path)
    module_spec = importlib.util.spec_from_file_location("two_step_acquired_skill", script)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    observed: list[tuple[str, Path, list[str]]] = []

    def fake_run_skill(skill, *, input_path, output_dir, extra_args, **kwargs):
        input_path = Path(input_path).resolve()
        nested = Path(output_dir)
        nested.mkdir(parents=True, exist_ok=True)
        observed.append((skill, input_path, list(extra_args)))
        payload = input_path.read_text(encoding="utf-8") + f"|{skill}"
        (nested / "processed.h5ad").write_text(payload, encoding="utf-8")
        return SimpleNamespace(
            success=True,
            output_dir=str(nested),
            stdout="",
            stderr="",
        )

    module.run_skill = fake_run_skill
    input_path = tmp_path / "two-step-input.h5ad"
    input_path.write_text("dataset", encoding="utf-8")
    run_out = tmp_path / "two-step-output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--input",
            str(input_path),
            "--output",
            str(run_out),
            "--min-genes",
            "321",
            "--resolution",
            "0.8",
        ],
    )

    module.main()

    assert [item[0] for item in observed] == ["sc-preprocessing", "sc-clustering"]
    assert observed[0][1] == input_path.resolve()
    assert observed[1][1] == (
        run_out / "steps" / "01_sc-preprocessing" / "processed.h5ad"
    ).resolve()
    assert observed[0][2] == ["--min-genes", "321"]
    assert observed[1][2] == ["--resolution", "0.8"]
    assert (run_out / "processed.h5ad").read_text(encoding="utf-8") == (
        "dataset|sc-preprocessing|sc-clustering"
    )
    evidence = json.loads(
        (Path(promoted.skill_dir) / "references" / "acquisition_abstraction.json").read_text(
            encoding="utf-8"
        )
    )
    assert [call["input_source"] for call in evidence["calls"]] == [
        "input",
        "step:1",
    ]


def test_facade_free_acquired_skill_reuses_two_inputs_and_two_parameter_sets(
    tmp_path: Path, monkeypatch
):
    """ACQ-06: one acquired artifact must work across a 2×2 input/parameter
    matrix; successful replay of the original run alone is not generalisation.

    The nested runner is replaced by a contract-faithful deterministic test
    double so this test exercises the generated entry script without invoking
    a heavyweight scientific backend.
    """
    import importlib.util
    from types import SimpleNamespace

    import omicsclaw.skill.scaffolder as scaffolder_module

    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=[
            "result = oc.run('sc-preprocessing', adata, min_genes=200)\n"
            "adata = result.adata\n"
            "ReturnAnswer('done')"
        ],
    )
    (source_dir / "skill_calls.jsonl").write_text(
        json.dumps(
            {
                "index": 1,
                "skill": "sc-preprocessing",
                "params": {"min_genes": "200"},
                "flags": ["--min-genes", "200"],
                "input_artifact": "source-call/input.h5ad",
                "output_dir": "source-call/out",
                "primary_artifact": "source-call/out/processed.h5ad",
                "status": "succeeded",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scaffolder_module,
        "_run_demo_smoke_gate",
        lambda *args, **kwargs: scaffolder_module._DemoGateOutcome(
            verdict="earned", reason="synthetic structured abstraction gate"
        ),
    )
    promoted = create_skill_scaffold(
        request="Acquire a reusable preprocessing workflow.",
        domain="singlecell",
        skill_name="matrix-generalized-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )
    script = Path(promoted.script_path)
    module_spec = importlib.util.spec_from_file_location("matrix_generalized_skill", script)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    observed: list[tuple[str, str]] = []

    def fake_run_skill(skill, *, input_path, output_dir, extra_args, **kwargs):
        assert skill == "sc-preprocessing"
        value = extra_args[extra_args.index("--min-genes") + 1]
        observed.append((str(Path(input_path).resolve()), value))
        nested = Path(output_dir)
        nested.mkdir(parents=True, exist_ok=True)
        payload = Path(input_path).read_text(encoding="utf-8") + f"|min_genes={value}"
        (nested / "processed.h5ad").write_text(payload, encoding="utf-8")
        return SimpleNamespace(
            success=True,
            output_dir=str(nested),
            stdout="",
            stderr="",
        )

    module.run_skill = fake_run_skill
    input_paths = []
    for label in ("dataset-a", "dataset-b"):
        path = tmp_path / f"{label}.h5ad"
        path.write_text(label, encoding="utf-8")
        input_paths.append(path)

    for input_path in input_paths:
        for min_genes in ("111", "777"):
            run_out = tmp_path / f"run-{input_path.stem}-{min_genes}"
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(script),
                    "--input",
                    str(input_path),
                    "--output",
                    str(run_out),
                    "--min-genes",
                    min_genes,
                ],
            )
            module.main()
            assert (run_out / "processed.h5ad").read_text(encoding="utf-8") == (
                f"{input_path.stem}|min_genes={min_genes}"
            )
            envelope = json.loads((run_out / "result.json").read_text(encoding="utf-8"))
            assert envelope["status"] == "ok"
            assert envelope["data"]["steps"][0]["parameters"]["min_genes"] == min_genes

    assert observed == [
        (str(input_path.resolve()), min_genes)
        for input_path in input_paths
        for min_genes in ("111", "777")
    ]


def test_rejected_structured_abstraction_is_regated_on_verbatim_fallback(
    tmp_path: Path, monkeypatch
):
    """A source transform never ships on static confidence alone.

    If its gate rejects, restore accepted source semantics, regenerate derived
    metadata, run the gate again, and retain the exact fallback reason.
    """
    import omicsclaw.skill.scaffolder as scaffolder_module

    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=[
            "result = oc.run('sc-preprocessing', adata)\n"
            "adata = result.adata\n"
            "ReturnAnswer('done')"
        ],
    )
    (source_dir / "skill_calls.jsonl").write_text(
        json.dumps(
            {
                "index": 1,
                "skill": "sc-preprocessing",
                "params": {},
                "flags": [],
                "input_artifact": "source-call/input.h5ad",
                "output_dir": "source-call/out",
                "primary_artifact": "source-call/out/processed.h5ad",
                "status": "succeeded",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gate_scripts: list[str] = []

    def fake_gate(script_path, *args, **kwargs):
        gate_scripts.append(script_path.read_text(encoding="utf-8"))
        if len(gate_scripts) == 1:
            return scaffolder_module._DemoGateOutcome(
                verdict="rejected", reason="synthetic structured semantic regression"
            )
        return scaffolder_module._DemoGateOutcome(
            verdict="earned", reason="verbatim fallback passed"
        )

    monkeypatch.setattr(scaffolder_module, "_run_demo_smoke_gate", fake_gate)
    promoted = create_skill_scaffold(
        request="Acquire with a safe fallback.",
        domain="singlecell",
        skill_name="structured-fallback-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )

    assert len(gate_scripts) == 2
    assert "from omicsclaw.skill.runner import run_skill" in gate_scripts[0]
    assert "build_facade" in gate_scripts[1]
    shipped = Path(promoted.script_path).read_text(encoding="utf-8")
    assert "build_facade" in shipped
    evidence = json.loads(
        (Path(promoted.skill_dir) / "references" / "acquisition_abstraction.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["applied"] is False
    assert evidence["facade_free"] is False
    assert evidence["fallback_reason"] == "synthetic structured semantic regression"


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


def test_promoted_skill_demo_gate_skips_without_crediting_when_bwrap_unavailable(
    tmp_path: Path, monkeypatch
):
    """Fix 2 (security): promoted (model-authored) code is untrusted and must
    not run unsandboxed. When bwrap is unavailable, the gate must NOT fall
    back to an unsandboxed run and award demo-validated trust — it must
    skip, leaving the skill at the smoke-only default. Untrusted code that did
    not run inside the required OS sandbox must stay outside the discoverable
    skill tree: keep it in the acquisition quarantine for inspection/retry."""
    import omicsclaw.autonomous.kernel_envelope as kernel_envelope_module

    monkeypatch.setattr(kernel_envelope_module, "envelope_available", lambda: False)

    output_root, source_dir = _build_real_mini_agent_run(tmp_path, real_input=True)

    result = create_skill_scaffold(
        request="Package the clustering run as a reusable skill.",
        domain="singlecell",
        skill_name="no-sandbox-promote-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )

    skill_dir = Path(result.skill_dir)
    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    formal_dir = tmp_path / "skills" / "singlecell" / "no-sandbox-promote-skill"
    assert result.quarantined is True
    assert ".quarantine" in skill_dir.parts
    assert not formal_dir.exists()
    assert manifest.validation.level != "demo-validated"
    assert not (skill_dir / "references" / "validation.md").exists()
    assert (skill_dir / "references" / "quarantine.md").exists()

    registry = OmicsRegistry()
    registry.load_all(tmp_path / "skills")
    assert "no-sandbox-promote-skill" not in registry.skills


def test_plain_scaffold_demo_gate_does_not_require_bwrap(tmp_path: Path, monkeypatch):
    """A freshly-scaffolded (non-promoted, self-authored template) skill is
    not untrusted model-authored code — its demo gate must keep working even
    when bwrap is unavailable, since require_sandbox is only set for
    promotions (source_bundle is not None)."""
    import omicsclaw.autonomous.kernel_envelope as kernel_envelope_module

    monkeypatch.setattr(kernel_envelope_module, "envelope_available", lambda: False)

    result = create_skill_scaffold(
        request="Create a demo-only placeholder skill.",
        domain="singlecell",
        skill_name="plain-scaffold-no-bwrap",
        skills_root=tmp_path / "skills",
        output_root=tmp_path / "output",
    )
    assert result.completion["completed"] is True


def test_find_latest_autonomous_analysis_discovers_mini_agent_run(tmp_path: Path):
    """``promote_from_latest`` must discover a real mini-agent run."""
    output_root, source_dir = _build_real_mini_agent_run(tmp_path)
    latest = find_latest_autonomous_analysis(output_root=output_root)
    assert latest is not None
    assert latest.resolve() == source_dir.resolve()


def test_promote_from_latest_requires_explicit_source_analysis_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="source_analysis_dir"):
        create_skill_scaffold(
            request="Promote the latest analysis.",
            domain="singlecell",
            skill_name="unsafe-latest-promotion",
            promote_from_latest=True,
            output_root=tmp_path / "output",
            skills_root=tmp_path / "skills",
        )


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


def test_promoted_mini_agent_skill_lifts_literal_oc_run_kwargs(tmp_path: Path):
    """P2a (acquisition-plan.md §P2): a literal oc.run(...) kwarg becomes an
    overridable CLI flag on the promoted script, instead of being frozen
    forever at whatever value the original mini-agent run happened to use."""
    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=[
            "res = oc.run('sc-preprocessing', adata, min_genes=5)",
            "show()\nReturnAnswer('done')",
        ],
    )
    result = create_skill_scaffold(
        request="Package the run as a reusable skill.",
        domain="singlecell",
        skill_name="lift-promote-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )
    skill_dir = Path(result.skill_dir)
    script = skill_dir / "lift_promote_skill.py"
    script_text = script.read_text(encoding="utf-8")
    assert "--min-genes" in script_text
    assert "min_genes=args.min_genes" in script_text
    assert "min_genes=5)" not in script_text
    assert lint_skill(skill_dir) == []

    # ADR 0041: allowed_extra_flags derives from the script's real argparse
    # surface, so the new flag is automatically usable with no schema wiring.
    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    accepted_flags = derive_accepted_flags(skill_dir, script.name, manifest.type)
    assert "--min-genes" in accepted_flags

    evidence = (skill_dir / "references" / "parameter_lift.md").read_text(encoding="utf-8")
    assert "--min-genes" in evidence
    assert "5" in evidence

    # The override must actually reach the nested oc.run() call — verified via
    # the facade's own skill_calls.jsonl provenance record (params round-trip
    # through CLI-flag strings, see skill_facade.py _flags_to_params).
    run_out = tmp_path / "run_out"
    proc = subprocess.run(
        [sys.executable, str(script), "--demo", "--output", str(run_out), "--min-genes", "777"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    skill_calls = [
        json.loads(line)
        for line in (run_out / "skill_calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert skill_calls[0]["params"]["min_genes"] == "777"


def test_lift_falls_back_to_verbatim_when_the_gate_rejects_the_lifted_script(tmp_path, monkeypatch):
    """P2a safety net (acquisition-plan.md §3 point 5): a code transform can
    silently break otherwise-working code. If the --demo gate rejects the
    LIFTED script, create_skill_scaffold must fall back to the untouched
    verbatim body and re-gate THAT — never ship a script that hasn't passed
    some gate run, and never leave parameters.md advertising a flag the
    shipped script doesn't have."""
    import omicsclaw.skill.scaffolder as scaffolder_module

    output_root, source_dir = _build_real_mini_agent_run(
        tmp_path,
        real_input=True,
        accepted_cells=[
            "res = oc.run('sc-preprocessing', adata, min_genes=5)",
            "show()\nReturnAnswer('done')",
        ],
    )

    real_gate = scaffolder_module._run_demo_smoke_gate
    seen_scripts: list[str] = []

    def fake_gate(script_path, output_dir, **kwargs):
        seen_scripts.append(script_path.read_text(encoding="utf-8"))
        if len(seen_scripts) == 1:
            return scaffolder_module._DemoGateOutcome(
                verdict="rejected", reason="synthetic failure for fallback test"
            )
        return real_gate(script_path, output_dir, **kwargs)

    monkeypatch.setattr(scaffolder_module, "_run_demo_smoke_gate", fake_gate)

    result = create_skill_scaffold(
        request="Package the run as a reusable skill.",
        domain="singlecell",
        skill_name="fallback-promote-skill",
        skills_root=tmp_path / "skills",
        source_analysis_dir=source_dir,
        output_root=output_root,
    )

    assert len(seen_scripts) == 2
    assert "min_genes=args.min_genes" in seen_scripts[0]  # 1st attempt: lifted
    assert "min_genes=5)" in seen_scripts[1]  # 2nd attempt: fallback verbatim

    skill_dir = Path(result.skill_dir)
    shipped_text = (skill_dir / "fallback_promote_skill.py").read_text(encoding="utf-8")
    assert "min_genes=5)" in shipped_text
    assert "--min-genes" not in shipped_text
    assert lint_skill(skill_dir) == []

    parameters_md = (skill_dir / "references" / "parameters.md").read_text(encoding="utf-8")
    assert "--min-genes" not in parameters_md

    evidence = (skill_dir / "references" / "parameter_lift.md").read_text(encoding="utf-8")
    assert "fell back to verbatim" in evidence
    assert "synthetic failure for fallback test" in evidence

    # The verbatim retry ran for real (real_gate) and succeeded on its own merits.
    assert result.demo_gate_verdict == "earned"
    assert result.quarantined is False
    assert load_skill_yaml(skill_dir / "skill.yaml").lifecycle.status == "mvp"


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
