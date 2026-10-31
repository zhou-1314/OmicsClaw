"""Artifact checks that retain failure and unavailable-dependency evidence."""

import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from .models import Verdict, Verification


def verify_file(path, checksum=None):
    if urlparse(str(path)).scheme in {"remote", "ssh", "s3", "http", "https"}:
        return Verification(
            check="file_exists",
            verdict=Verdict.INCONCLUSIVE,
            evidence_ref=str(path),
            details="remote artifact has not been fetched",
        )
    target = Path(path)
    try:
        if not target.is_file():
            return Verification(
                check="file_exists", verdict=Verdict.FAIL, evidence_ref=str(target)
            )
        if checksum:
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            return Verification(
                check="checksum",
                verdict=Verdict.PASS if actual == checksum else Verdict.FAIL,
                evidence_ref=actual,
            )
        return Verification(
            check="file_exists", verdict=Verdict.PASS, evidence_ref=str(target)
        )
    except OSError as exc:
        return Verification(
            check="file_exists",
            verdict=Verdict.FAIL,
            evidence_ref=str(target),
            details=str(exc),
        )


def verify_json(path, required=(), *, schema=None):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return Verification(
            check="json_schema",
            verdict=Verdict.FAIL,
            evidence_ref=str(path),
            details=str(exc),
        )
    if schema:
        try:
            import jsonschema
        except ImportError:
            return Verification(
                check="json_schema",
                verdict=Verdict.INCONCLUSIVE,
                evidence_ref=str(path),
                details="jsonschema unavailable",
            )
        try:
            jsonschema.validate(data, schema)
        except jsonschema.SchemaError as exc:
            return Verification(
                check="json_schema",
                verdict=Verdict.INCONCLUSIVE,
                evidence_ref=str(path),
                details=exc.message,
            )
        except jsonschema.ValidationError as exc:
            return Verification(
                check="json_schema",
                verdict=Verdict.FAIL,
                evidence_ref=str(path),
                details=exc.message,
            )
    if required and not isinstance(data, dict):
        return Verification(
            check="json_schema",
            verdict=Verdict.FAIL,
            evidence_ref=str(path),
            details="expected JSON object",
        )
    missing = [key for key in required if key not in data]
    return Verification(
        check="json_schema",
        verdict=Verdict.WARN if missing else Verdict.PASS,
        evidence_ref=",".join(missing),
    )


def verify_csv(path, required_columns=()):
    try:
        with Path(path).open(newline="") as handle:
            columns = next(csv.reader(handle))
    except (OSError, ValueError, StopIteration) as exc:
        return Verification(
            check="csv_schema",
            verdict=Verdict.FAIL,
            evidence_ref=str(path),
            details=str(exc),
        )
    missing = [column for column in required_columns if column not in columns]
    return Verification(
        check="csv_schema",
        verdict=Verdict.WARN if missing else Verdict.PASS,
        evidence_ref=",".join(missing),
    )


def verify_exit_code(code):
    try:
        value = int(code)
    except (TypeError, ValueError):
        return Verification(
            check="exit_code", verdict=Verdict.INCONCLUSIVE, evidence_ref=str(code)
        )
    return Verification(
        check="exit_code",
        verdict=Verdict.PASS if value == 0 else Verdict.FAIL,
        evidence_ref=str(value),
    )


def verify_artifact(path, checksum=None, required_json=(), *, schema=None):
    if urlparse(str(path)).scheme in {"remote", "ssh", "s3", "http", "https"}:
        return verify_file(path, checksum)
    suffix = Path(path).suffix.lower()
    schema = schema or {}
    if suffix == ".json":
        return verify_json(path, required_json, schema=schema)
    if suffix == ".csv":
        return verify_csv(path, schema.get("required_columns", required_json))
    if suffix == ".h5ad":
        return verify_h5ad(
            path,
            required_obs=schema.get("required_obs", required_json),
            required_vars=schema.get("required_vars", ()),
            min_shape=schema.get("min_shape"),
        )
    return verify_file(path, checksum)


def verify_h5ad(
    path, required_dims=(), *, required_obs=(), required_vars=(), min_shape=None
):
    try:
        import anndata
    except ImportError:
        return Verification(
            check="h5ad_schema",
            verdict=Verdict.INCONCLUSIVE,
            evidence_ref=str(path),
            details="anndata unavailable",
        )
    data = None
    try:
        data = anndata.read_h5ad(Path(path), backed="r")
        missing_dims = [name for name in required_dims if not hasattr(data, name)]
        missing_obs = [name for name in required_obs if name not in data.obs]
        missing_vars = [name for name in required_vars if name not in data.var]
        shape = tuple(int(value) for value in data.shape)
        too_small = bool(
            min_shape and (shape[0] < int(min_shape[0]) or shape[1] < int(min_shape[1]))
        )
        details = json.dumps(
            {
                "shape": shape,
                "missing_dims": missing_dims,
                "missing_obs": missing_obs,
                "missing_vars": missing_vars,
            },
            sort_keys=True,
        )
        verdict = (
            Verdict.FAIL
            if too_small
            else (
                Verdict.WARN
                if missing_dims or missing_obs or missing_vars
                else Verdict.PASS
            )
        )
        return Verification(check="h5ad_schema", verdict=verdict, evidence_ref=details)
    except Exception as exc:  # noqa: BLE001 - malformed HDF5 data has backend-specific errors
        return Verification(
            check="h5ad_schema",
            verdict=Verdict.FAIL,
            evidence_ref=str(path),
            details=str(exc),
        )
    finally:
        if data is not None and getattr(data, "file", None) is not None:
            data.file.close()
import math
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_float(val: str) -> float | None:
    text = val.strip()
    if not text or text.lower() in {"na", "nan", "null", "none", ""}:
        return math.nan
    if text.lower() in {"inf", "+inf", "infinity", "+infinity"}:
        return math.inf
    if text.lower() in {"-inf", "-infinity"}:
        return -math.inf
    try:
        return float(text)
    except ValueError:
        return None


def _floats_close(a: float, b: float, *, rtol: float = 1e-4, atol: float = 1e-5) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isinf(a) or math.isinf(b):
        return a == b
    return math.isclose(a, b, rel_tol=rtol, abs_tol=atol)


def verify_table_equivalence(
    expected_path: str | Path,
    actual_path: str | Path,
    *,
    rtol: float = 1e-4,
    atol: float = 1e-5,
    jaccard_min: float = 0.99,
    id_column: str | None = None,
) -> Verification:
    exp_file = Path(expected_path)
    act_file = Path(actual_path)
    if not exp_file.is_file() or not act_file.is_file():
        return Verification(
            check="table_equivalence",
            verdict=Verdict.FAIL,
            evidence_ref=f"{exp_file} vs {act_file}",
            details="missing table file",
        )
    delimiter = "\t" if exp_file.suffix.lower() in {".tsv", ".txt"} else ","
    try:
        with exp_file.open(newline="", encoding="utf-8") as fh:
            exp_rows = list(csv.reader(fh, delimiter=delimiter))
        with act_file.open(newline="", encoding="utf-8") as fh:
            act_rows = list(csv.reader(fh, delimiter=delimiter))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        return Verification(
            check="table_equivalence",
            verdict=Verdict.FAIL,
            evidence_ref=str(act_file),
            details=str(exc),
        )
    if not exp_rows or not act_rows:
        verdict = Verdict.PASS if exp_rows == act_rows else Verdict.FAIL
        return Verification(
            check="table_equivalence",
            verdict=verdict,
            evidence_ref=str(act_file),
            details="empty table comparison",
        )
    exp_header, exp_body = exp_rows[0], exp_rows[1:]
    act_header, act_body = act_rows[0], act_rows[1:]
    if exp_header != act_header:
        return Verification(
            check="table_equivalence",
            verdict=Verdict.FAIL,
            evidence_ref=str(act_file),
            details=f"header mismatch: {exp_header} != {act_header}",
        )
    if len(exp_body) != len(act_body):
        return Verification(
            check="table_equivalence",
            verdict=Verdict.FAIL,
            evidence_ref=str(act_file),
            details=f"row count mismatch: {len(exp_body)} != {len(act_body)}",
        )
    if not exp_body:
        return Verification(
            check="table_equivalence",

            verdict=Verdict.PASS,
            evidence_ref=str(act_file),
            details="headers match, 0 rows",
        )

    id_idx = None
    if id_column and id_column in exp_header:
        id_idx = exp_header.index(id_column)
    else:
        candidates = {
            "gene",
            "gene_id",
            "symbol",
            "names",
            "feature",
            "id",
            "cluster",
            "cell_id",
            "spot_id",
            "",
        }
        for idx, col in enumerate(exp_header):
            if col.strip().lower() in candidates:
                id_idx = idx
                break
        if id_idx is None and _parse_float(exp_body[0][0]) is None:
            id_idx = 0

    jaccard = 1.0
    aligned_pairs: list[tuple[list[str], list[str]]] = []
    if id_idx is not None:
        exp_ids = [r[id_idx].strip() for r in exp_body if len(r) > id_idx]
        act_ids = [r[id_idx].strip() for r in act_body if len(r) > id_idx]
        exp_set, act_set = set(exp_ids), set(act_ids)
        union_len = len(exp_set | act_set)
        jaccard = (len(exp_set & act_set) / union_len) if union_len else 1.0
        if jaccard < jaccard_min:
            return Verification(
                check="table_equivalence",
                verdict=Verdict.FAIL,
                evidence_ref=str(act_file),
                details=f"id jaccard {jaccard:.4f} < {jaccard_min}",
            )
        if len(exp_set) == len(exp_body) and len(act_set) == len(act_body) and exp_set == act_set:
            act_map = {r[id_idx].strip(): r for r in act_body}
            aligned_pairs = [(r, act_map[r[id_idx].strip()]) for r in exp_body]
        else:
            aligned_pairs = list(zip(exp_body, act_body))
    else:
        aligned_pairs = list(zip(exp_body, act_body))

    for row_idx, (exp_row, act_row) in enumerate(aligned_pairs):
        if len(exp_row) != len(act_row):
            return Verification(
                check="table_equivalence",
                verdict=Verdict.FAIL,
                evidence_ref=str(act_file),
                details=f"column length mismatch at row {row_idx}",
            )
        for col_idx, (ev, av) in enumerate(zip(exp_row, act_row)):
            ef, af = _parse_float(ev), _parse_float(av)
            if ef is not None and af is not None:
                if not _floats_close(ef, af, rtol=rtol, atol=atol):
                    col_name = exp_header[col_idx] if col_idx < len(exp_header) else str(col_idx)
                    return Verification(
                        check="table_equivalence",
                        verdict=Verdict.FAIL,
                        evidence_ref=str(act_file),
                        details=f"numeric mismatch at row {row_idx} col {col_name}: {ev} vs {av}",
                    )
            elif ev.strip() != av.strip():
                col_name = exp_header[col_idx] if col_idx < len(exp_header) else str(col_idx)
                return Verification(
                    check="table_equivalence",
                    verdict=Verdict.FAIL,
                    evidence_ref=str(act_file),
                    details=f"value mismatch at row {row_idx} col {col_name}: {ev!r} vs {av!r}",
                )

    return Verification(
        check="table_equivalence",
        verdict=Verdict.PASS,
        evidence_ref=str(act_file),
        details=json.dumps({"rows": len(exp_body), "jaccard": round(jaccard, 6)}),
    )


def _json_equivalent(a: Any, b: Any, *, rtol: float = 1e-4, atol: float = 1e-5) -> bool:
    volatile = {"created_at", "updated_at", "timestamp", "elapsed_s", "finished_at", "started_at"}
    if type(a) is not type(b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
            return _floats_close(float(a), float(b), rtol=rtol, atol=atol)
        return False
    if isinstance(a, dict):
        ka = {k for k in a if k not in volatile}
        kb = {k for k in b if k not in volatile}
        if ka != kb:
            return False
        return all(_json_equivalent(a[k], b[k], rtol=rtol, atol=atol) for k in ka)
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(_json_equivalent(x, y, rtol=rtol, atol=atol) for x, y in zip(a, b))
    if isinstance(a, float):
        return _floats_close(a, b, rtol=rtol, atol=atol)
    return a == b


def verify_json_equivalence(
    expected_path: str | Path,
    actual_path: str | Path,
    *,
    rtol: float = 1e-4,
    atol: float = 1e-5,
) -> Verification:
    try:
        exp_data = json.loads(Path(expected_path).read_text(encoding="utf-8"))
        act_data = json.loads(Path(actual_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Verification(
            check="json_equivalence",
            verdict=Verdict.FAIL,
            evidence_ref=str(actual_path),
            details=str(exc),
        )
    ok = _json_equivalent(exp_data, act_data, rtol=rtol, atol=atol)
    return Verification(
        check="json_equivalence",
        verdict=Verdict.PASS if ok else Verdict.FAIL,
        evidence_ref=str(actual_path),
        details="" if ok else "JSON payload values diverged",
    )


def compare_h5ad_equivalence(
    path_a: str | Path,
    path_b: str | Path,
    *,
    rtol: float = 1e-4,
    atol: float = 1e-5,
) -> tuple[bool, str]:
    """Compare two ``.h5ad`` files for structural and numerical equivalence."""
    fa = Path(path_a)
    fb = Path(path_b)
    if not fa.is_file() or not fb.is_file():
        return False, f"missing h5ad file: {fa} vs {fb}"

    try:
        import anndata
        import numpy as np
    except ImportError:
        alt_py = Path("/tmp/omicsclaw-de-check-env/bin/python")
        if alt_py.is_file() and str(alt_py) != sys.executable:
            repo_root = str(Path(__file__).resolve().parents[2])
            env = dict(os.environ)
            env["PYTHONPATH"] = f"{repo_root}:{env.get('PYTHONPATH', '')}"
            code = (
                "import json, sys; from omicsclaw.runs.verification import compare_h5ad_equivalence; "
                f"ok, msg = compare_h5ad_equivalence({str(fa.resolve())!r}, {str(fb.resolve())!r}, rtol={rtol!r}, atol={atol!r}); "
                "print(json.dumps({'ok': ok, 'msg': msg}))"
            )
            try:
                proc = subprocess.run(
                    [str(alt_py), "-c", code],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    res = json.loads(proc.stdout.strip().splitlines()[-1])
                    return bool(res.get("ok")), str(res.get("msg", ""))
            except Exception as exc:  # noqa: BLE001
                return False, f"h5ad fallback check failed: {exc}"
        return False, "anndata or numpy unavailable"

    va = verify_h5ad(fa)
    vb = verify_h5ad(fb)
    if va.verdict != Verdict.PASS or vb.verdict != Verdict.PASS:
        return False, f"h5ad schema check failed: {va.details or vb.details}"

    ad_a = None
    ad_b = None
    try:
        ad_a = anndata.read_h5ad(fa)
        ad_b = anndata.read_h5ad(fb)

        if tuple(ad_a.shape) != tuple(ad_b.shape):
            return False, f"shape mismatch: {ad_a.shape} != {ad_b.shape}"

        if list(ad_a.obs.columns) != list(ad_b.obs.columns):
            return False, f"obs columns mismatch: {list(ad_a.obs.columns)} != {list(ad_b.obs.columns)}"
        if [str(x) for x in ad_a.obs.index] != [str(x) for x in ad_b.obs.index]:
            return False, "obs index mismatch"

        if list(ad_a.var.columns) != list(ad_b.var.columns):
            return False, f"var columns mismatch: {list(ad_a.var.columns)} != {list(ad_b.var.columns)}"
        if [str(x) for x in ad_a.var.index] != [str(x) for x in ad_b.var.index]:
            return False, "var index mismatch"

        if set(ad_a.uns.keys()) != set(ad_b.uns.keys()):
            return False, f"uns keys mismatch: {set(ad_a.uns.keys())} != {set(ad_b.uns.keys())}"

        if set(ad_a.obsm.keys()) != set(ad_b.obsm.keys()):
            return False, f"obsm keys mismatch: {set(ad_a.obsm.keys())} != {set(ad_b.obsm.keys())}"
        import pandas as pd

        for k in ad_a.obsm.keys():
            ob_a = ad_a.obsm[k]
            ob_b = ad_b.obsm[k]
            if getattr(ob_a, "shape", None) != getattr(ob_b, "shape", None):
                return False, f"obsm[{k!r}] shape mismatch: {getattr(ob_a, 'shape', None)} != {getattr(ob_b, 'shape', None)}"
            arr_a = ob_a.toarray() if hasattr(ob_a, "toarray") else np.asarray(ob_a)
            arr_b = ob_b.toarray() if hasattr(ob_b, "toarray") else np.asarray(ob_b)
            if (
                isinstance(arr_a.dtype, np.dtype)
                and isinstance(arr_b.dtype, np.dtype)
                and np.issubdtype(arr_a.dtype, np.number)
                and np.issubdtype(arr_b.dtype, np.number)
            ):
                if not np.allclose(arr_a, arr_b, rtol=rtol, atol=atol, equal_nan=True):
                    return False, f"obsm[{k!r}] numeric values diverged"

        xa = ad_a.X
        xb = ad_b.X
        if xa is not None or xb is not None:
            if xa is None or xb is None:
                return False, "one of X matrices is None"
            arr_xa = xa.toarray() if hasattr(xa, "toarray") else np.asarray(xa)
            arr_xb = xb.toarray() if hasattr(xb, "toarray") else np.asarray(xb)
            if arr_xa.shape != arr_xb.shape:
                return False, f"X shape mismatch: {arr_xa.shape} != {arr_xb.shape}"
            if not np.allclose(
                arr_xa.astype(float, copy=False),
                arr_xb.astype(float, copy=False),
                rtol=rtol,
                atol=atol,
                equal_nan=True,
            ):
                max_diff = float(np.nanmax(np.abs(arr_xa.astype(float) - arr_xb.astype(float))))
                return False, f"X mismatch: matrix values diverged (max_abs_diff={max_diff:.6g})"

        for label, df_a, df_b in (("obs", ad_a.obs, ad_b.obs), ("var", ad_a.var, ad_b.var)):
            for col in df_a.columns:
                sa = df_a[col]
                sb = df_b[col]
                if pd.api.types.is_bool_dtype(sa.dtype) and pd.api.types.is_bool_dtype(sb.dtype):
                    if not np.array_equal(sa.to_numpy(dtype=bool), sb.to_numpy(dtype=bool)):
                        return False, f"{label}[{col!r}] boolean values diverged"
                elif (
                    pd.api.types.is_numeric_dtype(sa.dtype)
                    and pd.api.types.is_numeric_dtype(sb.dtype)
                    and not pd.api.types.is_bool_dtype(sa.dtype)
                    and not pd.api.types.is_bool_dtype(sb.dtype)
                ):
                    va_arr = sa.to_numpy(dtype=float, na_value=np.nan)
                    vb_arr = sb.to_numpy(dtype=float, na_value=np.nan)
                    if not np.allclose(va_arr, vb_arr, rtol=rtol, atol=atol, equal_nan=True):
                        return False, f"{label}[{col!r}] numeric values diverged"
                else:
                    la = ["<NA>" if (x is None or (isinstance(x, float) and math.isnan(x))) else str(x) for x in sa.tolist()]
                    lb = ["<NA>" if (x is None or (isinstance(x, float) and math.isnan(x))) else str(x) for x in sb.tolist()]
                    if la != lb:
                        return False, f"{label}[{col!r}] categorical/string values diverged"

        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"h5ad comparison error: {exc}"
    finally:
        if ad_a is not None and getattr(ad_a, "file", None) is not None:
            ad_a.file.close()
        if ad_b is not None and getattr(ad_b, "file", None) is not None:
            ad_b.file.close()


def verify_h5ad_equivalence(
    path_a,
    path_b,
    *,
    rtol: float = 1e-4,
    atol: float = 1e-5,
) -> Verification:
    """Return a Verification verdict comparing two .h5ad files across X, obs, var, uns, and obsm."""
    ok, msg = compare_h5ad_equivalence(path_a, path_b, rtol=rtol, atol=atol)
    return Verification(
        check="h5ad_equivalence",
        verdict=Verdict.PASS if ok else Verdict.FAIL,
        evidence_ref=f"{path_a} vs {path_b}",
        details=msg or None,
    )


def _valid_figure_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return False
    if not head:
        return False
    suffix = path.suffix.lower()
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".pdf":
        return head.startswith(b"%PDF")
    if suffix == ".svg":
        return b"<svg" in head.lower() or b"<?xml" in head.lower()
    return len(head) > 0


def _discover_workspace_outputs(workspace: Path) -> list[str]:
    found: list[str] = []
    skip_names = {"reproduce.py", "reproduce.R", "REPORT.md", "README.md", "replay_receipt.json", "review_brief.md", "manifest.json"}
    for sub in ("output", "results", "figures", "tables"):
        base = workspace / sub
        if not base.is_dir():
            continue
        for file in sorted(base.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(workspace).as_posix()
            if (
                "/provenance/" in f"/{rel}"
                or "/reviews/" in f"/{rel}"
                or "/sidecars/" in f"/{rel}"
                or file.name in skip_names
                or file.name.startswith(".")
            ):
                continue
            found.append(rel)
    return found


def _stage_clean_room_tree(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        dst_item = dst_dir / item.name
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved.is_dir():
            _stage_clean_room_tree(resolved, dst_item)
        elif resolved.is_file():
            try:
                if resolved.stat().st_size > 64 * 1024 * 1024:
                    dst_item.symlink_to(resolved)
                else:
                    shutil.copy2(resolved, dst_item)
            except OSError:
                pass


def run_clean_room_replay(
    script_path: str | Path,
    workspace: str | Path,
    *,
    python: str | None = None,
    timeout_s: float = 180.0,
    tracked_outputs: list[str] | None = None,
    tracked_inputs: list[str] | None = None,
    rtol: float = 1e-4,
    atol: float = 1e-5,
) -> dict[str, Any]:
    ws = Path(workspace).resolve()
    script = Path(script_path)
    if not script.is_absolute():
        script = (ws / script).resolve()
    if not script.is_file():
        return {
            "status": "failed",
            "exit_code": -1,
            "error": f"replay script not found: {script}",
            "verified_outputs": [],
            "changed_outputs": [],
            "missing_outputs": [],
            "orphan_outputs": [],
        }

    expected_rels = list(
        dict.fromkeys(
            tracked_outputs if tracked_outputs is not None else _discover_workspace_outputs(ws)
        )
    )
    disk_outputs = _discover_workspace_outputs(ws)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_replay_") as tmp_dir:
        clean = Path(tmp_dir).resolve()
        for input_dir_name in ("data", "inputs", "raw", "skills"):
            src_dir = ws / input_dir_name
            if src_dir.is_dir():
                _stage_clean_room_tree(src_dir, clean / input_dir_name)
        for sidecar_src in (ws / "provenance" / "sidecars", ws / "output" / "sidecars"):
            if sidecar_src.is_dir():
                _stage_clean_room_tree(sidecar_src, clean / "provenance" / "sidecars")
                _stage_clean_room_tree(sidecar_src, clean / "output" / "sidecars")
        for prov_meta in (
            "provenance/artifacts.json",
            "provenance/findings.json",
            ".omicsclaw/provenance/artifacts.json",
            ".omicsclaw/provenance/findings.json",
        ):
            src_meta = ws / prov_meta
            if src_meta.is_file():
                dst_meta = clean / prov_meta
                dst_meta.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_meta.resolve(), dst_meta)
        for rel_in in tracked_inputs or ():
            norm_in = Path(rel_in).as_posix().lstrip("./")
            if norm_in.startswith(("output/", "results/", "figures/", "tables/", ".omicsclaw/", "provenance/")):
                continue
            src_in = ws / norm_in
            dst_in = clean / norm_in
            if src_in.is_file() and not dst_in.exists():
                dst_in.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_in.resolve(), dst_in)

        for out_dir_name in ("output", "output/tables", "output/figures", "results", "figures", "tables"):
            if (ws / out_dir_name).is_dir():
                (clean / out_dir_name).mkdir(parents=True, exist_ok=True)

        try:
            script_rel = script.relative_to(ws).as_posix()
        except ValueError:
            script_rel = f"output/{script.name}"
        clean_script = clean / script_rel
        clean_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, clean_script)

        repo_root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ)
        existing_py = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{repo_root}:{clean}" + (f":{existing_py}" if existing_py else "")
        env["OMICSCLAW_REPLAY_VERIFY"] = "1"
        env["MPLBACKEND"] = "Agg"
        env["PYTHONHASHSEED"] = "0"

        runner = [python or sys.executable, str(clean_script)]
        if clean_script.suffix.lower() == ".r":
            runner = ["Rscript", str(clean_script)]

        try:
            proc = subprocess.run(
                runner,
                cwd=str(clean),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            exit_code = int(proc.returncode)
            stdout_tail = proc.stdout[-4000:] if proc.stdout else ""
            stderr_tail = proc.stderr[-4000:] if proc.stderr else ""
            alt_py = Path("/tmp/omicsclaw-de-check-env/bin/python")
            if (
                exit_code != 0
                and python is None
                and clean_script.suffix.lower() != ".r"
                and "ModuleNotFoundError" in stderr_tail
                and alt_py.is_file()
                and str(alt_py) != sys.executable
            ):
                proc = subprocess.run(
                    [str(alt_py), str(clean_script)],
                    cwd=str(clean),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    check=False,
                )
                exit_code = int(proc.returncode)
                stdout_tail = proc.stdout[-4000:] if proc.stdout else ""
                stderr_tail = proc.stderr[-4000:] if proc.stderr else ""
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "status": "failed",
                "exit_code": -1,
                "error": str(exc),
                "verified_outputs": [],
                "changed_outputs": list(expected_rels),
                "missing_outputs": list(expected_rels),
                "orphan_outputs": [],
            }

        verified_outputs: list[dict[str, Any]] = []
        changed_outputs: list[str] = []
        missing_outputs: list[str] = []
        comparison_details: dict[str, str] = {}

        all_candidates = list(dict.fromkeys([*expected_rels, *disk_outputs]))
        orphan_outputs: list[str] = []

        for rel in all_candidates:
            norm = Path(rel).as_posix().lstrip("./")
            if norm in {"output/reproduce.py", "output/reproduce.R", "reproduce.py", "reproduce.R"}:
                continue
            ws_file = ws / norm
            clean_file = clean / norm
            if not ws_file.is_file():
                if norm in expected_rels:
                    missing_outputs.append(norm)
                    changed_outputs.append(norm)
                continue
            if not clean_file.is_file():
                if norm in expected_rels:
                    missing_outputs.append(norm)
                    changed_outputs.append(norm)
                    if norm in disk_outputs:
                        orphan_outputs.append(norm)
                else:
                    orphan_outputs.append(norm)
                continue

            ws_sha = sha256_file(ws_file)
            clean_sha = sha256_file(clean_file)
            if ws_sha == clean_sha:
                verified_outputs.append(
                    {
                        "path": norm,
                        "sha256": ws_sha,
                        "replay_sha256": clean_sha,
                        "match_mode": "sha256",
                    }
                )
                continue

            suffix = ws_file.suffix.lower()
            if suffix in {".csv", ".tsv"}:
                cmp_res = verify_table_equivalence(ws_file, clean_file, rtol=rtol, atol=atol)
                if cmp_res.verdict == Verdict.PASS:
                    verified_outputs.append(
                        {
                            "path": norm,
                            "sha256": ws_sha,
                            "replay_sha256": clean_sha,
                            "match_mode": "table_equivalence",
                        }
                    )
                else:
                    changed_outputs.append(norm)
                    comparison_details[norm] = cmp_res.details
            elif suffix == ".json":
                cmp_res = verify_json_equivalence(ws_file, clean_file, rtol=rtol, atol=atol)
                if cmp_res.verdict == Verdict.PASS:
                    verified_outputs.append(
                        {
                            "path": norm,
                            "sha256": ws_sha,
                            "replay_sha256": clean_sha,
                            "match_mode": "json_equivalence",
                        }
                    )
                else:
                    changed_outputs.append(norm)
                    comparison_details[norm] = cmp_res.details
            elif suffix in {".png", ".pdf", ".svg"}:
                if _valid_figure_file(ws_file) and _valid_figure_file(clean_file):
                    ws_size = ws_file.stat().st_size
                    clean_size = clean_file.stat().st_size
                    ratio = min(ws_size, clean_size) / max(ws_size, clean_size, 1)
                    if ratio >= 0.5:
                        verified_outputs.append(
                            {
                                "path": norm,
                                "sha256": ws_sha,
                                "replay_sha256": clean_sha,
                                "match_mode": "figure_regenerated",
                            }
                        )
                    else:
                        changed_outputs.append(norm)
                        comparison_details[norm] = f"figure size diverged: {ws_size} vs {clean_size}"
                else:
                    changed_outputs.append(norm)
                    comparison_details[norm] = "invalid figure header"
            elif suffix == ".h5ad":
                ok_h5, reason_h5 = compare_h5ad_equivalence(
                    ws_file, clean_file, rtol=rtol, atol=atol
                )
                if ok_h5:
                    verified_outputs.append(
                        {
                            "path": norm,
                            "sha256": ws_sha,
                            "replay_sha256": clean_sha,
                            "match_mode": "h5ad_equivalence",
                        }
                    )
                else:
                    changed_outputs.append(norm)
                    comparison_details[norm] = reason_h5 or "h5ad content diverged"
            else:
                changed_outputs.append(norm)
                comparison_details[norm] = "sha256 mismatch"

        ok = (
            exit_code == 0
            and not changed_outputs
            and not missing_outputs
            and not orphan_outputs
        )
        return {
            "status": "ok" if ok else "failed",
            "exit_code": exit_code,
            "script": script_rel,
            "script_sha256": sha256_file(script),
            "verified_outputs": verified_outputs,
            "changed_outputs": sorted(set(changed_outputs)),
            "missing_outputs": sorted(set(missing_outputs)),
            "orphan_outputs": sorted(set(orphan_outputs)),
            "comparison_details": comparison_details,
            "stdout": stdout_tail,
            "stderr": stderr_tail,
        }


def verify_replay_receipt(
    path: str | Path,
    workspace: str | Path | None = None,
    *,
    require_reexecution: bool = False,
) -> Verification:
    receipt_path = Path(path)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Verification(
            check="replay_receipt",
            verdict=Verdict.FAIL,
            evidence_ref=str(receipt_path),
            details=str(exc),
        )
    if not isinstance(receipt, dict):
        return Verification(
            check="replay_receipt",
            verdict=Verdict.FAIL,
            evidence_ref=str(receipt_path),
            details="receipt is not a JSON object",
        )
    ws = Path(workspace) if workspace is not None else Path(receipt.get("workspace") or receipt_path.resolve().parent.parent)
    issues: list[str] = []
    if require_reexecution:
        v_mode = str(receipt.get("verification_mode") or receipt.get("mode") or "")
        if v_mode in ("trace", "ledger_trace") and not receipt.get("recomputed"):
            issues.append(f"re-execution required but receipt mode={v_mode!r}")
    if receipt.get("status") != "ok":
        issues.append(f"receipt status={receipt.get('status')!r}")
    if receipt.get("exit_code", 0) != 0:
        issues.append(f"exit_code={receipt.get('exit_code')!r}")
    for field_name in ("changed_outputs", "missing_outputs", "orphan_outputs", "stale_inputs", "unregistered_reads", "modified_helper_files", "modified_Helper_files"):
        items = receipt.get(field_name) or []
        if items:
            issues.append(f"{field_name}={items}")

    script_rel = receipt.get("script")
    script_sha = receipt.get("script_sha256")
    if script_rel and script_sha:
        script_file = ws / script_rel
        if not script_file.is_file() or sha256_file(script_file) != script_sha:
            issues.append(f"replay script modified or missing: {script_rel}")

    for inp in receipt.get("inputs", []):
        if isinstance(inp, dict) and inp.get("path") and inp.get("sha256"):
            inp_file = ws / inp["path"]
            if not inp_file.is_file() or sha256_file(inp_file) != inp["sha256"]:
                issues.append(f"stale input modified since replay: {inp['path']}")

    for out in receipt.get("verified_outputs", []):
        if isinstance(out, dict) and out.get("path") and out.get("sha256"):
            out_file = ws / out["path"]
            if not out_file.is_file() or sha256_file(out_file) != out["sha256"]:
                issues.append(f"verified output modified or deleted since replay: {out['path']}")

    for helper in receipt.get("skill_files", []):
        if isinstance(helper, dict) and helper.get("path") and helper.get("sha256"):
            h_file = Path(helper["path"])
            if not h_file.is_absolute():
                if (ws / h_file).is_file():
                    h_file = ws / h_file
                else:
                    from omicsclaw.skills.sidecar import DEFAULT_SKILLS_ROOT

                    for base in (DEFAULT_SKILLS_ROOT.parent, DEFAULT_SKILLS_ROOT):
                        if (base / h_file).is_file():
                            h_file = base / h_file
                            break
                    else:
                        h_file = ws / h_file
            if not h_file.is_file() or sha256_file(h_file) != helper["sha256"]:
                issues.append(f"sidecar skill file modified since execution: {helper['path']}")

    current_disk = set(_discover_workspace_outputs(ws))
    recorded_outputs = {
        out["path"]
        for out in receipt.get("verified_outputs", [])
        if isinstance(out, dict) and out.get("path")
    }
    if recorded_outputs:
        new_orphans = sorted(current_disk - recorded_outputs)
        if new_orphans:
            issues.append(f"untracked orphan outputs in workspace: {new_orphans}")

    report_sha = receipt.get("report_sha256")
    report_rel = receipt.get("report_path")
    if report_rel and report_sha:
        rep_file = ws / report_rel
        if not rep_file.is_file() or sha256_file(rep_file) != report_sha:
            issues.append(f"report modified after replay receipt: {report_rel}")
        elif rep_file.is_file():
            try:
                rep_txt = rep_file.read_text(encoding="utf-8", errors="ignore")
                if "{{artifact:" in rep_txt or "{{!artifact:" in rep_txt:
                    mv = verify_artifact_markers(rep_file, workspace=ws)
                    if mv.verdict == Verdict.FAIL:
                        issues.append(f"broken artifact marker in report: {mv.details}")
            except OSError:
                pass

    return Verification(
        check="replay_receipt",
        verdict=Verdict.FAIL if issues else Verdict.PASS,
        evidence_ref=str(receipt_path),
        details="; ".join(issues),
    )


import re as _re
import sqlite3 as _sqlite3


def _verification_passed(self: Verification) -> bool:
    return self.verdict == Verdict.PASS


def _verification_getitem(self: Verification, key: str) -> Any:
    if key == "passed":
        return self.verdict == Verdict.PASS
    if key in ("verdict", "check", "evidence_ref", "details", "artifact_id", "run_id", "job_id", "reviewer"):
        return getattr(self, key)
    meta = getattr(self, "_meta", None)
    if isinstance(meta, dict) and key in meta:
        return meta[key]
    raise KeyError(key)


Verification.passed = property(_verification_passed)  # type: ignore[attr-defined]
Verification.__getitem__ = _verification_getitem  # type: ignore[attr-defined]

_VALID_ARTIFACT_ID_RE = _re.compile(
    r"^(?:art_)?[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
)
_ARTIFACT_MARKER_IN_DOC_RE = _re.compile(r"\{\{(!?)artifact:([^}]*)\}\}")
_MD_IMAGE_RE = _re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def is_valid_artifact_id(raw_id: str) -> bool:
    """Return True if *raw_id* matches Claude Science's well-formed artifact/version id rules."""
    if not isinstance(raw_id, str) or not raw_id:
        return False
    if any(ch in raw_id for ch in ("'", '"', "[", "]", "{", "}", " ", "\t", "\n", "\r", "\\", "..")):
        return False
    if raw_id.startswith("/") or raw_id.endswith("/"):
        return False
    return bool(_VALID_ARTIFACT_ID_RE.fullmatch(raw_id))


def resolve_artifact_reference(
    ref_id: str,
    workspace: str | Path,
    *,
    artifact_store: Any = None,
    registry: Any = None,
) -> Path | None:
    """Resolve an artifact ID, version ID, {{artifact:...}} marker, or filename to an on-disk Path."""
    ws = Path(workspace).resolve()
    raw = str(ref_id or "").strip()
    if raw.startswith("{{artifact:") and raw.endswith("}}"):
        raw = raw[len("{{artifact:") : -2].strip()
    elif raw.startswith("{{!artifact:") and raw.endswith("}}"):
        raw = raw[len("{{!artifact:") : -2].strip()
    if not raw or any(ch in raw for ch in ("'", '"', "[", "]", "{", "}", "\n", "\r", "\t", "\\", "..")):
        return None
    if raw.startswith("/"):
        return None

    own_db = None
    if artifact_store is None:
        jobs_db = ws / ".omicsclaw" / "jobs.db"
        if jobs_db.is_file():
            try:
                from omicsclaw.memory.artifacts import ArtifactStore
                from omicsclaw.memory.database import Database

                own_db = Database(jobs_db)
                artifact_store = ArtifactStore(own_db)
            except Exception:  # noqa: BLE001
                own_db = None
                artifact_store = None

    try:
        candidates_ids = [raw]
        if raw.startswith("art_"):
            candidates_ids.append(raw[4:])
        else:
            candidates_ids.append(f"art_{raw}")

        # 0. Check explicit registry or provenance/artifacts.json
        reg_sources: list[Any] = []
        if registry is not None:
            reg_sources.append(registry)
        for reg_cand in (
            ws / "provenance" / "artifacts.json",
            ws / ".omicsclaw" / "provenance" / "artifacts.json",
        ):
            if reg_cand.is_file():
                try:
                    reg_sources.append(json.loads(reg_cand.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    pass
        for reg in reg_sources:
            entries: list[dict[str, Any]] = []
            if isinstance(reg, dict):
                if raw in reg and isinstance(reg[raw], (str, dict)):
                    val = reg[raw]
                    p_str = val if isinstance(val, str) else str(val.get("path") or "")
                    if p_str:
                        fp = (ws / p_str).resolve() if not Path(p_str).is_absolute() else Path(p_str).resolve()
                        if fp.is_file():
                            return fp
                if isinstance(reg.get("artifacts"), list):
                    entries.extend(x for x in reg["artifacts"] if isinstance(x, dict))
                else:
                    entries.extend(v for v in reg.values() if isinstance(v, dict))
            elif isinstance(reg, list):
                entries.extend(x for x in reg if isinstance(x, dict))
            for ent in entries:
                ent_ids = {
                    str(ent.get(k) or "")
                    for k in ("id", "artifact_id", "version_id", "latest_version_id", "vid")
                } - {""}
                ent_path = str(ent.get("path") or ent.get("filename") or "")
                if (ent_ids & set(candidates_ids)) or ent_path == raw or (ent_path and Path(ent_path).name == raw):
                    if ent_path:
                        fp = (ws / ent_path).resolve() if not Path(ent_path).is_absolute() else Path(ent_path).resolve()
                        if fp.is_file():
                            return fp

        # 1. Check ArtifactStore or direct sqlite3 on .omicsclaw/jobs.db
        if artifact_store is not None:
            try:
                for cid in candidates_ids:
                    get_fn = getattr(artifact_store, "get_artifact", None)
                    if callable(get_fn):
                        rec = get_fn(cid)
                        if rec is not None and getattr(rec, "path", ""):
                            p = Path(rec.path)
                            if not p.is_absolute():
                                p = ws / p
                            if p.is_file():
                                return p.resolve()
                for rec in artifact_store.list_artifacts(limit=500):
                    rec_id = str(getattr(rec, "id", ""))
                    rec_sha = str(getattr(rec, "sha256", ""))
                    rec_path = Path(str(getattr(rec, "path", "")))
                    if not rec_path.is_absolute():
                        rec_path = ws / rec_path
                    meta = getattr(rec, "meta", {}) or {}
                    ver_id = str(meta.get("version_id") or meta.get("latest_version_id") or "")
                    rel_rec = ""
                    try:
                        rel_rec = rec_path.resolve().relative_to(ws).as_posix()
                    except ValueError:
                        rel_rec = ""
                    if (
                        rec_id in candidates_ids
                        or (ver_id and ver_id in candidates_ids)
                        or (rec_sha and len(raw) >= 8 and rec_sha.startswith(raw))
                        or rec_path.name == raw
                        or rel_rec == raw
                        or (rel_rec and rel_rec.endswith(f"/{raw}"))
                    ):
                        if rec_path.is_file():
                            return rec_path.resolve()
            except Exception:  # noqa: BLE001
                pass
        elif (ws / ".omicsclaw" / "jobs.db").is_file():
            try:
                with _sqlite3.connect(str(ws / ".omicsclaw" / "jobs.db")) as conn:
                    conn.row_factory = _sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, path, sha256, meta_json FROM artifacts ORDER BY created_at DESC LIMIT 500"
                    ).fetchall()
                for r in rows:
                    r_id = str(r["id"] or "")
                    r_path = Path(str(r["path"] or ""))
                    if not r_path.is_absolute():
                        r_path = ws / r_path
                    r_sha = str(r["sha256"] or "")
                    meta = json.loads(r["meta_json"] or "{}") if r["meta_json"] else {}
                    ver_id = str(meta.get("version_id") or meta.get("latest_version_id") or "")
                    if (
                        r_id in candidates_ids
                        or (ver_id and ver_id in candidates_ids)
                        or (r_sha and len(raw) >= 8 and r_sha.startswith(raw))
                        or r_path.name == raw
                    ) and r_path.is_file():
                        return r_path.resolve()
            except Exception:  # noqa: BLE001
                pass

        # 2. Check replay_receipt.json
        for receipt_cand in (
            ws / "provenance" / "replay_receipt.json",
            ws / ".omicsclaw" / "provenance" / "replay_receipt.json",
        ):
            if receipt_cand.is_file():
                try:
                    rdata = json.loads(receipt_cand.read_text(encoding="utf-8"))
                    for out in rdata.get("verified_outputs") or []:
                        if not isinstance(out, dict):
                            continue
                        out_p = str(out.get("path") or "")
                        out_id = str(out.get("artifact_id") or out.get("version_id") or "")
                        out_sha = str(out.get("sha256") or "")
                        if (
                            (out_id and out_id in candidates_ids)
                            or (out_sha and len(raw) >= 8 and out_sha.startswith(raw))
                            or out_p == raw
                            or out_p.endswith(f"/{raw}")
                            or Path(out_p).name == raw
                        ):
                            fp = ws / out_p
                            if fp.is_file():
                                return fp.resolve()
                except Exception:  # noqa: BLE001
                    pass

        # 3. Direct relative path under workspace or workspace/output
        for direct_cand in (ws / raw, ws / "output" / raw):
            try:
                resolved_cand = direct_cand.resolve()
                if resolved_cand.is_relative_to(ws) and resolved_cand.is_file():
                    return resolved_cand
            except Exception:  # noqa: BLE001
                pass

        # 4. Scan standard workspace output directories
        for rel_out in _discover_workspace_outputs(ws):
            fp = ws / rel_out
            if not fp.is_file():
                continue
            stem_slug = f"art_{fp.stem}"
            if (
                rel_out == raw
                or rel_out.endswith(f"/{raw}")
                or fp.name == raw
                or stem_slug in candidates_ids
            ):
                return fp.resolve()
            if len(raw) >= 8 and all(c in "0123456789abcdefABCDEF" for c in raw):
                if sha256_file(fp).startswith(raw.lower()):
                    return fp.resolve()
        return None
    finally:
        if own_db is not None:
            own_db.close()


def resolve_code_artifact_markers(
    code: str,
    workspace: str | Path,
    *,
    artifact_store: Any = None,
    registry: Any = None,
) -> str:
    """Resolve {{artifact:<id>}} to physical paths in code cells and strip ! from {{!artifact:<id>}}."""
    if "artifact:" not in code:
        return code
    ws = Path(workspace).resolve()

    def _replace(match: _re.Match[str]) -> str:
        bang = match.group(1)
        raw_id = match.group(2)
        if bang == "!":
            return f"{{{{artifact:{raw_id}}}}}"
        if not is_valid_artifact_id(raw_id):
            return match.group(0)
        resolved = resolve_artifact_reference(
            raw_id, ws, artifact_store=artifact_store, registry=registry
        )
        if resolved is None:
            return match.group(0)
        try:
            return resolved.relative_to(ws).as_posix()
        except ValueError:
            return resolved.as_posix()

    return _ARTIFACT_MARKER_IN_DOC_RE.sub(_replace, code)


def unescape_output_path(text: str) -> str:
    """Convert escaped {{!artifact:<id>}} markers into literal {{artifact:<id>}} text."""
    if "{{!artifact:" not in text:
        return text
    return _re.sub(r"\{\{!artifact:([^}]*)\}\}", r"{{artifact:\1}}", text)


def verify_artifact_markers(
    doc_path_or_text: str | Path,
    workspace: str | Path | None = None,
    *,
    artifact_store: Any = None,
    registry: Any = None,
) -> Verification:
    """Verify Claude Science {{artifact:...}} and image reference rules inside a saved document."""
    if workspace is None:
        if isinstance(doc_path_or_text, Path):
            ws = doc_path_or_text.resolve().parent
        else:
            ws = Path(".").resolve()
    else:
        ws = Path(workspace).resolve()

    ref_label = str(doc_path_or_text)
    if isinstance(doc_path_or_text, Path) or (
        isinstance(doc_path_or_text, str)
        and "\n" not in doc_path_or_text
        and "\r" not in doc_path_or_text
        and len(doc_path_or_text) < 260
        and (Path(doc_path_or_text).is_file() or (ws / doc_path_or_text).is_file())
    ):
        p = Path(doc_path_or_text)
        if not p.is_absolute() and (ws / p).is_file():
            p = ws / p
        try:
            text = p.read_text(encoding="utf-8")
            ref_label = str(p)
        except OSError as exc:
            v_err = Verification(
                check="artifact_markers",
                verdict=Verdict.FAIL,
                evidence_ref=str(p),
                details=str(exc),
            )
            v_err._meta = {  # type: ignore[attr-defined]
                "resolved": [],
                "unresolved_markers": [],
                "malformed_markers": [],
                "escaped_markers": [],
                "issues": [str(exc)],
            }
            return v_err
    else:
        text = str(doc_path_or_text)

    issues: list[str] = []
    resolved_markers: list[dict[str, str]] = []
    unresolved_markers: list[str] = []
    malformed_markers: list[str] = []
    escaped_markers: list[str] = []
    for match in _ARTIFACT_MARKER_IN_DOC_RE.finditer(text):
        bang, raw_id = match.group(1), match.group(2)
        if bang == "!":
            escaped_markers.append(raw_id)
            continue
        if not is_valid_artifact_id(raw_id):
            malformed_markers.append(raw_id)
            issues.append(f"malformed artifact marker id: {raw_id!r}")
            continue
        target = resolve_artifact_reference(
            raw_id, ws, artifact_store=artifact_store, registry=registry
        )
        if target is None:
            unresolved_markers.append(raw_id)
            issues.append(f"unresolved artifact marker: {{{{artifact:{raw_id}}}}}")
        else:
            resolved_markers.append({"id": raw_id, "path": target.name})

    for img_match in _MD_IMAGE_RE.finditer(text):
        img_ref = img_match.group(1).strip()
        if not img_ref or img_ref.startswith(("{{artifact:", "http://", "https://", "data:")):
            continue
        if img_ref.startswith("{{!artifact:"):
            escaped_markers.append(img_ref)
            issues.append(f"unescaped image marker: {img_ref}")
            continue
        if img_ref.startswith("/tmp/") or "'" in img_ref or '"' in img_ref:
            malformed_markers.append(img_ref)
            issues.append(f"ephemeral or invalid image path in document: {img_ref}")
            continue
        resolved_img = resolve_artifact_reference(
            img_ref, ws, artifact_store=artifact_store, registry=registry
        )
        if resolved_img is None and not (ws / img_ref).is_file():
            unresolved_markers.append(img_ref)
            issues.append(f"missing referenced image artifact: {img_ref}")

    res = Verification(
        check="artifact_markers",
        verdict=Verdict.FAIL if issues else Verdict.PASS,
        evidence_ref=ref_label,
        details="; ".join(issues) if issues else json.dumps({"resolved": resolved_markers}),
    )
    res._meta = {  # type: ignore[attr-defined]
        "resolved": resolved_markers,
        "unresolved_markers": unresolved_markers,
        "malformed_markers": malformed_markers,
        "escaped_markers": escaped_markers,
        "issues": issues,
    }
    return res


def trace_tabular_claim(
    table_path_or_workspace: str | Path,
    expected_or_artifact: Any = None,
    expected_value_pos: Any = None,
    *,
    expected_value: Any = None,
    key_column: str | None = None,
    key_value: Any = None,
    row_filter: dict[str, Any] | None = None,
    target_column: str | None = None,
    value_column: str | None = None,
    rtol: float = 1e-4,
    atol: float = 1e-5,
    rel_tol: float | None = None,
    abs_tol: float | None = None,
    workspace: str | Path | None = None,
) -> Verification:
    """Trace a cell value in a CSV/TSV artifact by header name (Claude Science 'Trace, don't recompute' rule)."""
    col = target_column or value_column
    if not col:
        raise ValueError("trace_tabular_claim requires target_column or value_column")
    if row_filter and not key_column:
        key_column, key_value = next(iter(row_filter.items()))
    if not key_column:
        raise ValueError("trace_tabular_claim requires key_column/key_value or row_filter")
    eff_rtol = rtol if rel_tol is None else rel_tol
    eff_atol = atol if abs_tol is None else abs_tol

    p_first = Path(table_path_or_workspace)
    ws_dir: Path | None = Path(workspace).resolve() if workspace is not None else None
    if p_first.is_dir():
        ws_dir = p_first.resolve()
        raw_table_ref = str(expected_or_artifact or "")
        if expected_value is None:
            expected_value = expected_value_pos
    else:
        raw_table_ref = str(table_path_or_workspace)
        if expected_value is None:
            expected_value = expected_or_artifact

    tpath = Path(raw_table_ref)
    if not tpath.is_file() and ws_dir is not None:
        resolved_t = resolve_artifact_reference(raw_table_ref, ws_dir)
        if resolved_t is not None:
            tpath = resolved_t
        elif (ws_dir / raw_table_ref).is_file():
            tpath = (ws_dir / raw_table_ref).resolve()
    if not tpath.is_file():
        res = Verification(
            check="tabular_trace",
            verdict=Verdict.FAIL,
            evidence_ref=str(tpath),
            details="table file not found",
        )
        res._meta = {"error": "table file not found"}  # type: ignore[attr-defined]
        return res
    delimiter = "\t" if tpath.suffix.lower() in {".tsv", ".txt"} else ","
    try:
        with tpath.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            fieldnames = list(reader.fieldnames or [])
            if key_column not in fieldnames or col not in fieldnames:
                res = Verification(
                    check="tabular_trace",
                    verdict=Verdict.FAIL,
                    evidence_ref=str(tpath),
                    details=f"missing column ({key_column!r} or {col!r}) in {fieldnames}",
                )
                res._meta = {"error": res.details}  # type: ignore[attr-defined]
                return res
            matched_vals = [
                row[col]
                for row in reader
                if (
                    all(str(row.get(k, "")).strip() == str(v).strip() for k, v in row_filter.items())
                    if row_filter
                    else str(row.get(key_column, "")).strip() == str(key_value).strip()
                )
            ]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        res = Verification(
            check="tabular_trace",
            verdict=Verdict.FAIL,
            evidence_ref=str(tpath),
            details=str(exc),
        )
        res._meta = {"error": str(exc)}  # type: ignore[attr-defined]
        return res

    if not matched_vals:
        res = Verification(
            check="tabular_trace",
            verdict=Verdict.FAIL,
            evidence_ref=str(tpath),
            details=f"no row where {key_column}=={key_value!r}",
        )
        res._meta = {"error": res.details}  # type: ignore[attr-defined]
        return res
    actual_raw = matched_vals[0]
    if expected_value is None:
        res = Verification(
            check="tabular_trace",
            verdict=Verdict.PASS,
            evidence_ref=str(tpath),
            details=json.dumps({"key": key_value, "column": col, "value": actual_raw}),
        )
        res._meta = {  # type: ignore[attr-defined]
            "key_column": key_column,
            "key_value": key_value,
            "target_column": col,
            "actual_value": actual_raw,
            "expected_value": None,
        }
        return res

    exp_f = _parse_float(str(expected_value))
    act_f = _parse_float(str(actual_raw))
    if exp_f is not None and act_f is not None:
        ok = _floats_close(exp_f, act_f, rtol=eff_rtol, atol=eff_atol)
    else:
        ok = str(actual_raw).strip() == str(expected_value).strip()

    res = Verification(
        check="tabular_trace",
        verdict=Verdict.PASS if ok else Verdict.FAIL,
        evidence_ref=str(tpath),
        details=(
            json.dumps({"key": key_value, "column": col, "actual": actual_raw, "expected": expected_value})
            if ok
            else f"tabular contradiction at {key_column}={key_value!r} col {col!r}: actual={actual_raw!r} vs claimed={expected_value!r}"
        ),
    )
    res._meta = {  # type: ignore[attr-defined]
        "key_column": key_column,
        "key_value": key_value,
        "target_column": col,
        "actual_value": act_f if act_f is not None else actual_raw,
        "expected_value": exp_f if exp_f is not None else expected_value,
    }
    return res

