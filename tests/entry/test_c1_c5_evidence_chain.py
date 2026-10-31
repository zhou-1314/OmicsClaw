"""End-to-end unit and integration tests for C1--C5 evidence-chain architecture."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.notebook.service import export_and_replay
from omicsclaw.entry.review import ReviewArchive
from omicsclaw.entry.shadow_review import _review_prompt
from omicsclaw.entry.subagent import MODULE_REVIEWER_PROMPT
from omicsclaw.kernel.session import CellResult, _append_workspace_ledger, _write_ipython_ledger_startup
from omicsclaw.memory.database import Database
from omicsclaw.memory.notebook import NotebookCell, NotebookStore
from omicsclaw.provider.config import ProviderConfig, resolve_config
from omicsclaw.provider.openai_provider import OpenAIProvider
from omicsclaw.runs.verification import (
    Verdict,
    verify_json_equivalence,
    verify_replay_receipt,
    verify_table_equivalence,
)
from omicsclaw.schema import Message, Role
from omicsclaw.skills.sidecar import build_sidecar_bootstrap_code
from tests.entry.test_subagent_wiring import REVIEW_CHECKS


def _seed_completed_notebook(workspace: Path, notebook_id: str, cell_code: str) -> None:
    oc_dir = workspace / ".omicsclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    db = Database(oc_dir / "notebooks.db")
    store = NotebookStore(db)
    cell = NotebookCell(
        notebook_id=notebook_id,
        cell_id="cell-1",
        source=cell_code,
        status="completed",
        execution_id="exec-1",
    )
    db.run(lambda c: store._put_cell(c, cell, 0))


def test_c1_export_and_replay_creates_verified_receipt_and_catches_tampering(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "input.csv").write_text("gene,val\nCD3D,10.5\nMS4A1,8.2\n", encoding="utf-8")

    cell_code = (
        "from pathlib import Path\n"
        "import json\n"
        "raw = Path('data/input.csv').read_text(encoding='utf-8')\n"
        "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/tables/markers.csv').write_text(raw, encoding='utf-8')\n"
        "Path('output/results.json').write_text(json.dumps({'n_genes': 2}), encoding='utf-8')\n"
    )
    _seed_completed_notebook(tmp_path, "default", cell_code)
    (tmp_path / "output" / "tables").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / "tables" / "markers.csv").write_text(
        "gene,val\nCD3D,10.5\nMS4A1,8.2\n", encoding="utf-8"
    )
    (tmp_path / "output" / "results.json").write_text(json.dumps({"n_genes": 2}), encoding="utf-8")
    (tmp_path / "output" / "REPORT.md").write_text("# Report\nn_genes: 2\n", encoding="utf-8")

    receipt = export_and_replay("default", tmp_path, module="01_qc")
    assert receipt["status"] == "ok"
    assert (tmp_path / "output" / "reproduce.py").is_file()
    receipt_file = tmp_path / "provenance" / "replay_receipt.json"
    assert receipt_file.is_file()
    assert (tmp_path / "provenance" / "review_brief.md").is_file()

    audit_ok = verify_replay_receipt(receipt_file, workspace=tmp_path)
    assert audit_ok.verdict == Verdict.PASS

    # Tamper with output table -> verify_replay_receipt must catch it
    (tmp_path / "output" / "tables" / "markers.csv").write_text("gene,val\nCD3D,999.0\n", encoding="utf-8")
    audit_tampered = verify_replay_receipt(receipt_file, workspace=tmp_path)
    assert audit_tampered.verdict == Verdict.FAIL
    assert "output/tables/markers.csv" in audit_tampered.details


def test_c1_table_and_json_equivalence_helpers(tmp_path: Path) -> None:
    t1 = tmp_path / "t1.csv"
    t2 = tmp_path / "t2.csv"
    t1.write_text("gene,log2fc,pval\nG1,2.500000,0.001000\nG2,-1.200000,0.020000\n", encoding="utf-8")
    t2.write_text("gene,log2fc,pval\nG1,2.500005,0.001000\nG2,-1.200001,0.020000\n", encoding="utf-8")
    res_ok = verify_table_equivalence(t1, t2)
    assert res_ok.verdict == Verdict.PASS

    t3 = tmp_path / "t3.csv"
    t3.write_text("gene,log2fc,pval\nG1,9.500000,0.001000\nOTHER,-1.200000,0.020000\n", encoding="utf-8")
    res_bad = verify_table_equivalence(t1, t3)
    assert res_bad.verdict == Verdict.FAIL

    j1 = tmp_path / "j1.json"
    j2 = tmp_path / "j2.json"
    j1.write_text(json.dumps({"score": 1.25, "timestamp": "2026-01-01"}), encoding="utf-8")
    j2.write_text(json.dumps({"score": 1.250001, "timestamp": "2026-10-30"}), encoding="utf-8")
    assert verify_json_equivalence(j1, j2).verdict == Verdict.PASS


def test_c2_sidecar_auto_ledger_and_kernel_audit_hook(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n", encoding="utf-8")
    kernel_py = skill_dir / "kernel.py"
    kernel_py.write_text(
        "__all__ = ['compute_metric']\ndef compute_metric(x, scale=2.0):\n    return x * scale\n",
        encoding="utf-8",
    )
    bootstrap = build_sidecar_bootstrap_code("demo-skill", kernel_path=kernel_py, src=kernel_py.read_text(encoding="utf-8"), skills_root=tmp_path / "skills")
    ns: dict[str, object] = {}
    exec(bootstrap, ns)  # noqa: S102
    compute_fn = ns["compute_metric"]
    assert callable(compute_fn)
    out = compute_fn(5, scale=3.0)
    assert out == 15.0

    ledger = ns["__omicsclaw_skill_ledger__"]
    assert isinstance(ledger, list) and len(ledger) == 1
    entry = ledger[0]
    assert entry["skill"] == "demo-skill"
    assert entry["slug"] == "demo_skill"
    assert entry["function"] == "compute_metric"
    assert len(entry["kernel_sha256"]) == 64

    ipy_dir = tmp_path / "ipython"
    _write_ipython_ledger_startup(ipy_dir, workspace=tmp_path, ledger_path=tmp_path / "ledger.json")
    startup_file = ipy_dir / "profile_default" / "startup" / "00_omicsclaw_ledger.py"
    assert startup_file.is_file()
    assert "sys.addaudithook" in startup_file.read_text(encoding="utf-8")

    cell_res = CellResult(
        stdout="ok",
        execution_count=1,
        files_read=[{"path": "data/input.csv", "sha256": "abc123"}],
        skill_calls=[entry],
    )
    _append_workspace_ledger(tmp_path, session_id="nb1", execution_count=1, code="print(1)", files_read=cell_res.files_read, files_written=[], skill_calls=cell_res.skill_calls)
    ledger_jsonl = tmp_path / ".omicsclaw" / "provenance" / "kernel_ledger.jsonl"
    assert ledger_jsonl.is_file()
    recorded = json.loads(ledger_jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert recorded["files_read"][0]["path"] == "data/input.csv"
    assert recorded["skill_calls"][0]["function"] == "compute_metric"


def test_c3_review_archive_supports_notebook_provenance_and_blocks_tampering(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "input.csv").write_text("gene,val\nG1,1.0\n", encoding="utf-8")
    cell_code = (
        "from pathlib import Path\n"
        "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/tables/res.csv').write_text('gene,val\\nG1,1.0\\n', encoding='utf-8')\n"
    )
    _seed_completed_notebook(tmp_path, "default", cell_code)
    (tmp_path / "output" / "tables").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / "tables" / "res.csv").write_text("gene,val\nG1,1.0\n", encoding="utf-8")
    (tmp_path / "output" / "REPORT.md").write_text("# QC Report\nG1 is 1.0\n", encoding="utf-8")

    export_and_replay("default", tmp_path, module="01_qc")

    archive = ReviewArchive(tmp_path, "01_qc")
    saved_rel = archive.save("VERDICT: APPROVE\nVerified clean-room replay and statistical checks.")
    assert (tmp_path / saved_rel).is_file()

    archive2 = ReviewArchive(tmp_path, "01_qc")
    (tmp_path / "output" / "tables" / "res.csv").write_text("gene,val\nG1,999.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        archive2.save("VERDICT: APPROVE\nShould fail due to tamper.")


def test_c4_shadow_review_and_subagent_prompts_and_skill_kernels(tmp_path: Path) -> None:
    for check in REVIEW_CHECKS:
        assert check in MODULE_REVIEWER_PROMPT
    assert "pseudoreplication" in MODULE_REVIEWER_PROMPT.lower()
    assert "double dipping" in MODULE_REVIEWER_PROMPT.lower()

    (tmp_path / "output" / "tables").mkdir(parents=True)
    (tmp_path / "output" / "tables" / "de.csv").write_text(
        "gene,log2fc,padj\nCD3D,2.4,0.0001\nMS4A1,1.9,0.002\n", encoding="utf-8"
    )
    (tmp_path / "provenance").mkdir(parents=True)
    (tmp_path / "provenance" / "replay_receipt.json").write_text(
        json.dumps({"status": "ok", "verified_outputs": [{"path": "output/tables/de.csv", "sha256": "sha"}]}),
        encoding="utf-8",
    )
    (tmp_path / "provenance" / "review_brief.md").write_text(
        "# Review Brief\nAll outputs verified.\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "output" / "REPORT.md"
    report_path.write_text("# Report\nCD3D log2fc is 2.4.\n", encoding="utf-8")

    rec = SimpleNamespace(kind="table", title="DE", path=str(tmp_path / "output" / "tables" / "de.csv"), size=42)
    prompt = _review_prompt("sess-1", [rec], [])
    assert len(prompt.encode("utf-8")) < 16_000
    assert "CD3D,2.4,0.0001" in prompt
    assert "Pseudoreplication" in prompt
    assert "replay_receipt.json" in prompt
    assert "review_brief.md" in prompt

    repo_root = Path(__file__).resolve().parents[2]
    sc_spec = importlib.util.spec_from_file_location(
        "sc_markers_kernel", repo_root / "skills" / "singlecell" / "scrna" / "sc-markers" / "kernel.py"
    )
    assert sc_spec and sc_spec.loader
    sc_mod = importlib.util.module_from_spec(sc_spec)
    sc_spec.loader.exec_module(sc_mod)
    assert callable(getattr(sc_mod, "check_homogeneous_null", None))

    sp_spec = importlib.util.spec_from_file_location(
        "spatial_de_kernel", repo_root / "skills" / "spatial" / "spatial-de" / "kernel.py"
    )
    assert sp_spec and sp_spec.loader
    sp_mod = importlib.util.module_from_spec(sp_spec)
    sp_spec.loader.exec_module(sp_mod)
    assert callable(getattr(sp_mod, "spatial_pseudobulk", None))
    try:
        import anndata as ad
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(42)
        # 1. Homogeneous null markers_df (low LFC / pct gap from over-clustering)
        null_df = pd.DataFrame(
            {
                "names": ["G1", "G2"],
                "pvals_adj": [0.001, 0.02],
                "logfoldchanges": [0.3, 0.4],
                "pct_nz_group": [0.50, 0.45],
                "pct_nz_reference": [0.42, 0.40],
            }
        )
        null_res = sc_mod.check_homogeneous_null(null_df)
        assert null_res["is_homogeneous"] is True
        assert null_res["recommendation"] == "abstain_single_cluster"

        # 2. Spatial multi-slide AnnData -> spatial_pseudobulk aggregates to slide level
        x_sp = rng.poisson(3.0, size=(40, 12)).astype(float)
        obs_sp = pd.DataFrame(
            {
                "slide_id": ["s1"] * 10 + ["s2"] * 10 + ["s3"] * 10 + ["s4"] * 10,
                "condition": ["ctrl"] * 20 + ["case"] * 20,
            },
            index=[f"spot{i}" for i in range(40)],
        )
        adata_sp = ad.AnnData(X=x_sp, obs=obs_sp)
        counts_df, meta_df = sp_mod.spatial_pseudobulk(
            adata_sp, sample_key="slide_id", condition_key="condition"
        )
        assert len(counts_df) == 4
        assert len(meta_df) == 4
    except ImportError:
        pass


def test_c5_openai_provider_reasoning_effort_withdraws_default_temperature() -> None:
    cfg = ProviderConfig(provider="openai", api_key="sk-test", model="gpt-5.4").with_overrides(
        reasoning_effort="high"
    )
    assert cfg.extra.get("reasoning_effort") == "high"

    resolved = resolve_config(
        "openai",
        env={"OPENAI_API_KEY": "sk-test", "OMICSCLAW_REASONING_EFFORT": "medium"},
    )
    assert resolved.extra.get("reasoning_effort") == "medium"

    provider = OpenAIProvider(cfg)
    payload = provider._request_payload([Message(role=Role.USER, content="hi")], None, stream=False)
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload


def test_c1_export_and_replay_multiturn_regeneration_nondefault_session_and_symlinks(tmp_path: Path) -> None:
    # 1. External data referenced via relative symlink inside workspace/data
    ext_dir = tmp_path / "ext_data"
    ext_dir.mkdir(parents=True)
    (ext_dir / "in.csv").write_text("gene,val\nG1,10\n", encoding="utf-8")

    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    import os

    rel_target = os.path.relpath(ext_dir / "in.csv", ws / "data")
    (ws / "data" / "in.csv").symlink_to(rel_target)

    (ws / "output" / "tables").mkdir(parents=True)
    (ws / "output" / "tables" / "out.csv").write_text("gene,val\nG1,10\n", encoding="utf-8")
    (ws / ".omicsclaw").mkdir(parents=True)
    db = Database(ws / ".omicsclaw" / "notebooks.db")
    store = NotebookStore(db)

    # Seed under non-default CLI session ID ("cli-sess-99") + an interleaved failed cell
    c1 = NotebookCell(
        notebook_id="cli-sess-99",
        cell_id="c1",
        source=(
            "from pathlib import Path\n"
            "raw = Path('data/in.csv').read_text(encoding='utf-8')\n"
            "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
            "Path('output/tables/out.csv').write_text(raw, encoding='utf-8')\n"
        ),
        status="completed",
        execution_id="e1",
        output={
            "files_read": [{"path": "data/in.csv"}],
            "files_written": [{"path": "output/tables/out.csv"}],
        },
    )
    c_bad = NotebookCell(
        notebook_id="cli-sess-99",
        cell_id="c_bad",
        source="raise RuntimeError('transient failure in exploration')\n",
        status="failed",
        execution_id="e_bad",
    )
    db.run(lambda c: store._put_cell(c, c1, 0))
    db.run(lambda c: store._put_cell(c, c_bad, 0))

    # Turn 1 export_and_replay called with "default" -> falls back to cli-sess-99, skips failed cell, resolves symlink
    r1 = export_and_replay("default", ws)
    assert r1["status"] == "ok"
    assert len(r1["steps"]) == 1

    # Turn 2: agent updates cell c1 (or adds c2) to write new output value
    c2 = NotebookCell(
        notebook_id="cli-sess-99",
        cell_id="c2",
        source="from pathlib import Path\nPath('output/tables/out.csv').write_text('gene,val\\nG1,99\\n', encoding='utf-8')\n",
        status="completed",
        execution_id="e2",
        output={"files_written": [{"path": "output/tables/out.csv"}]},
    )
    db.run(lambda c: store._put_cell(c, c2, 0))
    db.close()
    (ws / "output" / "tables" / "out.csv").write_text("gene,val\nG1,99\n", encoding="utf-8")

    # Turn 2 export_and_replay must overwrite output/reproduce.py and pass replay
    r2 = export_and_replay("default", ws)
    assert r2["status"] == "ok"
    assert len(r2["steps"]) == 2
    assert "99" in (ws / "output" / "reproduce.py").read_text(encoding="utf-8")


def test_c3_review_archive_rejects_post_replay_report_tamper_and_orphan_outputs(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "in.csv").write_text("gene,val\nG1,1.0\n", encoding="utf-8")
    cell_code = (
        "from pathlib import Path\n"
        "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/tables/res.csv').write_text('gene,val\\nG1,1.0\\n', encoding='utf-8')\n"
    )
    _seed_completed_notebook(tmp_path, "default", cell_code)
    (tmp_path / "output" / "tables").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / "tables" / "res.csv").write_text("gene,val\nG1,1.0\n", encoding="utf-8")
    (tmp_path / "output" / "REPORT.md").write_text("# QC Report\nG1 is 1.0\n", encoding="utf-8")

    export_and_replay("default", tmp_path, module="01_qc")

    # 1. Tamper REPORT.md after replay before ReviewArchive is constructed -> must raise ValueError
    (tmp_path / "output" / "REPORT.md").write_text("# QC Report\nFABRICATED CLAIM\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report modified since replay"):
        ReviewArchive(tmp_path, "01_qc")

    # Restore REPORT.md
    (tmp_path / "output" / "REPORT.md").write_text("# QC Report\nG1 is 1.0\n", encoding="utf-8")
    assert ReviewArchive(tmp_path, "01_qc") is not None

    # 2. Create untracked orphan file in output/tables after replay -> must raise ValueError
    (tmp_path / "output" / "tables" / "orphan.csv").write_text("gene\nORPHAN\n", encoding="utf-8")
    with pytest.raises(ValueError, match="untracked orphan outputs"):
        ReviewArchive(tmp_path, "01_qc")


def test_sidecar_injection_clean_room_replay_end_to_end(tmp_path: Path) -> None:
    ws = tmp_path / "ws_sidecar"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "counts.csv").write_text("donor_id,val\nd1,4\nd1,6\nd2,10\n", encoding="utf-8")
    skill_dir = ws / "skills" / "demo-pseudobulk"
    skill_dir.mkdir(parents=True)
    kernel_py = skill_dir / "kernel.py"
    kernel_py.write_text(
        "__all__ = ['aggregate_pseudobulk']\n"
        "def aggregate_pseudobulk(rows):\n"
        "    out = {}\n"
        "    for d, v in rows:\n"
        "        out[d] = out.get(d, 0) + int(v)\n"
        "    return out\n",
        encoding="utf-8",
    )

    bootstrap = build_sidecar_bootstrap_code([("demo-pseudobulk", kernel_py)], workspace=ws)
    assert (ws / "provenance" / "sidecars" / "demo-pseudobulk" / "kernel.py").is_file()
    assert (ws / "provenance" / "skills_loaded.json").is_file()

    ns: dict[str, object] = {}
    exec(bootstrap, ns)  # noqa: S102
    agg_fn = ns["aggregate_pseudobulk"]
    assert callable(agg_fn)
    assert agg_fn([("d1", 4), ("d1", 6), ("d2", 10)]) == {"d1": 10, "d2": 10}

    # Cell calls aggregate_pseudobulk directly WITHOUT importing it
    cell_code = (
        "from pathlib import Path\n"
        "import json\n"
        "lines = Path('data/counts.csv').read_text(encoding='utf-8').strip().splitlines()[1:]\n"
        "rows = [line.split(',') for line in lines]\n"
        "pb = aggregate_pseudobulk(rows)\n"
        "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
        "Path('output/tables/pb.csv').write_text('donor_id,sum\\n' + '\\n'.join(f'{k},{v}' for k, v in sorted(pb.items())) + '\\n', encoding='utf-8')\n"
        "Path('output/results.json').write_text(json.dumps({'n_donors': len(pb)}), encoding='utf-8')\n"
    )
    (ws / ".omicsclaw").mkdir(parents=True, exist_ok=True)
    db = Database(ws / ".omicsclaw" / "notebooks.db")
    store = NotebookStore(db)
    cell = NotebookCell(
        notebook_id="default",
        cell_id="c1",
        source=cell_code,
        status="completed",
        execution_id="exec-sc-1",
        output={
            "files_read": [{"path": "data/counts.csv"}],
            "files_written": [{"path": "output/tables/pb.csv"}, {"path": "output/results.json"}],
            "skill_calls": list(ns["__omicsclaw_skill_ledger__"]),  # type: ignore[arg-type]
        },
    )
    db.run(lambda c: store._put_cell(c, cell, 0))
    db.close()

    (ws / "output" / "tables").mkdir(parents=True, exist_ok=True)
    (ws / "output" / "tables" / "pb.csv").write_text("donor_id,sum\nd1,10\nd2,10\n", encoding="utf-8")
    (ws / "output" / "results.json").write_text(json.dumps({"n_donors": 2}), encoding="utf-8")

    receipt = export_and_replay("default", ws)
    assert receipt["status"] == "ok"
    assert receipt["replay_verified"] is True
    assert "__omicsclaw_register_sidecar__" in (ws / "output" / "reproduce.py").read_text(encoding="utf-8")


def _delegate_to_de_check_env_if_needed(test_name: str) -> bool:
    import subprocess
    try:
        import anndata  # noqa: F401
        return False
    except ImportError:
        alt_py = Path("/tmp/omicsclaw-de-check-env/bin/python")
        if alt_py.is_file():
            import os
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", CUDA_VISIBLE_DEVICES="")
            res = subprocess.run(
                [str(alt_py), "-m", "pytest", f"{__file__}::{test_name}", "-q"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            assert res.returncode == 0, f"Delegated test {test_name} failed:\n{res.stdout}\n{res.stderr}"
            return True
        pytest.skip("anndata not installed")
        return True


def test_h5ad_clean_room_replay_catches_corrupted_matrix(tmp_path: Path) -> None:
    if _delegate_to_de_check_env_if_needed("test_h5ad_clean_room_replay_catches_corrupted_matrix"):
        return
    import anndata as ad
    import numpy as np
    import pandas as pd
    from omicsclaw.runs.verification import (
        compare_h5ad_equivalence,
        verify_h5ad_equivalence,
    )

    obs = pd.DataFrame({"donor_id": ["d1"] * 5 + ["d2"] * 5}, index=[f"c{i}" for i in range(10)])
    var = pd.DataFrame({"gene": [f"G{j}" for j in range(4)]}, index=[f"G{j}" for j in range(4)])

    p_ones = tmp_path / "ones.h5ad"
    p_ones_copy = tmp_path / "ones_copy.h5ad"
    p_zeros = tmp_path / "zeros.h5ad"
    ad.AnnData(X=np.ones((10, 4), dtype=float), obs=obs, var=var, uns={"method": "pb"}).write_h5ad(p_ones)
    ad.AnnData(X=np.ones((10, 4), dtype=float), obs=obs, var=var, uns={"method": "pb"}).write_h5ad(p_ones_copy)
    ad.AnnData(X=np.zeros((10, 4), dtype=float), obs=obs, var=var, uns={"method": "pb"}).write_h5ad(p_zeros)

    ok_same, detail_same = compare_h5ad_equivalence(p_ones, p_ones_copy)
    assert ok_same is True, detail_same
    assert verify_h5ad_equivalence(p_ones, p_ones_copy).verdict == Verdict.PASS

    ok, detail = compare_h5ad_equivalence(p_ones, p_zeros)
    assert ok is False
    assert "X mismatch" in detail
    assert verify_h5ad_equivalence(p_ones, p_zeros).verdict == Verdict.FAIL

    # End-to-end export_and_replay when replay reproduces the identical .h5ad matrix -> status == "ok"
    ws_ok = tmp_path / "ws_h5ad_ok"
    (ws_ok / "output").mkdir(parents=True)
    (ws_ok / ".omicsclaw").mkdir(parents=True)
    ad.AnnData(X=np.ones((10, 4), dtype=float), obs=obs, var=var, uns={"method": "pb"}).write_h5ad(
        ws_ok / "output" / "processed.h5ad"
    )
    good_cell_code = (
        "import anndata as ad, numpy as np, pandas as pd\n"
        "obs = pd.DataFrame({'donor_id': ['d1']*5 + ['d2']*5}, index=[f'c{i}' for i in range(10)])\n"
        "var = pd.DataFrame({'gene': [f'G{j}' for j in range(4)]}, index=[f'G{j}' for j in range(4)])\n"
        "ad.AnnData(X=np.ones((10, 4), dtype=float), obs=obs, var=var, uns={'method': 'pb'}).write_h5ad('output/processed.h5ad')\n"
    )
    db_ok = Database(ws_ok / ".omicsclaw" / "notebooks.db")
    store_ok = NotebookStore(db_ok)
    cell_ok = NotebookCell(
        notebook_id="default",
        cell_id="c-h5-ok",
        source=good_cell_code,
        status="completed",
        execution_id="exec-h5-ok",
        output={
            "files_read": [],
            "files_written": [{"path": "output/processed.h5ad"}],
        },
    )
    db_ok.run(lambda c: store_ok._put_cell(c, cell_ok, 0))
    db_ok.close()

    receipt_ok = export_and_replay("default", ws_ok)
    assert receipt_ok["status"] == "ok"
    assert receipt_ok["replay_verified"] is True

    # End-to-end export_and_replay when replay produces a corrupted (all-zeros) .h5ad matrix -> status == "failed"
    ws_bad = tmp_path / "ws_h5ad_bad"
    (ws_bad / "output").mkdir(parents=True)
    (ws_bad / ".omicsclaw").mkdir(parents=True)
    ad.AnnData(X=np.ones((10, 4), dtype=float), obs=obs, var=var, uns={"method": "pb"}).write_h5ad(
        ws_bad / "output" / "processed.h5ad"
    )
    bad_cell_code = (
        "import anndata as ad, numpy as np, pandas as pd\n"
        "obs = pd.DataFrame({'donor_id': ['d1']*5 + ['d2']*5}, index=[f'c{i}' for i in range(10)])\n"
        "var = pd.DataFrame({'gene': [f'G{j}' for j in range(4)]}, index=[f'G{j}' for j in range(4)])\n"
        "ad.AnnData(X=np.zeros((10, 4), dtype=float), obs=obs, var=var, uns={'method': 'pb'}).write_h5ad('output/processed.h5ad')\n"
    )
    db_bad = Database(ws_bad / ".omicsclaw" / "notebooks.db")
    store_bad = NotebookStore(db_bad)
    cell_bad = NotebookCell(
        notebook_id="default",
        cell_id="c-h5-bad",
        source=bad_cell_code,
        status="completed",
        execution_id="exec-h5-bad",
        output={
            "files_read": [],
            "files_written": [{"path": "output/processed.h5ad"}],
        },
    )
    db_bad.run(lambda c: store_bad._put_cell(c, cell_bad, 0))
    db_bad.close()

    receipt_bad = export_and_replay("default", ws_bad)
    assert receipt_bad["status"] == "failed"
    assert receipt_bad["replay_verified"] is False
    assert "output/processed.h5ad" in receipt_bad["changed_outputs"]


def test_c3_shadow_review_inspects_tables_and_provenance(tmp_path: Path) -> None:
    ws = tmp_path / "ws_c3"
    (ws / "output" / "tables").mkdir(parents=True)
    de_csv = ws / "output" / "tables" / "de.csv"
    de_csv.write_text("gene,log2fc,padj\nCD3D,2.4,0.0001\n", encoding="utf-8")
    (ws / "provenance").mkdir(parents=True)
    (ws / "provenance" / "replay_receipt.json").write_text(
        json.dumps({"status": "ok", "verified_outputs": [{"path": "output/tables/de.csv", "sha256": "abc"}]}),
        encoding="utf-8",
    )
    (ws / "provenance" / "review_brief.md").write_text(
        "# Review Brief\nVERIFIED_3_LEVEL_PROVENANCE\n",
        encoding="utf-8",
    )
    rec = SimpleNamespace(kind="table", title="DE", path=str(de_csv), size=32)
    prompt = _review_prompt("sess-c3", [rec], [])
    assert "replay_receipt.json" in prompt
    assert "review_brief.md" in prompt
    assert "VERIFIED_3_LEVEL_PROVENANCE" in prompt


def test_shadow_review_finds_provenance_at_nested_results_depth(tmp_path: Path) -> None:
    from omicsclaw.entry.shadow_review import _extract_artifact_evidence

    ws = tmp_path / "ws_nested"
    (ws / "provenance").mkdir(parents=True)
    (ws / "provenance" / "review_brief.md").write_text(
        "# Review Brief\nUNIQUE_NESTED_PROVENANCE_MARKER\n", encoding="utf-8"
    )
    nested_table = ws / "results" / "01_qc" / "tables" / "qc.csv"
    nested_table.parent.mkdir(parents=True)
    nested_table.write_text("metric,val\nn_cells,500\n", encoding="utf-8")

    rec = SimpleNamespace(kind="table", title="QC", path=str(nested_table), size=25)
    evidence = _extract_artifact_evidence([rec], max_bytes=12_000)
    assert any("UNIQUE_NESTED_PROVENANCE_MARKER" in s for s in evidence)


def test_notebook_service_ledger_join_and_h5ad_read_capture(tmp_path: Path) -> None:
    if _delegate_to_de_check_env_if_needed("test_notebook_service_ledger_join_and_h5ad_read_capture"):
        return
    import anndata as ad
    import numpy as np
    import pandas as pd
    from omicsclaw.kernel.session import SessionKernel

    ws = tmp_path / "ws_ledger_h5ad"
    (ws / "data").mkdir(parents=True)
    obs = pd.DataFrame({"donor_id": ["d1", "d2"]}, index=["c1", "c2"])
    var = pd.DataFrame({"g": ["G1", "G2"]}, index=["G1", "G2"])
    ad.AnnData(X=np.array([[1.0, 2.0], [3.0, 4.0]]), obs=obs, var=var).write_h5ad(ws / "data" / "test.h5ad")

    kernel = SessionKernel(workspace=ws, session_id="nb-h5")
    try:
        kernel.start()
        cell_source = (
            "import anndata as ad\n"
            "import h5py\n"
            "from pathlib import Path\n"
            "adata = ad.read_h5ad('data/test.h5ad')\n"
            "with h5py.File('data/test.h5ad', 'r') as f:\n"
            "    keys = sorted(list(f.keys()))\n"
            "Path('output/tables').mkdir(parents=True, exist_ok=True)\n"
            "Path('output/tables/h5_keys.csv').write_text('key\\n' + '\\n'.join(keys) + f'\\nshape_{adata.n_obs}\\n', encoding='utf-8')\n"
        )
        res = kernel.execute(cell_source, execution_id="exec-h5-1")
        assert res.error is None
        assert any(item["path"] == "data/test.h5ad" for item in res.files_read)
        ledger_path = ws / "provenance" / "session_ledger.jsonl"
        assert ledger_path.is_file()
        ledger_records = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            any(r.get("path") == "data/test.h5ad" for r in rec.get("files_read", []))
            for rec in ledger_records
        )

        # Store cell with trailing whitespace and empty output dict to test code_sha256 strip + execution_id ledger join
        (ws / ".omicsclaw").mkdir(parents=True, exist_ok=True)
        db = Database(ws / ".omicsclaw" / "notebooks.db")
        store = NotebookStore(db)
        cell = NotebookCell(
            notebook_id="nb-h5",
            cell_id="c-h5",
            source=cell_source + "\n   \n",
            status="completed",
            execution_id="exec-h5-1",
            output={},
        )
        db.run(lambda c: store._put_cell(c, cell, 0))
        db.close()

        receipt = export_and_replay("nb-h5", ws)
        assert receipt["status"] == "ok"
        assert receipt["replay_verified"] is True
        assert any(inp["path"] == "data/test.h5ad" for inp in receipt["inputs"])
    finally:
        kernel.shutdown()


def test_spatial_pseudobulk_empty_when_all_slides_below_min_spots() -> None:
    if _delegate_to_de_check_env_if_needed("test_spatial_pseudobulk_empty_when_all_slides_below_min_spots"):
        return
    import anndata as ad
    import numpy as np
    import pandas as pd

    repo_root = Path(__file__).resolve().parents[2]
    sp_spec = importlib.util.spec_from_file_location(
        "spatial_de_kernel_empty", repo_root / "skills" / "spatial" / "spatial-de" / "kernel.py"
    )
    assert sp_spec and sp_spec.loader
    sp_mod = importlib.util.module_from_spec(sp_spec)
    sp_spec.loader.exec_module(sp_mod)

    adata_sp = ad.AnnData(
        X=np.ones((6, 3), dtype=float),
        obs=pd.DataFrame(
            {"slide_id": ["s1"] * 3 + ["s2"] * 3, "condition": ["ctrl"] * 3 + ["case"] * 3},
            index=[f"spot{i}" for i in range(6)],
        ),
    )
    counts_df, meta_df = sp_mod.spatial_pseudobulk(
        adata_sp, sample_key="slide_id", condition_key="condition", min_spots=9999
    )
    assert counts_df.empty
    assert meta_df.empty
    assert list(meta_df.columns) == ["condition", "n_spots"]


def test_export_and_replay_auto_opens_jobs_db_when_artifact_store_is_none(tmp_path: Path) -> None:
    from omicsclaw.memory.artifacts import ArtifactRecord, ArtifactStore

    ws = tmp_path / "ws_jobs_db"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "in.csv").write_text("gene,val\nG1,5\n", encoding="utf-8")
    (ws / "custom_out").mkdir(parents=True)
    custom_csv = ws / "custom_out" / "summary.csv"
    custom_csv.write_text("gene,val\nG1,5\n", encoding="utf-8")

    cell_code = (
        "from pathlib import Path\n"
        "raw = Path('data/in.csv').read_text(encoding='utf-8')\n"
        "Path('custom_out').mkdir(parents=True, exist_ok=True)\n"
        "Path('custom_out/summary.csv').write_text(raw, encoding='utf-8')\n"
    )
    _seed_completed_notebook(ws, "default", cell_code)
    jobs_db = Database(ws / ".omicsclaw" / "jobs.db")
    art_store = ArtifactStore(jobs_db)
    art_store.insert_artifact(
        ArtifactRecord(
            id="art-custom-1",
            job_id="job-1",
            session_id="default",
            kind="table",
            title="Custom Summary",
            path=str(custom_csv),
        )
    )
    jobs_db.close()

    receipt = export_and_replay("default", ws, artifact_store=None)
    assert receipt["status"] == "ok"
    assert any(v["path"] == "custom_out/summary.csv" for v in receipt["verified_outputs"])
