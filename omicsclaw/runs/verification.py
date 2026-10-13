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
