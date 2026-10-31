"""Tests for E1--E4 evidence-chain benchmark suite and multi-turn adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omicsclaw.bench.adapters import build_adapter
from omicsclaw.bench.evidence import (
    E1ReplayFaithfulnessGrader,
    E2StalenessLedgerGrader,
    E3ReviewerGradientGrader,
    E4NullTwinDGPGrader,
    build_e3_reviewer_prompt,
    calibrate_e4_suite,
    deterministic_statistical_audit,
    generate_double_dipping_dgp,
    generate_pseudorep_dgp,
    generate_spatial_dgp,
    run_e2_staleness_suite,
    verify_provenance_bundle,
)
from omicsclaw.bench.grade import Submission, load_grader, self_check
from omicsclaw.bench.layout import RunPaths
from omicsclaw.bench.manifest import Arm, Case, Model, RunSpec


@pytest.mark.parametrize(
    "ref",
    [
        "omicsclaw.bench.evidence:E1ReplayFaithfulnessGrader",
        "omicsclaw.bench.evidence:E2StalenessLedgerGrader",
        "omicsclaw.bench.evidence:E3ReviewerGradientGrader",
        "omicsclaw.bench.evidence:E4NullTwinDGPGrader",
    ],
)
def test_all_evidence_graders_load_and_pass_self_check(ref: str) -> None:
    grader = load_grader(ref)
    controls = list(grader.controls())
    assert sum(1 for c in controls if c.expect_pass) >= 2
    assert sum(1 for c in controls if not c.expect_pass) >= 3
    assert self_check(grader) == []


def test_e1_grader_accepts_float_tolerance_and_rejects_orphan_table(tmp_path: Path) -> None:
    grader = E1ReplayFaithfulnessGrader()
    ws = tmp_path / "ws"
    oracle = tmp_path / "oracle"
    (ws / "output" / "tables").mkdir(parents=True)
    oracle.mkdir(parents=True)

    script = (
        "from pathlib import Path\n"
        "import json\n"
        "out = Path('output')\n"
        "(out / 'tables').mkdir(parents=True, exist_ok=True)\n"
        "(out / 'tables' / 'de.csv').write_text('gene,pval\\nG1,0.00100001\\n', encoding='utf-8')\n"
        "(out / 'results.json').write_text(json.dumps({'n_significant': 1, 'top_genes': ['G1']}), encoding='utf-8')\n"
    )
    (ws / "output" / "reproduce.py").write_text(script, encoding="utf-8")
    (ws / "output" / "tables" / "de.csv").write_text("gene,pval\nG1,0.00100002\n", encoding="utf-8")
    (ws / "output" / "results.json").write_text(
        json.dumps({"n_significant": 1, "top_genes": ["G1"]}), encoding="utf-8"
    )
    (ws / "output" / "REPORT.md").write_text("# DE\n- n_significant: 1\nTop: G1\n", encoding="utf-8")
    (oracle / "expected.json").write_text(
        json.dumps({"tables": ["de.csv"], "claims": {"n_significant": 1}, "top_genes": ["G1"]}),
        encoding="utf-8",
    )

    sub = Submission(run="r1", case="c1", workspace=ws, deliverables={})
    grade = grader.grade(sub, oracle)
    assert grade.passed is True

    # Add an unregenerated orphan table in workspace output/tables -> must fail artifact_equivalence
    (ws / "output" / "tables" / "orphan.csv").write_text("gene,pval\nBAD,0.5\n", encoding="utf-8")
    grade_orphan = grader.grade(sub, oracle)
    assert grade_orphan.passed is False
    assert grade_orphan.metrics["artifact_equivalence"] is False


def test_e2_staleness_suite_achieves_100_percent_recall_and_zero_fpr(tmp_path: Path) -> None:
    report = run_e2_staleness_suite(tmp_path)
    assert report["recall"] == 1.0
    assert report["false_positive_rate"] == 0.0
    scenarios = report["scenarios"]
    for idx in range(1, 10):
        key = f"I{idx}"
        assert scenarios[key]["valid"] is False
        assert any(a.startswith(f"I{idx}_") for a in scenarios[key]["anomalies"])
    assert scenarios["I10"]["valid"] is True
    assert scenarios["I10"]["anomalies"] == []


def test_e3_reviewer_prompts_and_gradient_audit() -> None:
    bundle = {
        "REPORT.md": "# Study\nWe found 42 DE genes.\n## Conclusion\nEverything is perfect and significant.\n",
        "reproduce.py": "df = load_multi_donor('donor_id')\nres = wilcoxon_cells(df)\n",
        "review_brief.md": "replay_verified: False\ntable_claim_mismatch:A1\n",
    }
    p_v1 = build_e3_reviewer_prompt("V1", bundle)
    assert "Everything is perfect" in p_v1
    assert "wilcoxon_cells" not in p_v1

    p_v2 = build_e3_reviewer_prompt("V2", bundle)
    assert "Everything is perfect" not in p_v2
    assert "wilcoxon_cells" in p_v2

    p_v5 = build_e3_reviewer_prompt("V5", bundle)
    assert "Domain Statistical & Provenance Audit Checklist" in p_v5
    assert "B5 Sample-vs-Cell Unit of Replication" in p_v5

    assert deterministic_statistical_audit(bundle, "V1")["verdict"] == "APPROVE"
    v4_audit = deterministic_statistical_audit(bundle, "V4")
    assert v4_audit["verdict"] == "REVISE"
    assert "A1" in v4_audit["categories"] and "A2" in v4_audit["categories"]
    v5_audit = deterministic_statistical_audit(bundle, "V5")
    assert "B5" in v5_audit["categories"]


def test_e4_dgp_tiers_and_pre_agent_calibration_gates() -> None:
    t1 = generate_pseudorep_dgp(seed=7, mode="null", tier="T1")
    t2 = generate_pseudorep_dgp(seed=7, mode="twin", tier="T2")
    assert "donor_id" in t1["obs"][0]
    assert "orig.ident" in t2["obs"][0] and "donor_id" not in t2["obs"][0]
    assert t1["true_genes"] == []
    assert len(t2["true_genes"]) == 16

    dd_null = generate_double_dipping_dgp(seed=7, mode="null")
    sp_twin = generate_spatial_dgp(seed=7, mode="twin")
    assert dd_null["true_genes"] == []
    assert len(sp_twin["true_genes"]) == 14

    cal = calibrate_e4_suite(n_seeds=10)
    assert cal["all_gates_passed"] is True
    assert cal["V1"]["passed"] is True
    assert cal["V2"]["passed"] is True
    assert cal["V3"]["passed"] is True
    assert cal["V4"]["passed"] is True


def test_multiturn_adapter_builds_and_launches(tmp_path: Path) -> None:
    from omicsclaw.bench.manifest import Budget

    arm = Arm(
        id="mt-arm",
        adapter="omicsclaw-multiturn",
        options={
            "reasoning_effort": "high",
            "turn2_prompt": "Audit unit of replication and export clean-room script.",
            "perturbation": {"kind": "F3", "target": "output/intermediate_cache.txt"},
            "enforce_replay_gate": True,
        },
    )
    adapter = build_adapter(arm)
    model = Model(id="gpt-5.4", provider="openai", model="gpt-5.4")
    case = Case(id="c1", prompt="Run DE analysis.", deliverables=("output/reproduce.py",))
    run = RunSpec(arm=arm, model=model, case=case, repeat=1)
    paths = RunPaths(
        workspace=tmp_path / "ws",
        meta=tmp_path / "meta",
    )
    paths.workspace.mkdir(parents=True)
    paths.meta.mkdir(parents=True)
    paths.prompt.write_text("Turn 1: run initial marker analysis.\n", encoding="utf-8")

    launch = adapter.launch(run, paths, Budget(wall_clock_s=60), {})
    assert launch.argv[1] == "-P"
    assert launch.argv[2] == "-c"
    driver_cfg = json.loads(launch.argv[4])
    assert driver_cfg["provider"] == "openai"
    assert driver_cfg["model"] == "gpt-5.4"
    assert driver_cfg["reasoning_effort"] == "high"
    assert driver_cfg["enforce_replay_gate"] is True
    assert len(driver_cfg["turns"]) == 2
    assert launch.env.get("OMICSCLAW_REASONING_EFFORT") == "high"
    assert launch.provenance.get("multiturn_count") == 2


def test_e2_verify_provenance_bundle_accepts_real_export_and_replay_bundle(tmp_path: Path) -> None:
    from omicsclaw.entry.notebook.service import export_and_replay
    from omicsclaw.memory.database import Database
    from omicsclaw.memory.notebook import NotebookCell, NotebookStore

    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "in.csv").write_text("gene,val\nG1,10\n", encoding="utf-8")
    (ws / "output" / "tables").mkdir(parents=True)
    (ws / "output" / "tables" / "out.csv").write_text("gene,val\nG1,10\n", encoding="utf-8")
    (ws / "output" / "results.json").write_text(json.dumps({"n_genes": 1}), encoding="utf-8")
    (ws / "output" / "REPORT.md").write_text("# Report\n- n_genes: 1\n", encoding="utf-8")

    (ws / ".omicsclaw").mkdir(parents=True)
    db = Database(ws / ".omicsclaw" / "notebooks.db")
    store = NotebookStore(db)
    code = (
        "from pathlib import Path\n"
        "import json\n"
        "raw = Path('data/in.csv').read_text(encoding='utf-8')\n"
        "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/tables/out.csv').write_text(raw, encoding='utf-8')\n"
        "Path('output/results.json').write_text(json.dumps({'n_genes': 1}), encoding='utf-8')\n"
    )
    cell = NotebookCell(
        notebook_id="default",
        cell_id="c1",
        source=code,
        status="completed",
        execution_id="e1",
        output={
            "files_read": [{"path": "data/in.csv"}],
            "files_written": [
                {"path": "output/tables/out.csv"},
                {"path": "output/results.json"},
            ],
        },
    )
    db.run(lambda c: store._put_cell(c, cell, 0))
    db.close()

    receipt = export_and_replay("default", ws, module="01_qc")
    assert receipt["status"] == "ok"

    verdict = verify_provenance_bundle(ws)
    assert verdict["valid"] is True
    assert verdict["anomalies"] == []

    # Tamper input file -> verify_provenance_bundle flags I1_tampered_input
    (ws / "data" / "in.csv").write_text("gene,val\nG1,999\n", encoding="utf-8")
    verdict_tampered = verify_provenance_bundle(ws)
    assert verdict_tampered["valid"] is False
    assert "I1_tampered_input:data/in.csv" in verdict_tampered["anomalies"]


def test_e4_t2_orig_ident_pseudobulk_recovery_and_ast_mechanism_hardening(tmp_path: Path) -> None:
    from omicsclaw.bench.evidence import _eval_pseudorep_methods, _inspect_statistical_mechanism

    dgp_t2 = generate_pseudorep_dgp(seed=42, mode="twin", tier="T2")
    unique_idents = {row["orig.ident"] for row in dgp_t2["obs"]}
    assert len(unique_idents) == 8
    assert "SC_L01" in unique_idents

    res_t2 = _eval_pseudorep_methods(dgp_t2)
    assert len(res_t2["pseudobulk_sig"]) >= 8

    # Comments mentioning donor_id or pseudobulk must NOT pass AST mechanism inspection
    assert _inspect_statistical_mechanism("# donor_id pseudobulk\nprint(1)\n") is False
    # Cell-level Wilcoxon rank_genes_groups(groupby="condition") must NOT pass AST mechanism inspection
    assert (
        _inspect_statistical_mechanism(
            'import scanpy as sc\nsc.tl.rank_genes_groups(adata, groupby="condition", method="wilcoxon")\n'
        )
        is False
    )
    # Real donor/slide aggregation passes
    assert _inspect_statistical_mechanism("pb = df.groupby('donor_id').sum()\n") is True
    assert _inspect_statistical_mechanism("pb = spatial_pseudobulk(adata, sample_key='slide_id')\n") is True

    # E4NullTwinDGPGrader rejects script that ignores workspace/data when workspace/data has files
    grader = E4NullTwinDGPGrader()
    ws = tmp_path / "e4_ws"
    oracle = tmp_path / "e4_oracle"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "counts.csv").write_text("cell,donor_id,G1\nc1,d1,5\n", encoding="utf-8")
    (ws / "output").mkdir(parents=True)
    (ws / "output" / "reproduce.py").write_text("pb = df.groupby('donor_id').sum()\n", encoding="utf-8")
    (ws / "output" / "results.json").write_text(
        json.dumps({"n_significant": 0, "significant_genes": []}), encoding="utf-8"
    )
    oracle.mkdir(parents=True)
    (oracle / "truth.json").write_text(
        json.dumps({"mode": "null", "paradigm": "pseudorep", "true_genes": []}), encoding="utf-8"
    )
    sub = Submission(run="r1", case="c1", workspace=ws, deliverables={})
    grade_no_read = grader.grade(sub, oracle)
    assert grade_no_read.passed is False

    # When script reads data/counts.csv, aggregates by donor_id, and writes output/results.json, it passes
    (ws / "output" / "reproduce.py").write_text(
        "import json\nfrom pathlib import Path\nimport pandas as pd\n"
        "df = pd.read_csv('data/counts.csv')\n"
        "pb = df.groupby('donor_id').sum()\n"
        "Path('output').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/results.json').write_text(json.dumps({'n_significant': 0, 'significant_genes': []}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    grade_with_read = grader.grade(sub, oracle)
    assert grade_with_read.passed is True


def test_e3_deterministic_audit_catches_real_review_brief_and_cell_level_groupby() -> None:
    bundle = {
        "REPORT.md": "# Multi-Donor DE\n",
        "reproduce.py": (
            "import scanpy as sc\n"
            "# multi_donor dataset with donor_id\n"
            "sc.tl.rank_genes_groups(adata, groupby='condition', method='wilcoxon')\n"
            "sc.pp.log1p(adata)\n"
            "run_deseq2(adata)\n"
            "hypergeom(k, M=20000, n=50, N=500)\n"
        ),
        "review_brief.md": (
            "# OmicsClaw Provenance & Replay Review Brief\n"
            "- **Status**: `failed` (exit_code=0)\n"
            "- **Orphan Outputs**: ['output/tables/orphan.csv']\n"
            "- **Missing Outputs**: ['output/figures/umap.png']\n"
        ),
    }
    audit = deterministic_statistical_audit(bundle, "V5")
    assert audit["verdict"] == "REVISE"
    for expected_cat in ("A2", "A4", "B5", "B8", "B9"):
        assert expected_cat in audit["categories"]


def test_multiturn_driver_script_compiles_and_resolves_surface_config(tmp_path: Path) -> None:
    from omicsclaw.bench.adapters.omicsclaw_multiturn import (
        _PARTICIPANT_SCRIPT,
        OmicsClawMultiTurnAdapter,
    )
    from omicsclaw.entry.config import resolve_app_config
    from omicsclaw.launch._surfaces import surface_config
    from omicsclaw.bench.manifest import Budget
    from tests.bench._support import scripted_backend

    compile(_PARTICIPANT_SCRIPT, "<multiturn_driver>", "exec")
    assert "from omicsclaw.launch._surfaces import" in _PARTICIPANT_SCRIPT
    assert "surface_config" in _PARTICIPANT_SCRIPT
    assert "omicsclaw.entry.surfaces" not in _PARTICIPANT_SCRIPT
    assert "_resolve_artifact_store(app)" in _PARTICIPANT_SCRIPT
    assert "post_repair_replay_summary.json" in _PARTICIPANT_SCRIPT
    cfg = surface_config(resolve_app_config(["--workspace", str(tmp_path)], {}), "once")
    assert cfg is not None

    arm = Arm(id="mt-collect", adapter="omicsclaw-multiturn", options={})
    adapter = OmicsClawMultiTurnAdapter(arm)
    model = Model(id="gpt-5.4", provider="openai", model="gpt-5.4")
    case = Case(id="c1", prompt="Run analysis.", deliverables=("output/reproduce.py",))
    run = RunSpec(arm=arm, model=model, case=case, repeat=1)
    paths = RunPaths(workspace=tmp_path / "ws", meta=tmp_path / "meta")
    paths.workspace.mkdir(parents=True)
    paths.meta.mkdir(parents=True)
    log_dir = paths.workspace / ".omicsclaw" / "logs"
    log_dir.mkdir(parents=True)
    events = [
        {"type": "span.end", "name": "interaction", "span": "r1", "parent": None, "ms": 1200.0, "turns": 2, "status": "ok"},
        {"type": "span.end", "name": "interaction", "span": "r2", "parent": None, "ms": 1800.0, "turns": 3, "status": "ok"},
    ]
    (log_dir / "omicsclaw.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    evidence = adapter.collect(run, paths)
    assert evidence.agent.turns == 5
    assert abs(evidence.wall_s - 3.0) < 1e-6

    # End-to-end subprocess execution of OmicsClawMultiTurnAdapter.run() + .collect()
    e2e_root = tmp_path / "mt_e2e"
    e2e_root.mkdir(parents=True)
    shim = scripted_backend(e2e_root)
    e2e_paths = RunPaths(workspace=e2e_root / "ws", meta=e2e_root / "meta")
    e2e_paths.workspace.mkdir(parents=True)
    e2e_paths.meta.mkdir(parents=True)
    (e2e_paths.workspace / "data").mkdir(parents=True)
    (e2e_paths.workspace / "data" / "numbers.txt").write_text("10\n20\n", encoding="utf-8")
    (e2e_paths.workspace / "data" / "covariates.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    e2e_paths.prompt.write_text("Turn 1 prompt\n<!-- TURN_2 -->\nTurn 2 prompt\n", encoding="utf-8")
    e2e_arm = Arm(
        id="mt-e2e",
        adapter="omicsclaw-multiturn",
        options={
            "perturbation": {"kind": "F4", "target": "data/covariates.csv"},
            "enforce_replay_gate": True,
        },
        env={"PYTHONPATH": str(shim)},
    )
    e2e_adapter = OmicsClawMultiTurnAdapter(e2e_arm)
    e2e_run = RunSpec(
        arm=e2e_arm,
        model=Model(id="m", provider="custom", model="stub-write"),
        case=Case(id="c1", prompt="x", deliverables=("output/answer.json",)),
        repeat=1,
    )
    exit_res = e2e_adapter.run(e2e_run, e2e_paths, Budget(wall_clock_s=30, max_turns=4))
    assert exit_res.started is True and exit_res.returncode == 0
    assert not (e2e_paths.workspace / "data" / "covariates.csv").exists()
    assert (e2e_paths.workspace / "provenance" / "post_repair_replay_summary.json").is_file()
    e2e_ev = e2e_adapter.collect(e2e_run, e2e_paths, exit_res)
    assert e2e_ev.agent.turns == 4
    assert e2e_ev.notes["interaction_count"] == 3


def test_e4_grader_rejects_uncalled_functions_dead_branches_and_non_reproducible_results(tmp_path: Path) -> None:
    from omicsclaw.bench.evidence import _inspect_statistical_mechanism

    assert _inspect_statistical_mechanism("if False:\n    pb = df.groupby('donor_id').sum()\n") is False
    assert _inspect_statistical_mechanism("def unused(df):\n    return df.groupby('donor_id').sum()\n") is False
    assert _inspect_statistical_mechanism("print('donor_id')\nx = sum([1, 2, 3])\n") is False
    assert _inspect_statistical_mechanism("def used(df):\n    return df.groupby('donor_id').sum()\npb = used(df)\n") is True
    assert _inspect_statistical_mechanism(
        "import scanpy as sc\nadata = sc.read_h5ad('data/dd.h5ad')\nsc.tl.leiden(adata)\nsc.tl.rank_genes_groups(adata, groupby='leiden')\nres = check_homogeneous_null(adata)\n"
    ) is True
    assert _inspect_statistical_mechanism(
        "import scanpy as sc\nadata = sc.read_h5ad('data/dd.h5ad')\nsc.tl.leiden(adata)\nsc.tl.rank_genes_groups(adata, groupby='leiden')\n"
    ) is False

    grader = E4NullTwinDGPGrader()
    ws = tmp_path / "e4_bypass_ws"
    oracle = tmp_path / "e4_bypass_oracle"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "counts.csv").write_text("cell,donor_id,G1\nc1,d1,5\n", encoding="utf-8")
    (ws / "output").mkdir(parents=True)
    # 1. Script reads data and aggregates at top level, but does NOT write output/results.json
    (ws / "output" / "reproduce.py").write_text(
        "import pandas as pd\ndf = pd.read_csv('data/counts.csv')\npb = df.groupby('donor_id').sum()\n",
        encoding="utf-8",
    )
    (ws / "output" / "results.json").write_text(
        json.dumps({"n_significant": 0, "significant_genes": []}), encoding="utf-8"
    )
    oracle.mkdir(parents=True)
    (oracle / "truth.json").write_text(
        json.dumps({"mode": "null", "paradigm": "pseudorep", "true_genes": []}), encoding="utf-8"
    )
    sub = Submission(run="r1", case="c1", workspace=ws, deliverables={})
    grade_stale = grader.grade(sub, oracle)
    assert grade_stale.passed is False
    assert grade_stale.score == 0.0
    assert grade_stale.metrics["clean_room_replay_verified"] is False

    # 2. Hardcoded output/results.json with comment `# data/sc_null.h5ad` and dead `if False:` / uncalled `def unused():`
    (ws / "output" / "reproduce.py").write_text(
        "# data/sc_null.h5ad\n"
        "import json\n"
        "from pathlib import Path\n"
        "def unused(df):\n"
        "    return df.groupby('donor_id').sum()\n"
        "if False:\n"
        "    pb = df.groupby('donor_id').sum()\n"
        "Path('output').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/results.json').write_text(json.dumps({'n_significant': 0, 'significant_genes': []}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    grade_comment_dead = grader.grade(sub, oracle)
    assert grade_comment_dead.passed is False
    assert grade_comment_dead.score == 0.0

    # 3. Naive cell-level rank_genes_groups that also prints "donor_id" and calls sum([1, 2])
    (ws / "output" / "reproduce.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "print('donor_id')\n"
        "_ = sum([1, 2])\n"
        "_ = Path('data/counts.csv').read_text(encoding='utf-8')\n"
        "def rank_genes_groups(adata, groupby='condition'):\n"
        "    return []\n"
        "rank_genes_groups(None, groupby='condition')\n"
        "Path('output').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/results.json').write_text(json.dumps({'n_significant': 0, 'significant_genes': []}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    grade_naive_de = grader.grade(sub, oracle)
    assert grade_naive_de.passed is False
    assert grade_naive_de.score == 0.0


def test_e4_double_dipping_nb_count_split_and_spatial_domains() -> None:
    dd = generate_double_dipping_dgp(seed=7, mode="null")
    assert len(dd["counts"]) == 400
    assert len(dd["counts"][0]) == 200
    assert len(dd["dispersions"]) == 200
    assert len(dd["size_factors"]) == 400
    assert max(max(row) for row in dd["counts"]) > 1

    sp = generate_spatial_dgp(seed=7, mode="twin")
    assert all("spatial_domain" in row and "region" in row for row in sp["obs"])

    cal = calibrate_e4_suite(n_seeds=10)
    assert cal["all_gates_passed"] is True
    assert "twin_recall" in cal["V4"] and cal["V4"]["twin_recall"] >= 0.60
    assert "twin_fdp" in cal["V4"] and cal["V4"]["twin_fdp"] <= 0.20


