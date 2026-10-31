"""End-to-end verification of all 6 Claude Science v0.1.60 optimizations in OmicsClaw."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from omicsclaw.entry.notebook.service import (
    compute_backward_artifact_slice,
    export_and_replay,
    export_and_trace,
    slice_for_artifact,
)
from omicsclaw.entry.review import ReviewArchive
from omicsclaw.entry.shadow_review import (
    MAX_CONSECUTIVE_BOUNCES,
    SHADOW_REVIEW_CELL_THRESHOLD,
    ShadowReview,
)
from omicsclaw.entry.subagent import MODULE_REVIEWER_PROMPT
from omicsclaw.kernel.session import (
    SessionKernel,
    _resolve_kernel_code_markers,
)
from omicsclaw.memory.artifacts import ArtifactStore
from omicsclaw.memory.database import Database
from omicsclaw.runs.models import Verdict
from omicsclaw.runs.verification import (
    is_valid_artifact_id,
    resolve_artifact_reference,
    resolve_code_artifact_markers,
    trace_tabular_claim,
    unescape_output_path,
    verify_artifact_markers,
    verify_replay_receipt,
)
from omicsclaw.skills.sidecar import build_sidecar_bootstrap_code
from omicsclaw.tools._workspace import Workspace


class MockDataFrame:
    """Lightweight DataFrame for testing in environments with or without pandas."""

    def __init__(self, data: dict[str, list]):
        self._data = {k: list(v) for k, v in data.items()}

    @property
    def columns(self) -> list[str]:
        return list(self._data.keys())

    @property
    def index(self) -> list[int]:
        if not self._data:
            return []
        first_col = next(iter(self._data.values()))
        return list(range(len(first_col)))

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.index), len(self.columns))

    def __getitem__(self, key: str) -> list:
        return self._data[key]

    def __setitem__(self, key: str, val: list) -> None:
        self._data[key] = list(val)

    def to_dict(self, orient: str = "list") -> dict[str, list]:
        return {k: list(v) for k, v in self._data.items()}


class MockAnnData:
    """Lightweight AnnData for testing object versioning and sidecar assumption contracts."""

    def __init__(self, X: list[list[float]], obs: MockDataFrame, var: MockDataFrame):
        self.X = X
        self.obs = obs
        self.var = var
        self.uns: dict = {}
        self.obsm: dict = {}
        self.layers: dict = {}

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.X), len(self.X[0]) if self.X else 0)


class TestClaudeScienceOptimizations(unittest.TestCase):
    def test_1_artifact_pointer_grounding_and_escaping(self) -> None:
        """Test {{artifact:<id>}}, {{!artifact:<id>}} escaping, verify_artifact_markers, and trace_tabular_claim."""
        self.assertTrue(is_valid_artifact_id("art_1"))
        self.assertTrue(is_valid_artifact_id("v1"))
        self.assertTrue(is_valid_artifact_id("deg.csv"))
        self.assertTrue(is_valid_artifact_id("tables/deg.csv"))
        self.assertFalse(is_valid_artifact_id("../secret.csv"))
        self.assertFalse(is_valid_artifact_id("/etc/passwd"))
        self.assertFalse(is_valid_artifact_id("bad id"))
        self.assertFalse(is_valid_artifact_id(""))

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "output" / "tables").mkdir(parents=True, exist_ok=True)
            (ws / "provenance").mkdir(parents=True, exist_ok=True)
            csv_path = ws / "output" / "tables" / "deg.csv"
            csv_path.write_text("gene,log2FC,padj\nTP53,2.50,0.0004\nEGFR,-1.25,0.0210\n", encoding="utf-8")
            (ws / "provenance" / "artifacts.json").write_text(
                json.dumps({"art_1": "output/tables/deg.csv", "v1": "output/tables/deg.csv"}),
                encoding="utf-8",
            )

            # Resolve by explicit ID, relative path, and filename
            self.assertEqual(resolve_artifact_reference("art_1", ws), csv_path.resolve())
            self.assertEqual(resolve_artifact_reference("tables/deg.csv", ws), csv_path.resolve())
            self.assertEqual(resolve_artifact_reference("deg.csv", ws), csv_path.resolve())
            self.assertIsNone(resolve_artifact_reference("nonexistent_art", ws))

            # Code marker resolution and {{!artifact:...}} escaping
            code = 'df = read("{{artifact:tables/deg.csv}}"); literal = "{{!artifact:tables/deg.csv}}"'
            resolved_code = resolve_code_artifact_markers(code, ws)
            self.assertIn('df = read("output/tables/deg.csv")', resolved_code)
            self.assertIn('literal = "{{artifact:tables/deg.csv}}"', resolved_code)

            kernel_resolved = _resolve_kernel_code_markers(code, ws)
            self.assertIn('df = read("output/tables/deg.csv")', kernel_resolved)
            self.assertIn('literal = "{{artifact:tables/deg.csv}}"', kernel_resolved)

            # Report marker verification (both positional and keyword workspace)
            report_ok = ws / "REPORT.md"
            report_ok.write_text(
                "Top hit TP53 in {{artifact:art_1}} (also {{artifact:tables/deg.csv}}). Escaped: {{!artifact:raw_syntax}}.\n",
                encoding="utf-8",
            )
            v_ok = verify_artifact_markers(report_ok, ws)
            self.assertEqual(v_ok.verdict, Verdict.PASS)
            self.assertTrue(v_ok.passed)
            self.assertTrue(v_ok["passed"])
            self.assertEqual(v_ok["unresolved_markers"], [])
            self.assertEqual(unescape_output_path("Escaped: {{!artifact:art_1}}"), "Escaped: {{artifact:art_1}}")

            report_bad = ws / "BAD_REPORT.md"
            report_bad.write_text("Broken link {{artifact:missing_999}}.\n", encoding="utf-8")
            v_bad = verify_artifact_markers(report_bad, workspace=ws)
            self.assertEqual(v_bad.verdict, Verdict.FAIL)
            self.assertFalse(v_bad.passed)
            self.assertFalse(v_bad["passed"])
            self.assertIn("missing_999", v_bad["unresolved_markers"])

            # Tabular claim tracing (both positional expected_value and keyword expected_value)
            t_pass1 = trace_tabular_claim(
                ws,
                "tables/deg.csv",
                2.50,
                row_filter={"gene": "TP53"},
                value_column="log2FC",
                abs_tol=1e-3,
            )
            self.assertEqual(t_pass1.verdict, Verdict.PASS)
            self.assertTrue(t_pass1.passed)
            self.assertTrue(t_pass1["passed"])
            self.assertAlmostEqual(t_pass1["actual_value"], 2.50)

            t_pass2 = trace_tabular_claim(
                ws,
                "{{artifact:art_1}}",
                row_filter={"gene": "TP53"},
                target_column="padj",
                expected_value=0.0004,
                rtol=1e-2,
            )
            self.assertEqual(t_pass2.verdict, Verdict.PASS)
            self.assertTrue(t_pass2["passed"])

            t_fail = trace_tabular_claim(
                ws,
                "art_1",
                row_filter={"gene": "TP53"},
                target_column="log2FC",
                expected_value=9.99,
                atol=1e-3,
            )
            self.assertEqual(t_fail.verdict, Verdict.FAIL)
            self.assertFalse(t_fail.passed)
            self.assertFalse(t_fail["passed"])

    def test_2_sidecar_statistical_assumption_contract(self) -> None:
        """Test sidecar skill wrapping and statistical assumption extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            skill_dir = ws / "skills" / "sc_de"
            skill_dir.mkdir(parents=True, exist_ok=True)
            kernel_src = (
                "__all__ = ['run_de']\n"
                "def run_de(adata, *, group_by='condition', layer='counts', method='wilcoxon'):\n"
                "    return adata.obs\n"
            )
            (skill_dir / "kernel.py").write_text(kernel_src, encoding="utf-8")

            prelude = build_sidecar_bootstrap_code(
                "sc_de",
                skill_dir / "kernel.py",
                kernel_src,
                skills_root=ws / "skills",
                workspace=ws,
            )
            ns: dict = {}
            recorded_calls: list[dict] = []
            ns["__omicsclaw_record_skill_call__"] = recorded_calls.append
            exec(compile(prelude, "<sidecar_prelude>", "exec"), ns)

            obs = MockDataFrame(
                {
                    "donor_id": ["D1"] * 30 + ["D2"] * 30,
                    "condition": ["ctrl"] * 30 + ["stim"] * 30,
                    "padj": [0.001] * 10 + [0.5] * 50,
                }
            )
            var = MockDataFrame({"gene": ["G1", "G2", "G3"]})
            X = [[25, 0, 12] for _ in range(60)]
            adata = MockAnnData(X=X, obs=obs, var=var)

            res = ns["run_de"](adata, group_by="condition", layer="counts", method="deseq2")
            self.assertIs(res, obs)
            self.assertEqual(len(recorded_calls), 1)
            assumptions = recorded_calls[0]["assumptions"]
            self.assertEqual(assumptions["n_obs"], 60)
            self.assertEqual(assumptions["n_vars"], 3)
            self.assertIn("donor_id", assumptions["batch_keys"])
            self.assertEqual(assumptions["n_donors"], 2)
            self.assertEqual(assumptions["n_replicates"], 2)
            self.assertFalse(assumptions["pseudoreplication_risk"])
            self.assertTrue(assumptions["raw_counts_looks_integer"])
            self.assertEqual(assumptions["input_scale"], "raw_counts")
            self.assertEqual(assumptions["group_by"], "condition")
            self.assertEqual(assumptions["layer"], "counts")
            self.assertEqual(assumptions["method"], "deseq2")
            self.assertEqual(assumptions["n_tested"], 60)
            self.assertEqual(assumptions["n_significant"], 10)

            # Test pseudoreplication detection when only 1 donor across 60 cells
            obs_pseudo = MockDataFrame({"donor_id": ["D1"] * 60, "condition": ["ctrl"] * 30 + ["stim"] * 30})
            adata_pseudo = MockAnnData(X=X, obs=obs_pseudo, var=var)
            ns["run_de"](adata_pseudo, group_by="condition")
            self.assertTrue(recorded_calls[-1]["assumptions"]["pseudoreplication_risk"])
            self.assertEqual(recorded_calls[-1]["assumptions"]["n_donors"], 1)

    def test_3_backward_artifact_slice_and_service_slice(self) -> None:
        """Test deterministic backward program slicing across in-place mutations and dead-end cells."""
        steps = [
            {
                "cell_index": 0,
                "source": "df = make_df()",
                "reads": ["data/raw.csv"],
                "writes": [],
                "object_versions": [{"name": "df", "version": 1, "previous_version": 0, "type": "DataFrame"}],
            },
            {
                "cell_index": 1,
                "source": "dead_df = explore(df)",
                "reads": [],
                "writes": ["output/plots/explore.png"],
                "object_versions": [{"name": "dead_df", "version": 1, "previous_version": 0, "type": "DataFrame"}],
            },
            {
                "cell_index": 2,
                "source": "df['norm'] = normalize(df)",
                "reads": [],
                "writes": [],
                "object_versions": [{"name": "df", "version": 2, "previous_version": 1, "type": "DataFrame"}],
            },
            {
                "cell_index": 3,
                "source": "write_csv(df, 'output/tables/deg.csv')",
                "reads": [],
                "writes": ["output/tables/deg.csv"],
                "object_versions": [],
            },
        ]

        # Call with steps list, suffix path "tables/deg.csv"
        s1 = compute_backward_artifact_slice(steps, "tables/deg.csv")
        self.assertEqual(s1["kept_indices"], [0, 2, 3])
        self.assertEqual(s1["kept_cells"], [0, 2, 3])
        self.assertEqual(s1["pruned_indices"], [1])
        self.assertEqual(s1["pruned_cells"], [1])

        # Call with receipt dict
        s2 = compute_backward_artifact_slice({"steps": steps}, "output/tables/deg.csv")
        self.assertEqual(s2["kept_indices"], [0, 2, 3])

        # Call with workspace path and slice_for_artifact
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "provenance").mkdir(parents=True, exist_ok=True)
            (ws / "provenance" / "replay_receipt.json").write_text(
                json.dumps({"steps": steps}), encoding="utf-8"
            )
            s3 = compute_backward_artifact_slice(ws, "deg.csv")
            self.assertEqual(s3["kept_indices"], [0, 2, 3])

            slice_res = slice_for_artifact(ws, "output/tables/deg.csv")
            self.assertEqual(slice_res["kept_indices"], [0, 2, 3])
            self.assertEqual(slice_res["pruned_indices"], [1])
            self.assertIn("df = make_df()", slice_res["script"])
            self.assertIn("df['norm'] = normalize(df)", slice_res["script"])
            self.assertNotIn("dead_df = explore(df)", slice_res["script"])

    def test_4_live_session_kernel_ledger_object_versioning_and_host_sdk(self) -> None:
        """Test live ipykernel execution with 00_omicsclaw_ledger.py, object versioning, host.* SDK, and export_and_trace."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "output" / "tables").mkdir(parents=True, exist_ok=True)
            (ws / "provenance").mkdir(parents=True, exist_ok=True)

            # Seed a ShadowReview finding so we can test host.findings inside the live kernel
            sr = ShadowReview(workspace=ws)
            f_rec = sr._record_finding(
                session_id="sess_live",
                notebook_id="nb_live",
                cell_id="cell_0",
                execution_id="exec_0",
                severity="warning",
                category="pseudoreplication",
                summary="Verify donor replicate count >= 2",
                evidence="n_obs=60",
            )

            db = Database(ws / ".omicsclaw" / "jobs.db")
            art_store = ArtifactStore(db)

            kernel = SessionKernel(session_id="sess_live", workspace=ws)
            try:
                kernel.start()

                # Cell 1: Create a DataFrame-like object and write an output table
                code_1 = """
class SimpleDF:
    def __init__(self, data):
        self._data = {k: list(v) for k, v in data.items()}
    @property
    def columns(self):
        return list(self._data.keys())
    @property
    def shape(self):
        first = next(iter(self._data.values()), [])
        return (len(first), len(self._data))
    def to_dict(self):
        return {k: list(v) for k, v in self._data.items()}

df = SimpleDF({"gene": ["TP53", "EGFR"], "log2FC": [2.5, -1.2], "padj": [0.0004, 0.02]})
with open("output/tables/deg.csv", "w", encoding="utf-8") as fh:
    fh.write("gene,log2FC,padj\\nTP53,2.5,0.0004\\nEGFR,-1.2,0.02\\n")
"""
                res1 = kernel.execute(code_1, execution_id="exec_1")
                self.assertEqual(res1.status, "ok", f"Cell 1 failed: {res1.error}")
                self.assertIn("output/tables/deg.csv", res1.writes)
                df_v1 = [ov for ov in res1.object_versions if ov.get("name") == "df"]
                self.assertEqual(len(df_v1), 1)
                self.assertEqual(df_v1[0]["version"], 1)
                self.assertEqual(df_v1[0]["previous_version"], 0)
                self.assertEqual(df_v1[0]["prev_version"], 0)

                # Register artifact in ArtifactStore & provenance/artifacts.json
                from omicsclaw.memory.artifacts import ArtifactRecord, hash_and_size
                sha_deg, sz_deg = hash_and_size(ws / "output" / "tables" / "deg.csv")
                art_rec = ArtifactRecord(
                    id="art_deg_1",
                    session_id="sess_live",
                    kind="table",
                    path=str(ws / "output" / "tables" / "deg.csv"),
                    title="deg.csv",
                    mime="text/csv",
                    sha256=sha_deg,
                    size=sz_deg,
                    produced_by="exec_1",
                    meta={"version_id": "v1"},
                )
                art_store.insert_artifact(art_rec)
                (ws / "provenance" / "artifacts.json").write_text(
                    json.dumps({"v1": "output/tables/deg.csv", art_rec.id: "output/tables/deg.csv"}),
                    encoding="utf-8",
                )

                # Cell 2: Mutate df in-place -> df@v2, read {{artifact:tables/deg.csv}}, exercise host.* SDK
                code_2 = f"""
df._data["log2FC"] = [2.75, -1.2]
with open("{{{{artifact:tables/deg.csv}}}}", "r", encoding="utf-8") as fh:
    header = fh.readline().strip()
m_raw = host.artifact_marker("v1")
m_esc = host.artifact_marker("v1", escaped=True)
p_v1 = str(host.artifact_path("v1"))
open_f = host.findings(status="unresolved")
n_open = open_f["count"]
host.findings.mark_addressed({f_rec.id!r}, "Confirmed 2 biological donors")
print(f"MARKER={{m_raw}}|ESC={{m_esc}}|OPEN={{n_open}}|HEADER={{header}}")
"""
                res2 = kernel.execute(code_2, execution_id="exec_2")
                self.assertEqual(res2.status, "ok", f"Cell 2 failed: {res2.error}")
                self.assertIn("MARKER={{artifact:v1}}|ESC={{!artifact:v1}}|OPEN=1|HEADER=gene,log2FC,padj", res2.stdout)
                df_v2 = [ov for ov in res2.object_versions if ov.get("name") == "df"]
                self.assertEqual(len(df_v2), 1)
                self.assertEqual(df_v2[0]["version"], 2)
                self.assertEqual(df_v2[0]["previous_version"], 1)

                # Verify ShadowReview sees the finding marked as addressed on disk
                addressed = sr.findings_for("sess_live", status="addressed")
                self.assertEqual(len(addressed), 1)
                self.assertEqual(addressed[0].id, f_rec.id)
                self.assertEqual(addressed[0].resolution_note, "Confirmed 2 biological donors")

                # Write a valid REPORT.md with {{artifact:v1}}
                (ws / "REPORT.md").write_text(
                    "# Report\nDEG table stored at {{artifact:v1}}.\n", encoding="utf-8"
                )

                receipt_trace = export_and_trace("sess_live", ws)
                self.assertIn(receipt_trace["verification_mode"], ("trace", "ledger_trace"))
                self.assertIn(receipt_trace["mode"], ("trace", "ledger_trace"))
                self.assertFalse(receipt_trace["recomputed"])
                self.assertEqual(receipt_trace["returncode"], 0)
                deg_slice = receipt_trace.get("artifact_lineage", {}).get("output/tables/deg.csv", {})
                self.assertEqual(deg_slice.get("target_path"), "output/tables/deg.csv")
                self.assertIn("output/tables/deg.csv", deg_slice.get("synthesized_code", ""))
                self.assertIn("SimpleDF", deg_slice.get("script", ""))

                v_trace = verify_replay_receipt(
                    ws / "provenance" / "replay_receipt.json",
                    ws,
                    require_reexecution=False,
                )
                self.assertEqual(v_trace.verdict, Verdict.PASS, f"Trace verification failed: {v_trace.details}")

                # ReviewArchive(ws, "01_de") should succeed when REPORT.md has valid {{artifact:v1}}
                archive = ReviewArchive(ws, "01_de")
                self.assertIn("REPORT.md", archive.snapshot["files"])

                # Tamper with deg.csv and verify verify_replay_receipt catches mismatch
                orig_deg = (ws / "output" / "tables" / "deg.csv").read_text(encoding="utf-8")
                (ws / "output" / "tables" / "deg.csv").write_text("tampered\n", encoding="utf-8")
                v_tampered = verify_replay_receipt(
                    ws / "provenance" / "replay_receipt.json",
                    ws,
                    require_reexecution=False,
                )
                self.assertEqual(v_tampered.verdict, Verdict.FAIL)
                self.assertIn("modified", v_tampered.details)
                (ws / "output" / "tables" / "deg.csv").write_text(orig_deg, encoding="utf-8")

                # Test standalone export_and_replay(..., replay=True) re-executing reproduce.py
                receipt_replay = export_and_replay("sess_live", ws, replay=True)
                self.assertIn(receipt_replay["verification_mode"], ("replay", "recompute"))
                self.assertTrue(receipt_replay["recomputed"])
                v_replay = verify_replay_receipt(
                    ws / "provenance" / "replay_receipt.json",
                    ws,
                    require_reexecution=True,
                )
                self.assertEqual(v_replay.verdict, Verdict.PASS, f"Replay verification failed: {v_replay.details}")
            finally:
                kernel.shutdown()
                db.close()

    def test_5_shadow_review_convergence_breaker_and_subagent_prompt(self) -> None:
        """Test MAX_CONSECUTIVE_BOUNCES=3, SHADOW_REVIEW_CELL_THRESHOLD=40, and MODULE_REVIEWER_PROMPT."""
        self.assertEqual(MAX_CONSECUTIVE_BOUNCES, 3)
        self.assertEqual(SHADOW_REVIEW_CELL_THRESHOLD, 40)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            sr = ShadowReview(workspace=ws, min_interval_s=0.0)
            for _ in range(MAX_CONSECUTIVE_BOUNCES):
                self.assertFalse(sr.is_convergence_tripped("sess_1"))
                sr.record_bounce("sess_1")
            self.assertTrue(sr.is_convergence_tripped("sess_1"))
            sr.reset_bounces("sess_1")
            self.assertFalse(sr.is_convergence_tripped("sess_1"))

        self.assertIn("assumptions", MODULE_REVIEWER_PROMPT)
        self.assertIn("@v1 -> @v2", MODULE_REVIEWER_PROMPT)
        self.assertIn("{{artifact:<id>}}", MODULE_REVIEWER_PROMPT)

    def test_6_desktop_artifact_lineage_enrichment(self) -> None:
        """Test desktop GET /artifacts/{id} lineage enrichment with slice, object_versions, assumptions, and findings."""
        from omicsclaw.entry.desktop.artifacts import _enrich_claude_science_lineage
        from omicsclaw.memory.artifacts import ArtifactRecord

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "output" / "tables").mkdir(parents=True)
            (ws / "provenance").mkdir(parents=True)
            deg_path = ws / "output" / "tables" / "deg.csv"
            deg_path.write_text("gene,log2FC,padj\nTP53,2.5,0.0004\n", encoding="utf-8")
            (ws / "provenance" / "replay_receipt.json").write_text(
                json.dumps(
                    {
                        "verification_mode": "trace",
                        "object_versions": [
                            {"name": "adata", "version": 2, "previous_version": 1, "shape": [500, 200]}
                        ],
                        "assumption_checks": [
                            {"skill": "sc_de.run_de", "assumptions": {"input_scale": "raw_counts", "pseudoreplication_risk": False}}
                        ],
                        "artifact_lineage": {
                            "output/tables/deg.csv": {
                                "kept_indices": [0, 2],
                                "pruned_indices": [1],
                                "synthesized_code": "df.to_csv('output/tables/deg.csv')",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (ws / "provenance" / "findings.json").write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "id": "find_1",
                                "session_id": "sess_desktop",
                                "severity": "warn",
                                "category": "pseudoreplication",
                                "summary": "Check donor replicates",
                                "status": "addressed",
                                "resolution_note": "Verified 3 donors",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            record = ArtifactRecord(
                id="art_1",
                session_id="sess_desktop",
                kind="table",
                path=str(deg_path),
                title="deg.csv",
            )
            lineage: dict = {"job": None, "parents": []}
            _enrich_claude_science_lineage(record, lineage)
            self.assertEqual(lineage.get("verification_mode"), "trace")
            self.assertEqual(len(lineage.get("object_versions", [])), 1)
            self.assertEqual(lineage["object_versions"][0]["name"], "adata")
            self.assertEqual(lineage.get("artifact_slice", {}).get("kept_indices"), [0, 2])
            self.assertEqual(lineage.get("artifact_slice", {}).get("pruned_indices"), [1])
            self.assertEqual(len(lineage.get("findings", [])), 1)
            self.assertEqual(lineage["findings"][0]["resolution_note"], "Verified 3 donors")

    def test_7_desktop_notebook_slice_route_and_finding_feedback(self) -> None:
        """Test GET/POST /notebooks/{id}/slice and POST /notebooks/{id}/feedback with kind='finding_addressed'."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from omicsclaw.entry.desktop.notebooks import mount_notebook_routes
        from omicsclaw.entry.notebook.service import NotebookAnalysis

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            db = Database(ws / "notebooks.db")
            sr = ShadowReview(workspace=ws)
            f_rec = sr._record_finding(
                session_id="sess_nb",
                notebook_id="demo",
                cell_id="c1",
                execution_id="exec_1",
                severity="warn",
                category="pseudoreplication",
                summary="Pseudoreplication warning",
            )
            analysis = NotebookAnalysis(db, kernel=None)
            app = FastAPI()
            mount_notebook_routes(
                app,
                analysis,
                authorized=lambda _req: True,
                read_json=lambda req, _limit: req.json(),
            )
            client = TestClient(app)

            client.put("/notebooks/demo/cells/c1", json={"source": "df = load_raw()", "expected_revision": 0})
            client.put("/notebooks/demo/cells/c2", json={"source": "dead_plot = plot_qc()", "expected_revision": 0})
            client.put(
                "/notebooks/demo/cells/c3",
                json={"source": "df.to_csv('output/tables/deg.csv')", "expected_revision": 0},
            )

            res_get = client.get("/notebooks/demo/slice?target_path=output/tables/deg.csv")
            self.assertEqual(res_get.status_code, 200, res_get.text)
            sl_get = res_get.json()["slice"]
            self.assertEqual(sl_get["target_path"], "output/tables/deg.csv")
            self.assertEqual(sl_get["kept_cell_ids"], ["c1", "c3"])
            self.assertEqual(sl_get["pruned_cell_ids"], ["c2"])
            self.assertIn("df = load_raw()", sl_get["synthesized_code"])
            self.assertNotIn("dead_plot", sl_get["synthesized_code"])

            res_post = client.post("/notebooks/demo/slice", json={"target_path": "output/tables/deg.csv"})
            self.assertEqual(res_post.status_code, 200, res_post.text)
            self.assertEqual(res_post.json()["slice"]["kept_cell_ids"], ["c1", "c3"])

            res_fb = client.post(
                "/notebooks/demo/feedback",
                json={
                    "kind": "finding_addressed",
                    "payload": {
                        "finding_id": f_rec.id,
                        "resolution_note": "Aggregated to donor pseudobulk",
                        "workspace": str(ws),
                    },
                },
            )
            self.assertEqual(res_fb.status_code, 200, res_fb.text)
            self.assertEqual(res_fb.json()["feedback"]["kind"], "finding_addressed")
            addressed = sr.findings_for("sess_nb", status="addressed")
            self.assertEqual(len(addressed), 1)
            self.assertEqual(addressed[0].resolution_note, "Aggregated to donor pseudobulk")
            db.close()


    def test_format_cell_summary_emits_code_run_appendix_provenance(self):
        from omicsclaw.kernel.manager import CellResult, format_cell_summary
        import json

        res = CellResult(
            status="ok",
            stdout="Loaded PBMC\n",
            stderr="",
            error=None,
            figures=[],
            files_written=["output/pbmc_qc.h5ad"],
            files_read=[],
            skill_calls=[{"skill": "sc-qc", "assumptions": [{"check": "mt < 15%", "passed": True}]}],
            skills_loaded=[{"skill": "sc-qc", "module": "omicsclaw.skills.sc_qc"}],
            object_versions=[{"name": "adata", "version": 2, "summary": "AnnData 2700 x 1838"}],
            usage={"wall_s": 0.4, "cpu_s": 0.3, "peak_rss_kb": 102400},
        )
        summary = format_cell_summary(res)
        self.assertIn("stdout:\nLoaded PBMC", summary)
        self.assertIn("[code_run_appendix]", summary)
        appendix_raw = summary.split("[code_run_appendix]\n", 1)[1]
        appendix = json.loads(appendix_raw)
        self.assertEqual(appendix["object_versions"][0]["name"], "adata")
        self.assertEqual(appendix["object_versions"][0]["version"], 2)
        self.assertEqual(appendix["skill_calls"][0]["skill"], "sc-qc")
        self.assertEqual(appendix["skills_loaded"][0]["skill"], "sc-qc")
        self.assertEqual(appendix["files_written"], ["output/pbmc_qc.h5ad"])

    def test_compute_status_payload_filters_by_session_id(self):
        from types import SimpleNamespace
        from omicsclaw.entry.desktop.service import compute_status_payload
        import json

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            kdir = ws / ".omicsclaw" / "kernels"
            kdir.mkdir(parents=True, exist_ok=True)
            for sid, var_name in [("sess-A", "adata_a"), ("sess-B", "adata_b")]:
                sdir = kdir / sid
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "ledger.jsonl").write_text(
                    json.dumps({
                        "cell": 1,
                        "ts": 1700000000.0,
                        "object_versions": [{"name": var_name, "version": 1, "summary": "AnnData"}],
                        "skills_loaded": [
                            {"skill": f"skill-{sid}", "module": f"mod.{sid}"},
                            {"skill": "sc-shared", "module": "mod.shared"},
                        ],
                    }) + "\n"
                )
            fake_handles = SimpleNamespace(_handles={
                "h1": SimpleNamespace(adata_id="h1", session_id="sess-A", revision=[1], summary="AnnData A", synced_at=1.0),
                "h2": SimpleNamespace(adata_id="h2", session_id="sess-B", revision=[1], summary="AnnData B", synced_at=1.0),
            })
            fake_mgr = SimpleNamespace(
                handles=fake_handles,
                snapshot=lambda: [
                    {"session_id": "sess-A", "alive": True, "pid": 11111, "peak_rss_kb": 1000, "language": "python"},
                    {"session_id": "sess-B", "alive": True, "pid": 22222, "peak_rss_kb": 2000, "language": "python"},
                ],
            )
            fake_app = SimpleNamespace(
                config=SimpleNamespace(workspace=ws),
                kernels=SimpleNamespace(manager=fake_mgr),
            )
            all_payload = compute_status_payload(fake_app)
            self.assertEqual(all_payload["active_kernels_count"], 2)
            self.assertEqual(len(all_payload["handles"]), 2)

            scoped = compute_status_payload(fake_app, session_id="sess-A")
            self.assertEqual(scoped["active_kernels_count"], 1)
            self.assertEqual([k["session_id"] for k in scoped["kernels"]], ["sess-A"])
            self.assertEqual([h["adata_id"] for h in scoped["handles"]], ["h1"])
            self.assertEqual([o["name"] for o in scoped["object_versions"]], ["adata_a"])
            self.assertEqual([s["skill"] for s in scoped["skills_loaded"]], ["skill-sess-A", "sc-shared"])
            scoped_b = compute_status_payload(fake_app, session_id="sess-B")
            self.assertEqual([s["skill"] for s in scoped_b["skills_loaded"]], ["skill-sess-B", "sc-shared"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


