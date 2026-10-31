"""Evidence-chain and scientific-validity benchmark suite (E1--E4).

This module implements the four deterministic benchmark suites and graders for
evaluating agentic bioinformatics workflows without circularity:

- **E1 (:class:`E1ReplayFaithfulnessGrader`)**: Clean-room script replay,
  table/JSON artifact equivalence, and report claim-to-artifact faithfulness.
- **E2 (:class:`E2StalenessLedgerGrader`, :func:`run_e2_staleness_suite`)**:
  10-scenario deterministic tampering and staleness detection (`I1`--`I10`),
  including `sys.addaudithook` bypass reads and sidecar kernel hash checks.
- **E3 (:class:`E3ReviewerGradientGrader`, :func:`build_e3_reviewer_prompt`)**:
  5-level reviewer information gradient (`V1`--`V5`) and defect taxonomy
  (`A1`--`A4`, `B5`--`B9`) evaluation.
- **E4 (:class:`E4NullTwinDGPGrader`, :func:`calibrate_e4_suite`)**:
  Matched Null/Twin data-generating processes (Pseudoreplication, Double-Dipping,
  and Multi-Slide Spatial) with pre-agent statistical calibration gates (`V1`--`V4`).

In accordance with :mod:`omicsclaw.bench` isolation rules, this module imports
only the Python standard library and :mod:`omicsclaw.bench.grade`.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .grade import Control, Grade, Submission, self_check

__all__ = [
    "E1ReplayFaithfulnessGrader",
    "E2StalenessLedgerGrader",
    "E3ReviewerGradientGrader",
    "E4NullTwinDGPGrader",
    "build_e3_reviewer_prompt",
    "calibrate_e4_suite",
    "deterministic_statistical_audit",
    "generate_double_dipping_dgp",
    "generate_pseudorep_dgp",
    "generate_spatial_dgp",
    "run_e2_staleness_suite",
    "verify_provenance_bundle",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _floats_close(a: float, b: float, rtol: float = 1e-4, atol: float = 1e-5) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= (atol + rtol * abs(b))


def _compare_json_values(
    expected: Any,
    actual: Any,
    *,
    rtol: float = 1e-4,
    atol: float = 1e-5,
    ignore_keys: frozenset[str] = frozenset({"timestamp", "created_at", "elapsed_s", "execution_time_s"}),
    path: str = "$",
) -> list[str]:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return [f"{path}: expected number {expected!r}, got {actual!r}"]
        if not _floats_close(float(expected), float(actual), rtol=rtol, atol=atol):
            return [f"{path}: expected {expected}, got {actual}"]
        return []
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [f"{path}: expected object, got {type(actual).__name__}"]
        diffs: list[str] = []
        for k, v in expected.items():
            if k in ignore_keys:
                continue
            if k not in actual:
                diffs.append(f"{path}.{k}: missing key")
            else:
                diffs.extend(
                    _compare_json_values(
                        v, actual[k], rtol=rtol, atol=atol, ignore_keys=ignore_keys, path=f"{path}.{k}"
                    )
                )
        for k in actual:
            if k not in ignore_keys and k not in expected:
                diffs.append(f"{path}.{k}: unexpected extra key")
        return diffs
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if len(expected) != len(actual):
            return [f"{path}: list length {len(expected)} != {len(actual)}"]
        diffs = []
        for idx, (ev, av) in enumerate(zip(expected, actual)):
            diffs.extend(
                _compare_json_values(
                    ev, av, rtol=rtol, atol=atol, ignore_keys=ignore_keys, path=f"{path}[{idx}]"
                )
            )
        return diffs
    if expected != actual:
        return [f"{path}: {expected!r} != {actual!r}"]
    return []


def _compare_csv_files(
    expected_csv: Path,
    actual_csv: Path,
    *,
    rtol: float = 1e-4,
    atol: float = 1e-5,
    min_id_jaccard: float = 0.99,
) -> tuple[bool, str]:
    if not expected_csv.is_file() or not actual_csv.is_file():
        return False, "missing csv file"
    try:
        with expected_csv.open("r", encoding="utf-8", newline="") as f1:
            rows1 = list(csv.reader(f1))
        with actual_csv.open("r", encoding="utf-8", newline="") as f2:
            rows2 = list(csv.reader(f2))
    except Exception as exc:
        return False, f"csv read error: {exc}"
    if not rows1 or not rows2:
        return (rows1 == rows2), "empty csv mismatch"
    if rows1[0] != rows2[0]:
        return False, f"header mismatch: {rows1[0]} vs {rows2[0]}"
    body1, body2 = rows1[1:], rows2[1:]
    ids1 = {r[0] for r in body1 if r}
    ids2 = {r[0] for r in body2 if r}
    if ids1 or ids2:
        jaccard = len(ids1 & ids2) / max(len(ids1 | ids2), 1)
        if jaccard < min_id_jaccard:
            return False, f"row ID Jaccard {jaccard:.4f} < {min_id_jaccard}"
    if len(body1) != len(body2):
        return False, f"row count {len(body1)} != {len(body2)}"
    if ids1 == ids2 and len(ids1) == len(body1) and len(ids2) == len(body2):
        row2_map = {r[0]: r for r in body2 if r}
        paired_rows = [(r1, row2_map[r1[0]]) for r1 in body1 if r1]
    else:
        paired_rows = list(zip(body1, body2))
    for r_idx, (r1, r2) in enumerate(paired_rows, start=1):
        if len(r1) != len(r2):
            return False, f"row {r_idx} column count mismatch"
        for c_idx, (v1, v2) in enumerate(zip(r1, r2)):
            if v1 == v2:
                continue
            try:
                f1_val, f2_val = float(v1), float(v2)
            except ValueError:
                return False, f"row {r_idx} col {c_idx}: {v1!r} != {v2!r}"
            if not _floats_close(f1_val, f2_val, rtol=rtol, atol=atol):
                return False, f"row {r_idx} col {c_idx}: {f1_val} != {f2_val}"
    return True, "ok"


# ============================================================================
# E1: Clean-Room Replay & Claim-Artifact Faithfulness Grader
# ============================================================================


class E1ReplayFaithfulnessGrader:
    """Grades clean-room replayability, artifact equivalence, and report faithfulness."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        script_path = submission.workspace / "output" / "reproduce.py"
        results_path = submission.workspace / "output" / "results.json"
        report_path = submission.workspace / "output" / "REPORT.md"
        expected_path = oracle / "expected.json"

        if not script_path.is_file() or not results_path.is_file() or not report_path.is_file():
            return Grade(
                passed=False,
                score=0.0,
                metrics={
                    "clean_room_pass": False,
                    "artifact_equivalence": False,
                    "claim_faithfulness": False,
                },
                detail="missing required deliverables (reproduce.py, results.json, or REPORT.md)",
            )

        try:
            ws_results = json.loads(results_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")
            expected = (
                json.loads(expected_path.read_text(encoding="utf-8"))
                if expected_path.is_file()
                else {}
            )
        except Exception as exc:
            return Grade(
                passed=False,
                score=0.0,
                metrics={
                    "clean_room_pass": False,
                    "artifact_equivalence": False,
                    "claim_faithfulness": False,
                },
                detail=f"malformed JSON or text deliverable: {exc}",
            )

        # 1. Execute output/reproduce.py in a fresh clean-room directory
        clean_room_pass = False
        artifact_equivalence = False
        diff_reasons: list[str] = []

        with tempfile.TemporaryDirectory(prefix="e1-cleanroom-") as tmp:
            clean_root = Path(tmp)
            for data_src in (oracle / "data", submission.workspace / "data"):
                if data_src.is_dir():
                    (clean_root / "data").mkdir(parents=True, exist_ok=True)
                    for item in data_src.rglob("*"):
                        rel_item = item.relative_to(data_src)
                        dst_item = clean_root / "data" / rel_item
                        resolved = item.resolve()
                        if resolved.is_dir():
                            dst_item.mkdir(parents=True, exist_ok=True)
                        elif resolved.is_file():
                            dst_item.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(resolved, dst_item)
            (clean_root / "output").mkdir(parents=True, exist_ok=True)
            clean_script = clean_root / "output" / "reproduce.py"
            shutil.copy2(script_path, clean_script)

            import os

            env = dict(os.environ)
            repo_root = str(Path(__file__).resolve().parents[2])
            existing_py = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{repo_root}:{clean_root}" + (f":{existing_py}" if existing_py else "")

            proc = subprocess.run(
                [sys.executable, str(clean_script)],
                cwd=str(clean_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            clean_results_path = clean_root / "output" / "results.json"
            if proc.returncode == 0 and clean_results_path.is_file():
                clean_room_pass = True
            else:
                diff_reasons.append(
                    f"clean-room replay failed (exit={proc.returncode}): {proc.stderr.strip()[:160]}"
                )

            if clean_room_pass:
                try:
                    clean_results = json.loads(clean_results_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    clean_results = None
                    diff_reasons.append(f"clean-room results.json invalid: {exc}")

                if clean_results is not None:
                    json_diffs = _compare_json_values(clean_results, ws_results)
                    if json_diffs:
                        diff_reasons.extend(json_diffs)

                    ws_tables_dir = submission.workspace / "output" / "tables"
                    clean_tables_dir = clean_root / "output" / "tables"
                    table_names: set[str] = set(expected.get("tables", []))
                    if ws_tables_dir.is_dir():
                        for p in ws_tables_dir.glob("*.csv"):
                            table_names.add(p.name)
                    if clean_tables_dir.is_dir():
                        for p in clean_tables_dir.glob("*.csv"):
                            table_names.add(p.name)

                    for tname in sorted(table_names):
                        ws_t = ws_tables_dir / tname
                        cl_t = clean_tables_dir / tname
                        if not ws_t.is_file() or not cl_t.is_file():
                            diff_reasons.append(f"table {tname} missing in workspace or clean-room")
                            continue
                        ok, msg = _compare_csv_files(cl_t, ws_t)
                        if not ok:
                            diff_reasons.append(f"table {tname}: {msg}")

                    if not diff_reasons:
                        artifact_equivalence = True

        # 2. Verify Claim-to-Artifact Faithfulness (results.json + REPORT.md vs expected & self)
        claim_faithfulness = True
        claim_reasons: list[str] = []

        expected_claims = expected.get("claims", {})
        for key, exp_val in expected_claims.items():
            if key not in ws_results:
                claim_faithfulness = False
                claim_reasons.append(f"results.json missing claim {key!r}")
                continue
            actual_val = ws_results[key]
            if _compare_json_values(exp_val, actual_val):
                claim_faithfulness = False
                claim_reasons.append(f"results.json[{key!r}]={actual_val!r} != expected {exp_val!r}")

        for key, actual_val in ws_results.items():
            if isinstance(actual_val, (int, float)) and not isinstance(actual_val, bool):
                pattern = re.compile(
                    rf"\b{re.escape(key)}\b\s*[:=]\s*([-+]?\d+(?:\.\d+)?)",
                    re.IGNORECASE,
                )
                matches = pattern.findall(report_text)
                if not matches and key in expected_claims:
                    claim_faithfulness = False
                    claim_reasons.append(f"REPORT.md omits required numeric claim {key!r}")
                for m in matches:
                    rep_num = float(m)
                    if not _floats_close(rep_num, float(actual_val)):
                        claim_faithfulness = False
                        claim_reasons.append(
                            f"REPORT.md states {key}={rep_num}, contradicting results.json ({actual_val})"
                        )

        for gene in expected.get("top_genes", []):
            if gene not in report_text:
                claim_faithfulness = False
                claim_reasons.append(f"REPORT.md omits expected top gene {gene!r}")
            top_in_res = ws_results.get("top_genes", [])
            if isinstance(top_in_res, list) and gene not in top_in_res:
                claim_faithfulness = False
                claim_reasons.append(f"results.json top_genes omits {gene!r}")

        passed = bool(clean_room_pass and artifact_equivalence and claim_faithfulness)
        score = (
            (1.0 if clean_room_pass else 0.0)
            + (1.0 if artifact_equivalence else 0.0)
            + (1.0 if claim_faithfulness else 0.0)
        ) / 3.0
        reasons = diff_reasons + claim_reasons
        detail = "all E1 checks passed" if passed else "; ".join(reasons[:3])
        return Grade(
            passed=passed,
            score=round(score, 4),
            metrics={
                "clean_room_pass": clean_room_pass,
                "artifact_equivalence": artifact_equivalence,
                "claim_faithfulness": claim_faithfulness,
            },
            detail=detail,
        )

    def controls(self) -> Sequence[Control]:
        valid_script = (
            "from pathlib import Path\n"
            "import json\n"
            "out = Path('output')\n"
            "(out / 'tables').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'tables' / 'markers.csv').write_text('gene,score\\nCD3D,12.5\\nMS4A1,9.25\\n', encoding='utf-8')\n"
            "(out / 'results.json').write_text(json.dumps({'n_significant': 2, 'top_score': 12.5, 'top_genes': ['CD3D', 'MS4A1']}), encoding='utf-8')\n"
        )
        valid_table = "gene,score\nCD3D,12.5\nMS4A1,9.25\n"
        valid_results = json.dumps(
            {"n_significant": 2, "top_score": 12.5, "top_genes": ["CD3D", "MS4A1"]}
        )
        valid_report = (
            "# Marker Report\n\n"
            "- n_significant: 2\n"
            "- top_score: 12.5\n"
            "- Top markers: CD3D, MS4A1\n"
        )
        oracle_expected = json.dumps(
            {
                "tables": ["markers.csv"],
                "claims": {"n_significant": 2, "top_score": 12.5},
                "top_genes": ["CD3D", "MS4A1"],
            }
        )

        spatial_script = (
            "from pathlib import Path\n"
            "import json\n"
            "out = Path('output')\n"
            "(out / 'tables').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'tables' / 'domains.csv').write_text('gene,moran_i\\nSNAP25,0.68\\nMBP,0.61\\n', encoding='utf-8')\n"
            "(out / 'results.json').write_text(json.dumps({'n_domains': 4, 'top_moran_i': 0.68, 'top_genes': ['SNAP25', 'MBP']}), encoding='utf-8')\n"
        )
        spatial_table = "gene,moran_i\nSNAP25,0.68\nMBP,0.61\n"
        spatial_results = json.dumps(
            {"n_domains": 4, "top_moran_i": 0.68, "top_genes": ["SNAP25", "MBP"]}
        )
        spatial_report = (
            "# Spatial Domain Report\n\n"
            "- n_domains: 4\n"
            "- top_moran_i: 0.68\n"
            "- Spatial markers: SNAP25, MBP\n"
        )
        spatial_oracle = json.dumps(
            {
                "tables": ["domains.csv"],
                "claims": {"n_domains": 4, "top_moran_i": 0.68},
                "top_genes": ["SNAP25", "MBP"],
            }
        )

        return [
            Control(
                name="e1-pass-deterministic-marker-pipeline",
                expect_pass=True,
                deliverables={
                    "output/reproduce.py": valid_script,
                    "output/tables/markers.csv": valid_table,
                    "output/results.json": valid_results,
                    "output/REPORT.md": valid_report,
                },
                oracle={"expected.json": oracle_expected},
            ),
            Control(
                name="e1-pass-spatial-summary-pipeline",
                expect_pass=True,
                deliverables={
                    "output/reproduce.py": spatial_script,
                    "output/tables/domains.csv": spatial_table,
                    "output/results.json": spatial_results,
                    "output/REPORT.md": spatial_report,
                },
                oracle={"expected.json": spatial_oracle},
            ),
            Control(
                name="e1-fail-stale-in-memory-table",
                expect_pass=False,
                deliverables={
                    "output/reproduce.py": valid_script,
                    "output/tables/markers.csv": "gene,score\nCD3D,99.9\nFAKE1,50.0\n",
                    "output/results.json": valid_results,
                    "output/REPORT.md": valid_report,
                },
                oracle={"expected.json": oracle_expected},
            ),
            Control(
                name="e1-fail-hallucinated-report-claim",
                expect_pass=False,
                deliverables={
                    "output/reproduce.py": valid_script,
                    "output/tables/markers.csv": valid_table,
                    "output/results.json": valid_results,
                    "output/REPORT.md": "# Marker Report\n- n_significant: 48\n- top_score: 12.5\n- Top markers: CD3D, MS4A1\n",
                },
                oracle={"expected.json": oracle_expected},
            ),
            Control(
                name="e1-fail-broken-replay-script",
                expect_pass=False,
                deliverables={
                    "output/reproduce.py": "raise RuntimeError('kernel state lost')\n",
                    "output/tables/markers.csv": valid_table,
                    "output/results.json": valid_results,
                    "output/REPORT.md": valid_report,
                },
                oracle={"expected.json": oracle_expected},
            ),
        ]


# ============================================================================
# E2: Provenance & Staleness Detection Benchmark (I1--I10)
# ============================================================================


def verify_provenance_bundle(workspace: Path) -> dict[str, Any]:
    """Audit a workspace provenance bundle against all 10 E2 tampering modes."""
    receipt_path = workspace / "provenance" / "replay_receipt.json"
    if not receipt_path.is_file():
        return {"valid": False, "anomalies": ["missing_receipt"]}

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception:
        return {"valid": False, "anomalies": ["corrupt_receipt"]}

    anomalies: list[str] = []

    # I1: Upstream data file byte modification (supports both dict and list-of-dicts schemas)
    raw_inputs = receipt.get("inputs", {})
    declared_inputs: dict[str, str] = {}
    if isinstance(raw_inputs, Mapping):
        for k, v in raw_inputs.items():
            declared_inputs[str(k)] = str(v)
    elif isinstance(raw_inputs, Sequence) and not isinstance(raw_inputs, (str, bytes)):
        for item in raw_inputs:
            if isinstance(item, Mapping) and item.get("path"):
                declared_inputs[str(item["path"])] = str(item.get("sha256") or "")

    for stale_rel in receipt.get("stale_inputs") or []:
        anomalies.append(f"I1_tampered_input:{stale_rel}")

    for rel_path, expected_sha in declared_inputs.items():
        fpath = workspace / rel_path
        if not fpath.is_file() or (expected_sha and _sha256_file(fpath) != expected_sha):
            anomalies.append(f"I1_tampered_input:{rel_path}")

    # I2 & I7: Step script / exported script hash mismatch or stale receipt
    script_rel = str(receipt.get("script_path") or receipt.get("script") or "output/reproduce.py")
    script_path = workspace / script_rel
    expected_script_sha = str(receipt.get("script_sha256") or "")
    if not script_path.is_file():
        anomalies.append("I2_missing_script")
    else:
        actual_script_sha = _sha256_file(script_path)
        if expected_script_sha and actual_script_sha != expected_script_sha:
            if receipt.get("run_id") != receipt.get("active_run_id", receipt.get("run_id")):
                anomalies.append("I7_stale_receipt")
            else:
                anomalies.append("I2_tampered_script")

    if receipt.get("active_run_id") and receipt.get("run_id") != receipt.get("active_run_id"):
        if "I7_stale_receipt" not in anomalies:
            anomalies.append("I7_stale_receipt")

    # I3: Unregistered file read in kernel_ledger.jsonl or receipt["unregistered_reads"]
    for unreg in receipt.get("unregistered_reads") or []:
        anomalies.append(f"I3_unregistered_read:{unreg}")

    for ledger_cand in (
        workspace / "provenance" / "kernel_ledger.jsonl",
        workspace / ".omicsclaw" / "provenance" / "kernel_ledger.jsonl",
    ):
        if ledger_cand.is_file():
            for line in ledger_cand.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                for f_rec in entry.get("files_read", []):
                    rel = f_rec.get("rel_path") or f_rec.get("path", "")
                    if rel and rel not in declared_inputs and not str(rel).startswith("output/"):
                        anomalies.append(f"I3_unregistered_read:{rel}")

    # I4: Sidecar kernel.py modification after receipt
    skill_records: list[tuple[str, str]] = []
    for sc in receipt.get("skill_calls") or []:
        if isinstance(sc, Mapping) and sc.get("kernel_path") and sc.get("kernel_sha256"):
            skill_records.append((str(sc["kernel_path"]), str(sc["kernel_sha256"])))
    for sf in receipt.get("skill_files") or []:
        if isinstance(sf, Mapping) and sf.get("path") and sf.get("sha256"):
            skill_records.append((str(sf["path"]), str(sf["sha256"])))
    for step in receipt.get("steps") or []:
        if isinstance(step, Mapping):
            for sc in step.get("skill_calls") or []:
                if isinstance(sc, Mapping) and sc.get("kernel_path") and sc.get("kernel_sha256"):
                    skill_records.append((str(sc["kernel_path"]), str(sc["kernel_sha256"])))

    for kpath_rel, expected_ksha in skill_records:
        kpath = Path(kpath_rel) if Path(kpath_rel).is_absolute() else (workspace / kpath_rel)
        if not kpath.is_file() or _sha256_file(kpath) != expected_ksha:
            anomalies.append(f"I4_tampered_sidecar_kernel:{kpath_rel}")

    # I5 & I8: Declared outputs (figures / cross-language CSVs) missing or tampered
    raw_outputs = receipt.get("outputs")
    declared_outputs: dict[str, str] = {}
    if isinstance(raw_outputs, Mapping):
        for k, v in raw_outputs.items():
            declared_outputs[str(k)] = str(v)
    elif isinstance(receipt.get("verified_outputs"), Sequence):
        for item in receipt.get("verified_outputs") or []:
            if isinstance(item, Mapping) and item.get("path"):
                declared_outputs[str(item["path"])] = str(item.get("sha256") or "")

    for miss_out in receipt.get("missing_outputs") or []:
        anomalies.append(f"I5_missing_output:{miss_out}")
    for chg_out in receipt.get("changed_outputs") or []:
        if chg_out not in (receipt.get("missing_outputs") or []):
            anomalies.append(f"I8_tampered_output:{chg_out}")
    for orph_out in receipt.get("orphan_outputs") or []:
        anomalies.append(f"I9_orphan_artifact:{orph_out}")

    for rel_path, expected_sha in declared_outputs.items():
        fpath = workspace / rel_path
        if not fpath.is_file():
            anomalies.append(f"I5_missing_output:{rel_path}")
        elif expected_sha and _sha256_file(fpath) != expected_sha:
            anomalies.append(f"I8_tampered_output:{rel_path}")

    # Check report figure links (I5) and report numeric claims / hash (I6)
    report_rel = str(receipt.get("report_path") or "output/REPORT.md")
    report_path = workspace / report_rel
    if not report_path.is_file() and (workspace / "output" / "REPORT.md").is_file():
        report_path = workspace / "output" / "REPORT.md"
    results_path = workspace / "output" / "results.json"
    if report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8")
        for fig_rel in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report_text):
            fig_candidate = (workspace / "output" / fig_rel) if not (workspace / fig_rel).is_file() else (workspace / fig_rel)
            if not fig_candidate.is_file():
                anomalies.append(f"I5_missing_report_figure:{fig_rel}")
        if results_path.is_file():
            try:
                res_data = json.loads(results_path.read_text(encoding="utf-8"))
                for key, val in res_data.items():
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        for m in re.findall(
                            rf"\b{re.escape(key)}\b\s*[:=]\s*([-+]?\d+(?:\.\d+)?)",
                            report_text,
                            flags=re.IGNORECASE,
                        ):
                            if not _floats_close(float(m), float(val)):
                                anomalies.append(f"I6_report_claim_mismatch:{key}")
            except Exception:
                anomalies.append("I6_corrupt_results_json")
        expected_rep_sha = str(receipt.get("report_sha256") or "")
        if expected_rep_sha and _sha256_file(report_path) != expected_rep_sha:
            if not any(a.startswith("I6_") for a in anomalies):
                anomalies.append(f"I6_tampered_report:{report_rel}")

    # I9: Leftover orphan files in output/tables or output/figures not in declared_outputs
    for sub in ("output/tables", "output/figures"):
        subdir = workspace / sub
        if subdir.is_dir():
            for child in subdir.iterdir():
                if child.is_file():
                    rel = child.relative_to(workspace).as_posix()
                    if rel not in declared_outputs:
                        anomalies.append(f"I9_orphan_artifact:{rel}")

    return {"valid": len(anomalies) == 0, "anomalies": sorted(set(anomalies))}


def _build_golden_e2_workspace(root: Path) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "sc_markers").mkdir(parents=True, exist_ok=True)
    (root / "output" / "tables").mkdir(parents=True, exist_ok=True)
    (root / "output" / "figures").mkdir(parents=True, exist_ok=True)
    (root / "provenance").mkdir(parents=True, exist_ok=True)

    data_file = root / "data" / "counts.csv"
    data_file.write_text("cell,G1,G2\nc1,10,2\nc2,12,3\n", encoding="utf-8")

    kernel_file = root / "skills" / "sc_markers" / "kernel.py"
    kernel_file.write_text("def rank_genes(df):\n    return ['G1']\n", encoding="utf-8")

    script_file = root / "output" / "reproduce.py"
    script_file.write_text("min_cells = 3\nprint('reproduce ok')\n", encoding="utf-8")

    table_file = root / "output" / "tables" / "bridge.csv"
    table_file.write_text("gene,log2fc\nG1,2.5\n", encoding="utf-8")

    fig_file = root / "output" / "figures" / "umap.png"
    fig_file.write_bytes(b"\x89PNG\r\n\x1a\ngolden-umap")

    results_file = root / "output" / "results.json"
    results_file.write_text(json.dumps({"n_significant": 1, "top_log2fc": 2.5}), encoding="utf-8")

    report_file = root / "output" / "REPORT.md"
    report_file.write_text(
        "# Analysis Report\n\n- n_significant: 1\n- top_log2fc: 2.5\n\n![UMAP](figures/umap.png)\n",
        encoding="utf-8",
    )

    receipt = {
        "run_id": "run-001",
        "active_run_id": "run-001",
        "script_path": "output/reproduce.py",
        "script_sha256": _sha256_file(script_file),
        "inputs": {"data/counts.csv": _sha256_file(data_file)},
        "outputs": {
            "output/tables/bridge.csv": _sha256_file(table_file),
            "output/figures/umap.png": _sha256_file(fig_file),
            "output/results.json": _sha256_file(results_file),
        },
        "skill_calls": [
            {
                "skill": "sc_markers",
                "function": "rank_genes",
                "kernel_path": "skills/sc_markers/kernel.py",
                "kernel_sha256": _sha256_file(kernel_file),
            }
        ],
    }
    (root / "provenance" / "replay_receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    ledger_entry = {
        "cell_number": 1,
        "files_read": [{"rel_path": "data/counts.csv", "sha256": _sha256_file(data_file)}],
    }
    (root / "provenance" / "kernel_ledger.jsonl").write_text(
        json.dumps(ledger_entry) + "\n", encoding="utf-8"
    )


def run_e2_staleness_suite(scratch_root: Path | None = None) -> dict[str, Any]:
    """Materialize and evaluate all 10 E2 tampering scenarios (I1--I10)."""
    own_temp = scratch_root is None
    temp_ctx = tempfile.TemporaryDirectory(prefix="e2-suite-") if own_temp else None
    base = Path(temp_ctx.name) if temp_ctx is not None else Path(scratch_root)  # type: ignore[arg-type]
    try:
        scenarios = ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I10"]
        results: dict[str, dict[str, Any]] = {}
        for sc in scenarios:
            ws = base / sc
            _build_golden_e2_workspace(ws)
            if sc == "I1":
                (ws / "data" / "counts.csv").write_text("cell,G1,G2\nc1,99,2\nc2,12,3\n", encoding="utf-8")
            elif sc == "I2":
                (ws / "output" / "reproduce.py").write_text("min_cells = 50\nprint('modified')\n", encoding="utf-8")
            elif sc == "I3":
                (ws / "provenance" / "kernel_ledger.jsonl").write_text(
                    json.dumps(
                        {
                            "cell_number": 2,
                            "files_read": [{"rel_path": "data/secret_unregistered.csv", "sha256": "abc"}],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif sc == "I4":
                (ws / "skills" / "sc_markers" / "kernel.py").write_text(
                    "def rank_genes(df):\n    return ['TAMPERED']\n", encoding="utf-8"
                )
            elif sc == "I5":
                (ws / "output" / "figures" / "umap.png").unlink()
            elif sc == "I6":
                (ws / "output" / "REPORT.md").write_text(
                    "# Analysis Report\n\n- n_significant: 999\n- top_log2fc: 2.5\n\n![UMAP](figures/umap.png)\n",
                    encoding="utf-8",
                )
            elif sc == "I7":
                rec = json.loads((ws / "provenance" / "replay_receipt.json").read_text(encoding="utf-8"))
                rec["run_id"] = "run-000-stale"
                rec["active_run_id"] = "run-001"
                (ws / "provenance" / "replay_receipt.json").write_text(json.dumps(rec), encoding="utf-8")
            elif sc == "I8":
                (ws / "output" / "tables" / "bridge.csv").write_text("gene,log2fc\nG1,99.9\n", encoding="utf-8")
            elif sc == "I9":
                (ws / "output" / "tables" / "orphan_old.csv").write_text("gene\nORPHAN\n", encoding="utf-8")
            results[sc] = verify_provenance_bundle(ws)

        tampered_ids = [f"I{k}" for k in range(1, 10)]
        detected = sum(1 for k in tampered_ids if not results[k]["valid"])
        recall = detected / len(tampered_ids)
        fpr = 0.0 if results["I10"]["valid"] else 1.0
        return {
            "recall": recall,
            "false_positive_rate": fpr,
            "scenarios": results,
        }
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


class E2StalenessLedgerGrader:
    """Grades staleness & tampering detection across I1--I10."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        verdicts_path = submission.workspace / "output" / "tamper_verdicts.json"
        expected_path = oracle / "expected_verdicts.json"
        if not verdicts_path.is_file() or not expected_path.is_file():
            return Grade(
                passed=False,
                score=0.0,
                metrics={"recall": 0.0, "false_positive_rate": 1.0},
                detail="missing tamper_verdicts.json or expected_verdicts.json",
            )
        try:
            verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return Grade(
                passed=False,
                score=0.0,
                metrics={"recall": 0.0, "false_positive_rate": 1.0},
                detail=f"invalid JSON: {exc}",
            )

        tampered_cases = [k for k, v in expected.items() if v.get("expect_tampered", False)]
        clean_cases = [k for k, v in expected.items() if not v.get("expect_tampered", False)]

        tp = 0
        missed: list[str] = []
        for k in tampered_cases:
            sub_v = verdicts.get(k, {})
            exp_code = expected[k].get("anomaly_prefix", "")
            anomalies = sub_v.get("anomalies", [])
            is_flagged = (sub_v.get("valid") is False) and (
                not exp_code or any(str(a).startswith(exp_code) for a in anomalies)
            )
            if is_flagged:
                tp += 1
            else:
                missed.append(k)

        fp = 0
        for k in clean_cases:
            sub_v = verdicts.get(k, {})
            if sub_v.get("valid") is not True or sub_v.get("anomalies"):
                fp += 1

        recall = tp / max(len(tampered_cases), 1)
        fpr = fp / max(len(clean_cases), 1)
        passed = (recall == 1.0) and (fpr == 0.0)
        score = max(0.0, recall - fpr)
        detail = (
            f"recall={recall:.2f}, fpr={fpr:.2f}"
            if passed
            else f"recall={recall:.2f} (missed={missed}), fpr={fpr:.2f}"
        )
        return Grade(
            passed=passed,
            score=round(score, 4),
            metrics={"recall": recall, "false_positive_rate": fpr, "missed": missed},
            detail=detail,
        )

    def controls(self) -> Sequence[Control]:
        expected_10 = {
            f"I{k}": {"expect_tampered": True, "anomaly_prefix": f"I{k}_"}
            for k in range(1, 10)
        }
        expected_10["I10"] = {"expect_tampered": False, "anomaly_prefix": ""}

        perfect_verdicts = {
            f"I{k}": {"valid": False, "anomalies": [f"I{k}_detected"]}
            for k in range(1, 10)
        }
        perfect_verdicts["I10"] = {"valid": True, "anomalies": []}

        subset_expected = {
            "I1": {"expect_tampered": True, "anomaly_prefix": "I1_"},
            "I3": {"expect_tampered": True, "anomaly_prefix": "I3_"},
            "I10": {"expect_tampered": False, "anomaly_prefix": ""},
        }
        subset_verdicts = {
            "I1": {"valid": False, "anomalies": ["I1_tampered_input:data/counts.csv"]},
            "I3": {"valid": False, "anomalies": ["I3_unregistered_read:data/secret.csv"]},
            "I10": {"valid": True, "anomalies": []},
        }

        missed_i1 = dict(perfect_verdicts)
        missed_i1["I1"] = {"valid": True, "anomalies": []}

        missed_i3 = dict(perfect_verdicts)
        missed_i3["I3"] = {"valid": True, "anomalies": []}

        false_alarm_i10 = dict(perfect_verdicts)
        false_alarm_i10["I10"] = {"valid": False, "anomalies": ["I9_orphan_artifact:false"]}

        return [
            Control(
                name="e2-pass-full-10-scenario-matrix",
                expect_pass=True,
                deliverables={"output/tamper_verdicts.json": json.dumps(perfect_verdicts)},
                oracle={"expected_verdicts.json": json.dumps(expected_10)},
            ),
            Control(
                name="e2-pass-golden-plus-tampered-subset",
                expect_pass=True,
                deliverables={"output/tamper_verdicts.json": json.dumps(subset_verdicts)},
                oracle={"expected_verdicts.json": json.dumps(subset_expected)},
            ),
            Control(
                name="e2-fail-missed-i1-data-tamper",
                expect_pass=False,
                deliverables={"output/tamper_verdicts.json": json.dumps(missed_i1)},
                oracle={"expected_verdicts.json": json.dumps(expected_10)},
            ),
            Control(
                name="e2-fail-missed-i3-bypass-read",
                expect_pass=False,
                deliverables={"output/tamper_verdicts.json": json.dumps(missed_i3)},
                oracle={"expected_verdicts.json": json.dumps(expected_10)},
            ),
            Control(
                name="e2-fail-false-alarm-on-i10",
                expect_pass=False,
                deliverables={"output/tamper_verdicts.json": json.dumps(false_alarm_i10)},
                oracle={"expected_verdicts.json": json.dumps(expected_10)},
            ),
        ]


# ============================================================================
# E3: Evidence-Gated Reviewer Ablation Benchmark (V1--V5)
# ============================================================================


def _strip_conclusion_sections(markdown_text: str) -> str:
    """Remove Conclusion / Summary prose sections for the V2 anti-halo ablation."""
    lines = markdown_text.splitlines()
    kept: list[str] = []
    skip = False
    for line in lines:
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if any(w in heading for w in ("conclusion", "summary", "discussion", "interpretation")):
                skip = True
                continue
            skip = False
        if not skip:
            kept.append(line)
    return "\n".join(kept)


def build_e3_reviewer_prompt(level: str, bundle: Mapping[str, str]) -> str:
    """Build the reviewer prompt for information gradient level V1--V5."""
    lvl = level.upper()
    if lvl not in {"V1", "V2", "V3", "V4", "V5"}:
        raise ValueError(f"Unsupported E3 reviewer gradient level: {level!r}")

    report_md = bundle.get("REPORT.md", "")
    code_py = bundle.get("reproduce.py", "")
    notebook_txt = bundle.get("notebook_cells", code_py)
    review_brief = bundle.get("review_brief.md", "")

    parts = [f"# E3 Scientific & Provenance Review ({lvl})"]
    if lvl == "V1":
        parts.append("## Narrative Report (`REPORT.md`)\n" + report_md)
    elif lvl == "V2":
        parts.append(
            "## Narrative Report (Conclusion Stripped)\n"
            + _strip_conclusion_sections(report_md)
            + "\n\n## Executed Code (`reproduce.py`)\n```python\n"
            + code_py
            + "\n```"
        )
    elif lvl == "V3":
        parts.append(
            "## Narrative Report (`REPORT.md`)\n"
            + report_md
            + "\n\n## Notebook Code & Outputs\n"
            + notebook_txt
        )
    elif lvl in {"V4", "V5"}:
        parts.append(
            "## Narrative Report (`REPORT.md`)\n"
            + report_md
            + "\n\n## Provenance & Artifact Brief (`review_brief.md`)\n"
            + review_brief
        )

    if lvl == "V5":
        parts.append(
            "## Domain Statistical & Provenance Audit Checklist (6-Point Mandatory Gate)\n"
            "1. **A1/A3 Claim-to-Table Concordance**: Verify every number and gene symbol in `REPORT.md` against the actual CSV tables.\n"
            "2. **A2/A4 Clean-Room Replay & Figure Integrity**: Reject if `replay_verified` is false, hashes mismatch, or referenced figures are missing.\n"
            "3. **B5 Sample-vs-Cell Unit of Replication**: Reject cell-level or spot-level differential testing across multi-donor (`donor_id`, `orig.ident`) or multi-slide (`slide_id`) studies without pseudobulk/mixed-model aggregation.\n"
            "4. **B6 Post-Clustering Selective Inference (Double-Dipping)**: Reject marker p-values computed by clustering and testing on the same counts without `check_homogeneous_null` or count-splitting.\n"
            "5. **B7 Multiple-Testing Correction**: Reject significance claims based on unadjusted `pval` instead of BH-adjusted `padj` / FDR.\n"
            "6. **B8/B9 Batch Confounding & Normalization**: Verify normalization and batch/spatial alignment before downstream claims."
        )
    return "\n\n".join(parts)


def deterministic_statistical_audit(bundle: Mapping[str, str], level: str = "V5") -> dict[str, Any]:
    """Evaluate a review bundle under information gradient `level` (V1--V5)."""
    lvl = level.upper()
    code_py = bundle.get("reproduce.py", "")
    review_brief = bundle.get("review_brief.md", "")
    findings: list[str] = []

    if lvl in {"V2", "V3", "V4", "V5"} and code_py:
        if lvl == "V5":
            has_explicit_cell_de = (
                "wilcoxon_cells" in code_py
                or "groupby='condition'" in code_py
                or 'groupby="condition"' in code_py
            )
            if (
                "donor_id" in code_py
                or "slide_id" in code_py
                or "orig.ident" in code_py
                or "multi_donor" in code_py
            ) and (has_explicit_cell_de or not _inspect_statistical_mechanism(code_py)):
                findings.append("B5")
            if ("kmeans" in code_py or "leiden" in code_py) and (
                "check_homogeneous_null" not in code_py and "count_split" not in code_py
            ):
                findings.append("B6")
            if "pval <" in code_py and "padj" not in code_py:
                findings.append("B7")
            if "log1p" in code_py and "deseq2" in code_py and "raw_counts" not in code_py and "layers['counts']" not in code_py and 'layers["counts"]' not in code_py:
                findings.append("B8")
            if ("hypergeom" in code_py or "enrich" in code_py) and "20000" in code_py and ("panel" in code_py or "500" in code_py):
                findings.append("B9")

    if lvl in {"V4", "V5"} and review_brief:
        has_nonempty_brief_anomaly = any(
            re.search(rf"-\s*\*\*{label}\*\*:\s*\[[^\]]+\]", review_brief)
            for label in ("Changed Outputs", "Orphan Outputs", "Stale Inputs", "Unregistered Bypass Reads")
        )
        if (
            "replay_verified: False" in review_brief
            or "stale_artifact" in review_brief
            or "- **Status**: `failed`" in review_brief
            or has_nonempty_brief_anomaly
        ):
            findings.append("A2")
        if "missing_figure" in review_brief or re.search(
            r"-\s*\*\*Missing Outputs\*\*:\s*\[[^\]]*\.(?:png|pdf|svg)", review_brief
        ):
            findings.append("A4")
        for m in re.findall(r"table_claim_mismatch:(\w+)", review_brief):
            findings.append(m)

    verdict = "REVISE" if findings else "APPROVE"
    return {"verdict": verdict, "categories": sorted(set(findings)), "level": lvl}


class E3ReviewerGradientGrader:
    """Grades a reviewer's verdict and defect category classification against oracle/defect.json."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        verdict_path = submission.workspace / "output" / "review_verdict.json"
        defect_path = oracle / "defect.json"
        if not verdict_path.is_file() or not defect_path.is_file():
            return Grade(
                passed=False,
                score=0.0,
                metrics={"verdict_correct": False, "category_matched": False},
                detail="missing review_verdict.json or oracle/defect.json",
            )
        try:
            review = json.loads(verdict_path.read_text(encoding="utf-8"))
            defect = json.loads(defect_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return Grade(
                passed=False,
                score=0.0,
                metrics={"verdict_correct": False, "category_matched": False},
                detail=f"malformed JSON: {exc}",
            )

        raw_verdict = str(review.get("verdict", "")).upper().strip()
        flagged_defect = raw_verdict in {"REVISE", "REJECT", "FAIL", "WARN"}
        has_defect = bool(defect.get("has_defect", False))
        expected_cat = str(defect.get("category", "")).upper().strip()
        reported_cats = {str(c).upper().strip() for c in review.get("categories", [])}
        if review.get("category"):
            reported_cats.add(str(review["category"]).upper().strip())

        verdict_correct = flagged_defect == has_defect
        if has_defect:
            category_matched = (not expected_cat) or (expected_cat in reported_cats)
        else:
            category_matched = len(reported_cats) == 0

        passed = bool(verdict_correct and category_matched)
        score = 1.0 if passed else (0.5 if verdict_correct else 0.0)
        detail = (
            f"verdict={raw_verdict}, categories={sorted(reported_cats)}"
            if passed
            else f"expected has_defect={has_defect} ({expected_cat}), got verdict={raw_verdict} ({sorted(reported_cats)})"
        )
        return Grade(
            passed=passed,
            score=score,
            metrics={
                "verdict_correct": verdict_correct,
                "category_matched": category_matched,
                "false_approval": bool(has_defect and not flagged_defect),
                "false_rejection": bool((not has_defect) and flagged_defect),
            },
            detail=detail,
        )

    def controls(self) -> Sequence[Control]:
        return [
            Control(
                name="e3-pass-catch-b5-pseudoreplication",
                expect_pass=True,
                deliverables={
                    "output/review_verdict.json": json.dumps(
                        {"verdict": "REVISE", "categories": ["B5"], "rationale": "Cell-level Wilcoxon across 8 donors"}
                    )
                },
                oracle={
                    "defect.json": json.dumps(
                        {"has_defect": True, "category": "B5", "description": "Multi-donor pseudoreplication"}
                    )
                },
            ),
            Control(
                name="e3-pass-approve-clean-twin",
                expect_pass=True,
                deliverables={
                    "output/review_verdict.json": json.dumps(
                        {"verdict": "APPROVE", "categories": [], "rationale": "Pseudobulk DE and receipt verified"}
                    )
                },
                oracle={
                    "defect.json": json.dumps(
                        {"has_defect": False, "category": "", "description": "Clean twin run"}
                    )
                },
            ),
            Control(
                name="e3-fail-approve-a1-fabricated-claim",
                expect_pass=False,
                deliverables={
                    "output/review_verdict.json": json.dumps(
                        {"verdict": "APPROVE", "categories": []}
                    )
                },
                oracle={
                    "defect.json": json.dumps(
                        {"has_defect": True, "category": "A1", "description": "Fabricated gene count in REPORT.md"}
                    )
                },
            ),
            Control(
                name="e3-fail-approve-b5-pseudoreplication",
                expect_pass=False,
                deliverables={
                    "output/review_verdict.json": json.dumps(
                        {"verdict": "APPROVE", "categories": []}
                    )
                },
                oracle={
                    "defect.json": json.dumps(
                        {"has_defect": True, "category": "B5", "description": "Pseudoreplication"}
                    )
                },
            ),
            Control(
                name="e3-fail-false-reject-clean-run",
                expect_pass=False,
                deliverables={
                    "output/review_verdict.json": json.dumps(
                        {"verdict": "REVISE", "categories": ["B6"]}
                    )
                },
                oracle={
                    "defect.json": json.dumps(
                        {"has_defect": False, "category": "", "description": "Clean run"}
                    )
                },
            ),
        ]


# ============================================================================
# E4: Matched Null/Twin DGP Generators, Calibrators (V1--V4), and Grader
# ============================================================================


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _exact_student_t_pvalue(t_abs: float, df: int) -> float:
    """Exact two-sided Student-t p-value for integer degrees of freedom."""
    if t_abs <= 0.0:
        return 1.0
    if df > 40:
        return max(0.0, min(1.0, 2.0 * (1.0 - _normal_cdf(t_abs))))
    theta = math.atan(t_abs / math.sqrt(df))
    sin_t = math.sin(theta)
    cos2 = math.cos(theta) ** 2
    if df % 2 == 0:
        m = df // 2
        total = 0.0
        term = 1.0
        for k in range(m):
            if k > 0:
                term *= ((2 * k - 1) / (2 * k)) * cos2
            total += term
        p = 1.0 - sin_t * total
    else:
        m = (df - 1) // 2
        total = 0.0
        term = 1.0
        for k in range(m):
            if k > 0:
                term *= ((2 * k) / (2 * k + 1)) * cos2
            total += term
        if m == 0:
            p = 1.0 - (2.0 / math.pi) * theta
        else:
            p = 1.0 - (2.0 / math.pi) * (theta + sin_t * math.cos(theta) * total)
    return max(0.0, min(1.0, p))


def _two_sided_z_pvalue(vals_a: Sequence[float], vals_b: Sequence[float]) -> float:
    """Exact two-sample pooled Student-t p-value using pure stdlib math."""
    na, nb = len(vals_a), len(vals_b)
    if na < 2 or nb < 2:
        return 1.0
    ma = sum(vals_a) / na
    mb = sum(vals_b) / nb
    ssa = sum((x - ma) ** 2 for x in vals_a)
    ssb = sum((x - mb) ** 2 for x in vals_b)
    df = max(na + nb - 2, 1)
    s2 = (ssa + ssb) / df
    denom = math.sqrt(s2 * (1.0 / na + 1.0 / nb) + 1e-12)
    t_abs = abs(ma - mb) / denom
    return _exact_student_t_pvalue(t_abs, df)


def _bh_adjust(pvals: Sequence[float]) -> list[float]:
    n = len(pvals)
    if n == 0:
        return []
    indexed = sorted(enumerate(pvals), key=lambda kv: kv[1])
    adj = [1.0] * n
    running_min = 1.0
    for rank in range(n, 0, -1):
        orig_idx, p = indexed[rank - 1]
        val = min(1.0, (p * n) / rank)
        running_min = min(running_min, val)
        adj[orig_idx] = running_min
    return adj


def generate_pseudorep_dgp(
    *,
    seed: int = 42,
    mode: str = "null",
    tier: str = "T1",
    n_donors: int = 8,
    cells_per_donor: int = 50,
    n_genes: int = 120,
    n_true_de: int = 16,
) -> dict[str, Any]:
    """Generate multi-donor single-cell counts with donor random effects (Null vs Twin)."""
    rng = random.Random(seed)
    genes = [f"GENE_{g:03d}" for g in range(n_genes)]
    true_de_genes = genes[:n_true_de] if mode == "twin" else []
    true_de_set = set(true_de_genes)

    obs: list[dict[str, Any]] = []
    counts: list[list[float]] = []

    for d in range(n_donors):
        cond = "case" if d >= (n_donors // 2) else "ctrl"
        donor_label = f"donor_{d + 1:02d}"
        seurat_ident = f"SC_L{d + 1:02d}"
        donor_shifts = [rng.gauss(0.0, 0.85) for _ in range(n_genes)]
        for c in range(cells_per_donor):
            cell_id = f"{donor_label}_{cond}_cell{c + 1:03d}"
            if tier.upper() == "T2":
                obs.append({"cell_id": cell_id, "orig.ident": seurat_ident, "condition": cond})
            else:
                obs.append({"cell_id": cell_id, "donor_id": donor_label, "condition": cond})
            row: list[float] = []
            for g_idx, g_name in enumerate(genes):
                base = 4.0 + donor_shifts[g_idx]
                if g_name in true_de_set and cond == "case":
                    base += 3.1
                val = max(0.0, base + rng.gauss(0.0, 0.45))
                row.append(round(val, 4))
            counts.append(row)

    return {
        "paradigm": "pseudorep",
        "mode": mode,
        "tier": tier,
        "genes": genes,
        "true_genes": true_de_genes,
        "obs": obs,
        "counts": counts,
    }


def _sample_poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0.0:
        return 0
    if lam < 30.0:
        limit = math.exp(-lam)
        k = 0
        p = 1.0
        while p > limit:
            k += 1
            p *= rng.random()
        return k - 1
    val = int(math.floor(lam + math.sqrt(lam) * rng.gauss(0.0, 1.0) + 0.5))
    return max(0, val)


def _sample_neg_binom(rng: random.Random, mean: float, dispersion: float) -> int:
    if mean <= 0.0:
        return 0
    theta = max(float(dispersion), 1e-3)
    gamma_rate = rng.gammavariate(theta, mean / theta)
    return _sample_poisson(rng, gamma_rate)


def _sample_binomial(rng: random.Random, n: int, p: float) -> int:
    if n <= 0 or p <= 0.0:
        return 0
    if p >= 1.0:
        return n
    if n < 25:
        return sum(1 for _ in range(n) if rng.random() < p)
    mu = n * p
    sd = math.sqrt(n * p * (1.0 - p))
    val = int(math.floor(mu + sd * rng.gauss(0.0, 1.0) + 0.5))
    return max(0, min(n, val))


def generate_double_dipping_dgp(
    *,
    seed: int = 42,
    mode: str = "null",
    n_cells: int = 400,
    n_genes: int = 200,
    n_true_markers: int = 15,
    fold_change: float = 3.5,
    dispersion: float = 2.5,
) -> dict[str, Any]:
    """Generate homogeneous K=1 Null vs K=2 Twin Negative Binomial count data ($C=400, G=200$)."""
    rng = random.Random(seed)
    genes = [f"GENE_{g:03d}" for g in range(n_genes)]
    true_markers = genes[:n_true_markers] if mode == "twin" else []
    true_set = set(true_markers)

    base_means = [rng.uniform(3.5, 8.0) for _ in range(n_genes)]
    dispersions = [round(dispersion * rng.uniform(0.85, 1.15), 4) for _ in range(n_genes)]
    size_factors: list[float] = []
    counts: list[list[float]] = []
    true_subpop: list[int] = []

    for c in range(n_cells):
        sub = 1 if (mode == "twin" and c >= (n_cells // 2)) else 0
        true_subpop.append(sub)
        s_c = math.exp(rng.gauss(0.0, 0.22))
        size_factors.append(round(s_c, 4))
        row: list[float] = []
        for g_idx, g_name in enumerate(genes):
            mu = s_c * base_means[g_idx]
            if g_name in true_set and sub == 1:
                mu *= fold_change
            cnt = _sample_neg_binom(rng, mu, dispersions[g_idx])
            row.append(float(cnt))
        counts.append(row)

    return {
        "paradigm": "double_dipping",
        "mode": mode,
        "genes": genes,
        "true_genes": true_markers,
        "true_subpop": true_subpop,
        "size_factors": size_factors,
        "dispersions": dispersions,
        "counts": counts,
    }


def generate_spatial_dgp(
    *,
    seed: int = 42,
    mode: str = "null",
    n_slides: int = 8,
    spots_per_slide: int = 40,
    n_genes: int = 100,
    n_true_de: int = 14,
) -> dict[str, Any]:
    """Generate multi-slide spatial transcriptomics data with slide random effects and spatial domains."""
    rng = random.Random(seed)
    genes = [f"GENE_{g:03d}" for g in range(n_genes)]
    true_de_genes = genes[:n_true_de] if mode == "twin" else []
    true_set = set(true_de_genes)

    obs: list[dict[str, Any]] = []
    counts: list[list[float]] = []
    for s in range(n_slides):
        cond = "disease" if s >= (n_slides // 2) else "healthy"
        slide_id = f"slide_{s + 1:02d}"
        slide_shifts = [rng.gauss(0.0, 0.82) for _ in range(n_genes)]
        for sp in range(spots_per_slide):
            x = float(sp % 8)
            y = float(sp // 8)
            domain = "cortex_L1" if x < 4.0 else "white_matter"
            obs.append(
                {
                    "spot_id": f"{slide_id}_spot{sp + 1:03d}",
                    "slide_id": slide_id,
                    "condition": cond,
                    "spatial_domain": domain,
                    "region": domain,
                    "x": x,
                    "y": y,
                }
            )
            row: list[float] = []
            for g_idx, g_name in enumerate(genes):
                base = 5.0 + slide_shifts[g_idx] + 0.05 * math.sin(x + y)
                if g_name in true_set and cond == "disease":
                    base += 3.25
                row.append(round(max(0.0, base + rng.gauss(0.0, 0.45)), 4))
            counts.append(row)

    return {
        "paradigm": "spatial",
        "mode": mode,
        "genes": genes,
        "true_genes": true_de_genes,
        "obs": obs,
        "counts": counts,
    }


def _eval_pseudorep_methods(dgp: Mapping[str, Any]) -> dict[str, list[str]]:
    genes: list[str] = list(dgp["genes"])
    obs: list[dict[str, Any]] = list(dgp["obs"])
    counts: list[list[float]] = list(dgp["counts"])

    cell_pvals: list[float] = []
    for g_idx in range(len(genes)):
        a = [counts[i][g_idx] for i, o in enumerate(obs) if o["condition"] == "case"]
        b = [counts[i][g_idx] for i, o in enumerate(obs) if o["condition"] == "ctrl"]
        cell_pvals.append(_two_sided_z_pvalue(a, b))
    cell_padj = _bh_adjust(cell_pvals)
    naive_sig = [genes[i] for i, q in enumerate(cell_padj) if q < 0.05]

    donor_groups: dict[tuple[str, str], list[int]] = {}
    for i, o in enumerate(obs):
        orig = str(o.get("orig.ident", ""))
        donor = o.get("donor_id") or (
            orig if orig.startswith("SC_L") else "_".join(orig.split("_")[:2])
        )
        donor_groups.setdefault((donor, o["condition"]), []).append(i)

    pb_pvals: list[float] = []
    for g_idx in range(len(genes)):
        a = [
            sum(counts[i][g_idx] for i in idxs) / len(idxs)
            for (_, cond), idxs in donor_groups.items()
            if cond == "case"
        ]
        b = [
            sum(counts[i][g_idx] for i in idxs) / len(idxs)
            for (_, cond), idxs in donor_groups.items()
            if cond == "ctrl"
        ]
        pb_pvals.append(_two_sided_z_pvalue(a, b))
    pb_padj = _bh_adjust(pb_pvals)
    pb_sig = [genes[i] for i, q in enumerate(pb_padj) if q < 0.05]
    return {"naive_sig": naive_sig, "pseudobulk_sig": pb_sig}


def _partition_2means_1d(values: Sequence[float]) -> tuple[list[int], list[int], float]:
    """Partition 1D projection into 2 clusters via 2-means and return (cl_a, cl_b, bss_tss_ratio)."""
    n = len(values)
    if n < 4:
        return list(range(n // 2)), list(range(n // 2, n)), 0.0
    s_vals = sorted(values)
    c0 = sum(s_vals[: n // 2]) / max(n // 2, 1)
    c1 = sum(s_vals[n // 2 :]) / max(n - n // 2, 1)
    for _ in range(8):
        mid = 0.5 * (c0 + c1)
        g0 = [x for x in values if x < mid]
        g1 = [x for x in values if x >= mid]
        if not g0 or not g1:
            break
        c0 = sum(g0) / len(g0)
        c1 = sum(g1) / len(g1)
    mid = 0.5 * (c0 + c1)
    cl_a = [i for i, x in enumerate(values) if x >= mid]
    cl_b = [i for i, x in enumerate(values) if x < mid]
    if not cl_a or not cl_b:
        med = s_vals[n // 2]
        cl_a = [i for i, x in enumerate(values) if x >= med]
        cl_b = [i for i, x in enumerate(values) if x < med]
    grand_mean = sum(values) / n
    tss = sum((x - grand_mean) ** 2 for x in values) + 1e-12
    mean_a = sum(values[i] for i in cl_a) / max(len(cl_a), 1)
    mean_b = sum(values[i] for i in cl_b) / max(len(cl_b), 1)
    bss = len(cl_a) * ((mean_a - grand_mean) ** 2) + len(cl_b) * ((mean_b - grand_mean) ** 2)
    return cl_a, cl_b, (bss / tss)


def _eval_double_dipping_methods(dgp: Mapping[str, Any]) -> dict[str, list[str]]:
    """Compare naive post-clustering DE against check_homogeneous_null + NB count-splitting."""
    genes: list[str] = list(dgp["genes"])
    counts: list[list[float]] = list(dgp["counts"])
    dispersions: list[float] = list(dgp.get("dispersions") or [2.5] * len(genes))
    size_factors: list[float] = list(dgp.get("size_factors") or [1.0] * len(counts))
    n_cells = len(counts)
    n_genes = len(genes)
    n_panel = min(20, n_genes)

    # 1. Naive double-dipping: cluster on log1p(X) and test on the SAME X
    log_x = [[math.log1p(max(0.0, val)) for val in row] for row in counts]
    naive_proj = [sum(row[:n_panel]) / max(n_panel, 1) for row in log_x]
    cl_a, cl_b, _ = _partition_2means_1d(naive_proj)

    naive_pvals = [
        _two_sided_z_pvalue([log_x[i][g] for i in cl_a], [log_x[i][g] for i in cl_b])
        for g in range(n_genes)
    ]
    naive_padj = _bh_adjust(naive_pvals)
    naive_sig = [genes[i] for i, q in enumerate(naive_padj) if q < 0.05]

    # 2. Calibrated pipeline: NB Binomial count-splitting (eps=0.5) + check_homogeneous_null gate
    rng = random.Random(9001 + n_cells + int(counts[0][0] if counts and counts[0] else 0))
    eps = 0.5
    x1_log: list[list[float]] = []
    x2_vst: list[list[float]] = []
    for c in range(n_cells):
        s_c = max(float(size_factors[c]) if c < len(size_factors) else 1.0, 1e-3)
        r1: list[float] = []
        r2: list[float] = []
        for g in range(n_genes):
            x_cg = int(round(max(0.0, counts[c][g])))
            x1_cg = _sample_binomial(rng, x_cg, eps)
            x2_cg = x_cg - x1_cg
            theta_g = max(dispersions[g] if g < len(dispersions) else 2.5, 0.5)
            r1.append(math.log1p(x1_cg / max(eps * s_c, 1e-6)))
            # NB dispersion-aware log2-VST normalized by cell size factor on held-out split X^(2)
            r2.append(
                math.log2(
                    1.0 + (x2_cg + 0.375) / max((1.0 - eps) * s_c * (1.0 + 1.0 / theta_g), 1e-6)
                )
            )
        x1_log.append(r1)
        x2_vst.append(r2)

    # Cluster exclusively on split X^(1)
    split_proj = [sum(row[:n_panel]) / max(n_panel, 1) for row in x1_log]
    sp_a, sp_b, _ = _partition_2means_1d(split_proj)

    # Test each gene on independent held-out split X^(2) using X^(1) cluster assignments
    split_pvals: list[float] = []
    split_lfcs: list[float] = []
    for g in range(n_genes):
        vals_a = [x2_vst[i][g] for i in sp_a]
        vals_b = [x2_vst[i][g] for i in sp_b]
        split_pvals.append(_two_sided_z_pvalue(vals_a, vals_b))
        ma = sum(vals_a) / max(len(vals_a), 1)
        mb = sum(vals_b) / max(len(vals_b), 1)
        split_lfcs.append(abs(ma - mb))

    split_padj = _bh_adjust(split_pvals)

    # check_homogeneous_null gate (matching sc-markers/kernel.py lfc_thresh=0.8):
    # if no candidate gene on held-out split X^(2) achieves |log2FC| >= 0.8, abstain (K=1 homogeneous null)
    sig_lfcs = [split_lfcs[i] for i, q in enumerate(split_padj) if q < 0.05]
    if not sig_lfcs or max(sig_lfcs) < 0.8:
        return {"naive_sig": naive_sig, "calibrated_sig": []}

    calibrated_sig = [
        genes[i] for i, q in enumerate(split_padj) if q < 0.05 and split_lfcs[i] >= 0.8
    ]
    return {"naive_sig": naive_sig, "calibrated_sig": calibrated_sig}


def _eval_spatial_methods(dgp: Mapping[str, Any]) -> dict[str, list[str]]:
    genes: list[str] = list(dgp["genes"])
    obs: list[dict[str, Any]] = list(dgp["obs"])
    counts: list[list[float]] = list(dgp["counts"])

    spot_pvals = [
        _two_sided_z_pvalue(
            [counts[i][g] for i, o in enumerate(obs) if o["condition"] == "disease"],
            [counts[i][g] for i, o in enumerate(obs) if o["condition"] == "healthy"],
        )
        for g in range(len(genes))
    ]
    spot_sig = [genes[i] for i, q in enumerate(_bh_adjust(spot_pvals)) if q < 0.05]

    slide_groups: dict[tuple[str, str], list[int]] = {}
    for i, o in enumerate(obs):
        slide_groups.setdefault((o["slide_id"], o["condition"]), []).append(i)

    slide_pvals = [
        _two_sided_z_pvalue(
            [
                sum(counts[i][g] for i in idxs) / len(idxs)
                for (_, cond), idxs in slide_groups.items()
                if cond == "disease"
            ],
            [
                sum(counts[i][g] for i in idxs) / len(idxs)
                for (_, cond), idxs in slide_groups.items()
                if cond == "healthy"
            ],
        )
        for g in range(len(genes))
    ]
    slide_sig = [genes[i] for i, q in enumerate(_bh_adjust(slide_pvals)) if q < 0.05]
    return {"naive_sig": spot_sig, "pseudobulk_sig": slide_sig}


def calibrate_e4_suite(n_seeds: int = 20, *, n_splits: int = 35) -> dict[str, Any]:
    """Run pre-agent calibration gates V1--V4 across `n_splits` (V1) and `n_seeds` (V2--V4) realizations."""
    v1_runs = max(int(n_splits), int(n_seeds))
    other_runs = max(int(n_seeds), 1)

    v1_pb_fp = 0
    v1_naive_fp = 0
    for idx in range(v1_runs):
        seed = 100 + idx
        pr_null = generate_pseudorep_dgp(seed=seed, mode="null")
        res_v1 = _eval_pseudorep_methods(pr_null)
        if len(res_v1["pseudobulk_sig"]) > 0:
            v1_pb_fp += 1
        if len(res_v1["naive_sig"]) > 0:
            v1_naive_fp += 1

    v2_recalls: list[float] = []
    v2_fdps: list[float] = []
    v3_cal_fp = 0
    v3_naive_fp = 0
    v4_pb_fp = 0
    v4_naive_fp = 0
    v4_recalls: list[float] = []
    v4_fdps: list[float] = []

    for idx in range(other_runs):
        seed = 100 + idx
        pr_twin = generate_pseudorep_dgp(seed=seed, mode="twin")
        res_v2 = _eval_pseudorep_methods(pr_twin)
        s_set = set(res_v2["pseudobulk_sig"])
        t_set = set(pr_twin["true_genes"])
        v2_recalls.append(len(s_set & t_set) / max(len(t_set), 1))
        v2_fdps.append(len(s_set - t_set) / max(len(s_set), 1))

        dd_null = generate_double_dipping_dgp(seed=seed, mode="null")
        res_v3 = _eval_double_dipping_methods(dd_null)
        if len(res_v3["calibrated_sig"]) > 0:
            v3_cal_fp += 1
        if len(res_v3["naive_sig"]) > 0:
            v3_naive_fp += 1

        sp_null = generate_spatial_dgp(seed=seed, mode="null")
        res_v4 = _eval_spatial_methods(sp_null)
        if len(res_v4["pseudobulk_sig"]) > 0:
            v4_pb_fp += 1
        if len(res_v4["naive_sig"]) > 0:
            v4_naive_fp += 1

        sp_twin = generate_spatial_dgp(seed=seed, mode="twin")
        res_v4_twin = _eval_spatial_methods(sp_twin)
        sp_s_set = set(res_v4_twin["pseudobulk_sig"])
        sp_t_set = set(sp_twin["true_genes"])
        v4_recalls.append(len(sp_s_set & sp_t_set) / max(len(sp_t_set), 1))
        v4_fdps.append(len(sp_s_set - sp_t_set) / max(len(sp_s_set), 1))

    v1_pb_fpr = v1_pb_fp / max(v1_runs, 1)
    v1_naive_fpr = v1_naive_fp / max(v1_runs, 1)
    v2_mean_recall = sum(v2_recalls) / other_runs
    v2_mean_fdp = sum(v2_fdps) / other_runs
    v3_cal_fpr = v3_cal_fp / other_runs
    v3_naive_fpr = v3_naive_fp / other_runs
    v4_pb_fpr = v4_pb_fp / other_runs
    v4_naive_fpr = v4_naive_fp / other_runs
    v4_mean_recall = sum(v4_recalls) / other_runs
    v4_mean_fdp = sum(v4_fdps) / other_runs

    v1_ok = (v1_pb_fpr <= 0.10) and (v1_naive_fpr >= 0.80)
    v2_ok = (0.40 <= v2_mean_recall <= 0.85) and (v2_mean_fdp <= 0.10)
    v3_ok = (v3_cal_fpr <= 0.10) and (v3_naive_fpr >= 0.80)
    v4_ok = (
        (v4_pb_fpr <= 0.10)
        and (v4_naive_fpr >= 0.80)
        and (v4_mean_recall >= 0.60)
        and (v4_mean_fdp <= 0.10)
    )

    return {
        "all_gates_passed": bool(v1_ok and v2_ok and v3_ok and v4_ok),
        "V1": {
            "passed": v1_ok,
            "n_splits": v1_runs,
            "pseudobulk_fpr": round(v1_pb_fpr, 4),
            "naive_fpr": round(v1_naive_fpr, 4),
        },
        "V2": {
            "passed": v2_ok,
            "n_seeds": other_runs,
            "mean_recall": round(v2_mean_recall, 4),
            "mean_fdp": round(v2_mean_fdp, 4),
        },
        "V3": {
            "passed": v3_ok,
            "n_seeds": other_runs,
            "calibrated_fpr": round(v3_cal_fpr, 4),
            "naive_fpr": round(v3_naive_fpr, 4),
        },
        "V4": {
            "passed": v4_ok,
            "n_seeds": other_runs,
            "pseudobulk_fpr": round(v4_pb_fpr, 4),
            "naive_fpr": round(v4_naive_fpr, 4),
            "mean_recall": round(v4_mean_recall, 4),
            "mean_fdp": round(v4_mean_fdp, 4),
            "twin_recall": round(v4_mean_recall, 4),
            "twin_fdp": round(v4_mean_fdp, 4),
        },
    }


_STRONG_MECHANISM_TOKENS = (
    "pseudobulk",
    "spatial_pseudobulk",
    "aggregate_pseudobulk",
    "check_homogeneous_null",
    "count_split",
    "mixedlm",
    "deseq2",
    "pydeseq2",
    "deseqdataset",
)
_UNIT_TOKENS = (
    "donor_id",
    "slide_id",
    "orig.ident",
    "sample_id",
    "patient_id",
)


def _iter_reachable_ast_nodes(tree: ast.Module) -> list[ast.AST]:
    """Return AST nodes reachable from top-level statements, pruning dead `if` branches and uncalled functions."""
    defined_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_funcs[stmt.name] = stmt

    reachable: list[ast.AST] = []
    called_funcs: set[str] = set()

    def _visit_stmt_list(stmts: Sequence[ast.stmt]) -> None:
        for stmt in stmts:
            _visit_node(stmt)

    def _visit_node(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Do not traverse function bodies until the function is actually called
            return
        if isinstance(node, ast.If):
            reachable.append(node.test)
            _visit_expr_children(node.test)
            if isinstance(node.test, ast.Constant):
                if bool(node.test.value):
                    _visit_stmt_list(node.body)
                else:
                    _visit_stmt_list(node.orelse)
                return
            _visit_stmt_list(node.body)
            _visit_stmt_list(node.orelse)
            return
        if isinstance(node, ast.While):
            reachable.append(node.test)
            _visit_expr_children(node.test)
            if isinstance(node.test, ast.Constant) and not bool(node.test.value):
                _visit_stmt_list(node.orelse)
                return
            _visit_stmt_list(node.body)
            _visit_stmt_list(node.orelse)
            return
        reachable.append(node)
        for child in ast.iter_child_nodes(node):
            _visit_node(child)
        if isinstance(node, ast.Call):
            fn_name = ""
            if isinstance(node.func, ast.Name):
                fn_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fn_name = node.func.attr
            if fn_name in defined_funcs and fn_name not in called_funcs:
                called_funcs.add(fn_name)
                fn_def = defined_funcs[fn_name]
                _visit_stmt_list(fn_def.body)

    def _visit_expr_children(expr: ast.AST) -> None:
        for child in ast.iter_child_nodes(expr):
            _visit_node(child)

    _visit_stmt_list(tree.body)
    return reachable


def _extract_call_string_literals(call: ast.Call) -> list[str]:
    out: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value.lower())
        elif isinstance(arg, (ast.List, ast.Tuple, ast.Set)):
            for elt in arg.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value.lower())
    for kw in call.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            out.append(kw.value.value.lower())
        elif isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
            for elt in kw.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value.lower())
    return out


def _inspect_statistical_mechanism(script_text: str) -> bool:
    """Verify via reachable AST inspection that valid unit aggregation or null calibration is executed."""
    try:
        tree = ast.parse(script_text)
    except SyntaxError:
        return False

    defined_func_names = {
        stmt.name.lower()
        for stmt in tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reachable_nodes = _iter_reachable_ast_nodes(tree)
    calls = [n for n in reachable_nodes if isinstance(n, ast.Call)]
    if not calls:
        return False

    has_strong_call = False
    has_unit_groupby = False
    has_agg_reducer = False
    has_cell_level_de = False
    has_clustering_call = False
    has_rank_genes_call = False
    has_double_dip_guard = False

    for call in calls:
        fn_name = ""
        if isinstance(call.func, ast.Name):
            fn_name = call.func.id.lower()
        elif isinstance(call.func, ast.Attribute):
            fn_name = call.func.attr.lower()
        if not fn_name:
            continue

        arg_strs = _extract_call_string_literals(call)

        if fn_name in {"leiden", "louvain", "kmeans"}:
            has_clustering_call = True
        if any(
            g in fn_name
            for g in (
                "check_homogeneous_null",
                "count_split",
                "binomial",
                "thinning",
                "permutation",
                "selective_inference",
            )
        ):
            has_double_dip_guard = True

        if fn_name == "wilcoxon_cells":
            has_cell_level_de = True
        elif fn_name == "rank_genes_groups":
            has_rank_genes_call = True
            is_unit_grouped = any(u in s for s in arg_strs for u in _UNIT_TOKENS)
            is_cluster_grouped = any(
                c in s for s in arg_strs for c in ("leiden", "louvain", "kmeans", "cluster")
            )
            if not is_unit_grouped and not is_cluster_grouped:
                has_cell_level_de = True

        # Check strong mechanism call (excluding user-defined functions unless their body also qualifies)
        if fn_name not in defined_func_names and fn_name not in {"print", "len", "str", "repr", "format"}:
            if any(tok in fn_name for tok in _STRONG_MECHANISM_TOKENS):
                has_strong_call = True

        # Check unit grouping: e.g. df.groupby('donor_id') or pivot_table(..., index='donor_id')
        if fn_name in {"groupby", "pivot_table"} and any(
            u in s for s in arg_strs for u in _UNIT_TOKENS
        ):
            has_unit_groupby = True

        # Check aggregation reducer on a grouped object or method call (never bare builtin sum([1, 2]))
        if isinstance(call.func, ast.Attribute) and fn_name in {
            "sum",
            "mean",
            "median",
            "agg",
            "aggregate",
        }:
            has_agg_reducer = True
            # If the receiver of .sum()/.mean()/.agg() is directly a .groupby('donor_id') call:
            recv = call.func.value
            if isinstance(recv, ast.Call):
                r_name = (
                    recv.func.attr.lower()
                    if isinstance(recv.func, ast.Attribute)
                    else (recv.func.id.lower() if isinstance(recv.func, ast.Name) else "")
                )
                if r_name == "groupby" and any(
                    u in s for s in _extract_call_string_literals(recv) for u in _UNIT_TOKENS
                ):
                    has_unit_groupby = True

    has_unit_aggregation = has_unit_groupby and has_agg_reducer
    if has_cell_level_de:
        return False
    if has_rank_genes_call and not (has_unit_aggregation or has_double_dip_guard):
        return False
    if has_clustering_call and has_rank_genes_call and not has_double_dip_guard:
        return False

    return bool(has_strong_call or has_unit_aggregation or has_double_dip_guard)


def _script_reads_workspace_data(script_text: str) -> bool:
    """Return True if reachable AST statements read workspace input data."""
    try:
        tree = ast.parse(script_text)
    except SyntaxError:
        return False

    reachable_nodes = _iter_reachable_ast_nodes(tree)
    read_fn_names = {
        "read_csv",
        "read_tsv",
        "read_table",
        "read_h5ad",
        "read_text",
        "read_bytes",
        "open",
        "reader",
        "dictreader",
        "loadtxt",
        "genfromtxt",
        "load",
        "read_parquet",
        "read_excel",
    }
    for node in reachable_nodes:
        if isinstance(node, ast.Call):
            fn_name = ""
            if isinstance(node.func, ast.Name):
                fn_name = node.func.id.lower()
            elif isinstance(node.func, ast.Attribute):
                fn_name = node.func.attr.lower()
            if fn_name == "open":
                lits = _extract_call_string_literals(node)
                mode_arg = ""
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    mode_arg = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        mode_arg = kw.value.value
                if not any(ch in mode_arg for ch in "wax+") and not any(s.startswith("output/") for s in lits):
                    return True
            elif fn_name in read_fn_names:
                return True
            if fn_name not in {"print", "len", "str", "repr", "write_text", "write_bytes", "to_csv", "write_h5ad"}:
                for s in _extract_call_string_literals(node):
                    if not s.startswith("output/") and ("data/" in s or s.endswith((".csv", ".tsv", ".h5ad"))):
                        return True
    return False


class E4NullTwinDGPGrader:
    """Grades Null/Twin DGP runs on FDCR, empirical FDP, Twin recall, clean-room replay, and AST mechanism."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        script_path = submission.workspace / "output" / "reproduce.py"
        results_path = submission.workspace / "output" / "results.json"
        truth_path = oracle / "truth.json"

        if not script_path.is_file() or not results_path.is_file() or not truth_path.is_file():
            return Grade(
                passed=False,
                score=0.0,
                metrics={"valid_mechanism": False, "clean_room_pass": False},
                detail="missing reproduce.py, results.json, or oracle/truth.json",
            )
        try:
            script_text = script_path.read_text(encoding="utf-8")
            results = json.loads(results_path.read_text(encoding="utf-8"))
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return Grade(
                passed=False,
                score=0.0,
                metrics={"valid_mechanism": False, "clean_room_pass": False},
                detail=f"malformed JSON or script: {exc}",
            )

        mode = str(truth.get("mode", "null")).lower()
        valid_mechanism = _inspect_statistical_mechanism(script_text)
        data_dir = submission.workspace / "data"
        oracle_data_dir = oracle / "data"
        has_workspace_data = (
            data_dir.is_dir() and any(f.is_file() for f in data_dir.rglob("*"))
        ) or (
            oracle_data_dir.is_dir() and any(f.is_file() for f in oracle_data_dir.rglob("*"))
        )
        if has_workspace_data and not _script_reads_workspace_data(script_text):
            valid_mechanism = False

        # Execute output/reproduce.py in a clean-room temp directory with data/ staged and NO pre-existing output/results.json
        clean_room_pass = False
        with tempfile.TemporaryDirectory(prefix="e4-cleanroom-") as tmp:
            clean_root = Path(tmp)
            for d_src in (oracle_data_dir, data_dir):
                if d_src.is_dir():
                    (clean_root / "data").mkdir(parents=True, exist_ok=True)
                    for item in d_src.rglob("*"):
                        rel_item = item.relative_to(d_src)
                        dst_item = clean_root / "data" / rel_item
                        resolved = item.resolve()
                        if resolved.is_dir():
                            dst_item.mkdir(parents=True, exist_ok=True)
                        elif resolved.is_file():
                            dst_item.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(resolved, dst_item)
            (clean_root / "output").mkdir(parents=True, exist_ok=True)
            clean_script = clean_root / "output" / "reproduce.py"
            shutil.copy2(script_path, clean_script)

            import os

            env = dict(os.environ)
            repo_root = str(Path(__file__).resolve().parents[2])
            existing_py = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{repo_root}:{clean_root}" + (
                f":{existing_py}" if existing_py else ""
            )

            try:
                proc = subprocess.run(
                    [sys.executable, str(clean_script)],
                    cwd=str(clean_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                alt_py = Path("/tmp/omicsclaw-de-check-env/bin/python")
                if (
                    proc.returncode != 0
                    and "ModuleNotFoundError" in (proc.stderr or "")
                    and alt_py.is_file()
                    and str(alt_py) != sys.executable
                ):
                    proc = subprocess.run(
                        [str(alt_py), str(clean_script)],
                        cwd=str(clean_root),
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                clean_res_path = clean_root / "output" / "results.json"
                if proc.returncode == 0 and clean_res_path.is_file():
                    clean_res = json.loads(clean_res_path.read_text(encoding="utf-8"))
                    clean_sig = list(clean_res.get("significant_genes", []))
                    clean_n = int(clean_res.get("n_significant", len(clean_sig)))
                    ws_sig = list(results.get("significant_genes", []))
                    ws_n = int(results.get("n_significant", len(ws_sig)))
                    if clean_n == ws_n and set(clean_sig) == set(ws_sig):
                        clean_room_pass = True
            except Exception:
                clean_room_pass = False

        if not clean_room_pass:
            valid_mechanism = False

        report_path = submission.workspace / "output" / "REPORT.md"
        report_text = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
        combined_lower = (script_text + "\n" + report_text).lower()
        noticed = valid_mechanism or any(
            tok in combined_lower
            for tok in (*_STRONG_MECHANISM_TOKENS, *_UNIT_TOKENS)
        )

        sig_genes = list(results.get("significant_genes", []))
        n_sig = int(results.get("n_significant", len(sig_genes)))

        if mode == "null":
            fdcr = 0.0 if (n_sig == 0 and len(sig_genes) == 0) else 1.0
            if valid_mechanism and fdcr == 0.0:
                notice_act = "noticed_and_acted"
            elif noticed:
                notice_act = "noticed_but_ignored"
            else:
                notice_act = "unnoticed"
            passed = (fdcr == 0.0) and valid_mechanism and clean_room_pass
            score = 1.0 if passed else 0.0
            detail = (
                f"null passed (fdcr=0.0, mechanism={notice_act})"
                if passed
                else f"null failed (fdcr={fdcr}, n_significant={n_sig}, valid_mechanism={valid_mechanism}, clean_room_pass={clean_room_pass}, notice_act={notice_act})"
            )
            return Grade(
                passed=passed,
                score=score,
                metrics={
                    "mode": "null",
                    "fdcr": fdcr,
                    "n_significant": n_sig,
                    "valid_mechanism": valid_mechanism,
                    "clean_room_pass": clean_room_pass,
                    "clean_room_replay_verified": clean_room_pass,
                    "notice_act": notice_act,
                },
                detail=detail,
            )

        true_set = set(truth.get("true_genes", []))
        sig_set = set(sig_genes)
        tp = len(sig_set & true_set)
        fp = len(sig_set - true_set)
        empirical_fdp = fp / max(len(sig_set), 1)
        recall = tp / max(len(true_set), 1)
        precision = 1.0 - empirical_fdp if sig_set else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

        passed = (empirical_fdp <= 0.15) and (recall >= 0.40) and valid_mechanism and clean_room_pass
        detail = (
            f"twin passed (fdp={empirical_fdp:.3f}, recall={recall:.3f}, f1={f1:.3f})"
            if passed
            else f"twin failed (fdp={empirical_fdp:.3f}, recall={recall:.3f}, valid_mechanism={valid_mechanism}, clean_room_pass={clean_room_pass})"
        )
        return Grade(
            passed=passed,
            score=round(f1 if (valid_mechanism and clean_room_pass) else 0.0, 4),
            metrics={
                "mode": "twin",
                "empirical_fdp": round(empirical_fdp, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "valid_mechanism": valid_mechanism,
                "clean_room_pass": clean_room_pass,
                "clean_room_replay_verified": clean_room_pass,
            },
            detail=detail,
        )

    def controls(self) -> Sequence[Control]:
        true_genes = [f"GENE_{i:03d}" for i in range(10)]
        header = "cell_id,donor_id,condition," + ",".join(true_genes)
        null_counts_csv = "\n".join(
            [
                header,
                "c1,d1,ctrl," + ",".join(["5"] * 10),
                "c2,d1,ctrl," + ",".join(["5"] * 10),
                "c3,d2,ctrl," + ",".join(["5"] * 10),
                "c4,d2,ctrl," + ",".join(["5"] * 10),
                "c5,d3,case," + ",".join(["5"] * 10),
                "c6,d3,case," + ",".join(["5"] * 10),
                "c7,d4,case," + ",".join(["5"] * 10),
                "c8,d4,case," + ",".join(["5"] * 10),
            ]
        ) + "\n"
        twin_case_vals = ["25"] * 8 + ["5"] * 2
        counts_csv = "\n".join(
            [
                header,
                "c1,d1,ctrl," + ",".join(["5"] * 10),
                "c2,d1,ctrl," + ",".join(["5"] * 10),
                "c3,d2,ctrl," + ",".join(["5"] * 10),
                "c4,d2,ctrl," + ",".join(["5"] * 10),
                "c5,d3,case," + ",".join(twin_case_vals),
                "c6,d3,case," + ",".join(twin_case_vals),
                "c7,d4,case," + ",".join(twin_case_vals),
                "c8,d4,case," + ",".join(twin_case_vals),
            ]
        ) + "\n"
        pb_script = (
            "import csv, json\n"
            "from pathlib import Path\n"
            "def run_pseudobulk(csv_path):\n"
            "    try:\n"
            "        import pandas as pd\n"
            "        df = pd.read_csv(csv_path)\n"
            "        pb = df.groupby(['donor_id', 'condition']).sum(numeric_only=True)\n"
            "    except ImportError:\n"
            "        pass\n"
            "    with open(csv_path, newline='', encoding='utf-8') as fh:\n"
            "        rows = list(csv.DictReader(fh))\n"
            "    genes = [k for k in rows[0].keys() if k.startswith('GENE_')]\n"
            "    donors = {}\n"
            "    for r in rows:\n"
            "        key = (r['donor_id'], r['condition'])\n"
            "        acc = donors.setdefault(key, {g: 0.0 for g in genes})\n"
            "        for g in genes:\n"
            "            acc[g] += float(r[g])\n"
            "    sig = []\n"
            "    for g in genes:\n"
            "        case_v = [v[g] for (_, c), v in donors.items() if c == 'case']\n"
            "        ctrl_v = [v[g] for (_, c), v in donors.items() if c == 'ctrl']\n"
            "        if case_v and ctrl_v and (sum(case_v)/len(case_v) - sum(ctrl_v)/len(ctrl_v)) >= 20.0:\n"
            "            sig.append(g)\n"
            "    return sig\n"
            "sig = run_pseudobulk('data/counts.csv')\n"
            "Path('output').mkdir(parents=True, exist_ok=True)\n"
            "Path('output/results.json').write_text(json.dumps({'n_significant': len(sig), 'significant_genes': sig}), encoding='utf-8')\n"
        )
        pb_null_script = pb_script
        pb_twin_script = pb_script
        return [
            Control(
                name="e4-pass-null-pseudobulk-abstention",
                expect_pass=True,
                deliverables={
                    "data/counts.csv": null_counts_csv,
                    "output/reproduce.py": pb_null_script,
                    "output/results.json": json.dumps({"n_significant": 0, "significant_genes": []}),
                },
                oracle={"truth.json": json.dumps({"mode": "null", "paradigm": "pseudorep", "true_genes": []})},
            ),
            Control(
                name="e4-pass-twin-pseudobulk-recovery",
                expect_pass=True,
                deliverables={
                    "data/counts.csv": counts_csv,
                    "output/reproduce.py": pb_twin_script,
                    "output/results.json": json.dumps(
                        {"n_significant": 8, "significant_genes": true_genes[:8]}
                    ),
                },
                oracle={"truth.json": json.dumps({"mode": "twin", "paradigm": "pseudorep", "true_genes": true_genes})},
            ),
            Control(
                name="e4-fail-null-pseudoreplication-false-discovery",
                expect_pass=False,
                deliverables={
                    "data/counts.csv": counts_csv,
                    "output/reproduce.py": "print('cell-level wilcoxon')\n",
                    "output/results.json": json.dumps(
                        {"n_significant": 3, "significant_genes": ["GENE_001", "GENE_002", "GENE_003"]}
                    ),
                },
                oracle={"truth.json": json.dumps({"mode": "null", "paradigm": "pseudorep", "true_genes": []})},
            ),
            Control(
                name="e4-fail-null-trivial-zero-without-mechanism",
                expect_pass=False,
                deliverables={
                    "data/counts.csv": counts_csv,
                    "output/reproduce.py": "# hardcoded empty output without donor aggregation\nx = 1\n",
                    "output/results.json": json.dumps({"n_significant": 0, "significant_genes": []}),
                },
                oracle={"truth.json": json.dumps({"mode": "null", "paradigm": "pseudorep", "true_genes": []})},
            ),
            Control(
                name="e4-fail-twin-high-fdp",
                expect_pass=False,
                deliverables={
                    "data/counts.csv": counts_csv,
                    "output/reproduce.py": pb_twin_script,
                    "output/results.json": json.dumps(
                        {
                            "n_significant": 10,
                            "significant_genes": [
                                "GENE_000",
                                "GENE_001",
                                "FP_1",
                                "FP_2",
                                "FP_3",
                                "FP_4",
                                "FP_5",
                                "FP_6",
                                "FP_7",
                                "FP_8",
                            ],
                        }
                    ),
                },
                oracle={"truth.json": json.dumps({"mode": "twin", "paradigm": "pseudorep", "true_genes": true_genes})},
            ),
        ]

