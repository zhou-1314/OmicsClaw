"""Inspect registered in-memory objects without serializing their matrices."""

import json
import math
import os
import secrets
from pathlib import Path


class _InspectionError(ValueError):
    pass


def _request(request, codec):
    fields = {
        "schemaVersion",
        "symbol",
        "adapter",
        "rowIdentity",
        "columnIdentity",
        "tableAxis",
        "declaredMatrix",
    }
    if (
        type(request) is not dict
        or set(request) != fields
        or type(request["schemaVersion"]) is not int
        or request["schemaVersion"] != 1
    ):
        raise _InspectionError("invalid_request")
    symbol = request["symbol"]
    if (
        type(symbol) is not str
        or not symbol.isidentifier()
        or len(symbol.encode("utf-8")) > 128
    ):
        raise _InspectionError("invalid_symbol")
    if request["adapter"] not in ("anndata", "python_table"):
        raise _InspectionError("unsupported_adapter")
    if request["tableAxis"] not in ("row", "column"):
        raise _InspectionError("invalid_request")
    for name in ("rowIdentity", "columnIdentity"):
        spec = request[name]
        if type(spec) is not dict or spec.get("kind") not in ("index", "columns"):
            raise _InspectionError("invalid_request")
        if spec["kind"] == "index":
            if set(spec) != {"kind"}:
                raise _InspectionError("invalid_request")
        elif set(spec) != {"kind", "names"}:
            raise _InspectionError("invalid_request")
        else:
            codec["keys"](spec["names"])
    matrix = request["declaredMatrix"]
    if (
        type(matrix) is not dict
        or set(matrix) != {"assay", "layer", "semantics"}
        or matrix["semantics"] not in ("raw_counts", "lognorm", "other", "unknown")
    ):
        raise _InspectionError("invalid_request")
    for name in ("assay", "layer"):
        if matrix[name] is not None:
            codec["text"](matrix[name], 128)


def _writing(path):
    """The fixed checkpoint inspector supplies a shared write budget here."""
    return path.open("xb")


def _atomic_json(path, value):
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with _writing(temporary) as stream:
            stream.write(
                json.dumps(
                    value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _warning(warnings, reason):
    if reason not in warnings and len(warnings) < 20:
        warnings.append(reason)


def _scalar(value, warnings):
    import numpy as np

    if value is None:
        return None
    if type(value) is str or type(value) is np.str_:
        value = str(value)
        try:
            if (
                "\0" not in value
                and len(value.encode("utf-8", errors="strict")) <= 1024
            ):
                return value
        except UnicodeError:
            pass
        _warning(warnings, "metadata_string_omitted")
        return None
    if type(value) is bool or type(value) is np.bool_:
        return bool(value)
    if type(value) in (int, float) or type(value) in (
        np.int8,
        np.int16,
        np.int32,
        np.int64,
        np.uint8,
        np.uint16,
        np.uint32,
        np.uint64,
        np.float16,
        np.float32,
        np.float64,
    ):
        if isinstance(value, (int, np.integer)):
            if abs(value) > 9007199254740991:
                _warning(warnings, "unsafe_integer_metadata_omitted")
                return None
            return int(value)
        if not math.isfinite(value):
            _warning(warnings, "nonfinite_metadata_omitted")
            return None
        return float(value)
    _warning(warnings, "unsupported_metadata_omitted")
    return None


def _objects(value, request):
    import pandas as pd

    declared = request["declaredMatrix"]
    matrix = dict(
        declared,
        exists=False,
        axesAligned=False,
        semanticsEvidence="unknown"
        if declared["semantics"] == "unknown"
        else "declared",
        reason="",
    )
    if request["adapter"] == "anndata":
        import anndata
        from anndata._core.views import DataFrameView

        if type(value) is not anndata.AnnData:
            raise _InspectionError("unsupported_type")
        if value.isbacked:
            raise _InspectionError("backed_anndata_unsupported")
        shape = list(value.shape)
        metadata = [value.obs, value.var]
        metadata_types = (pd.DataFrame, DataFrameView)
        roles = ["obs", "var"]
        if declared["assay"] is not None:
            matrix["reason"] = "assay_not_supported"
        elif declared["layer"] is not None and declared["layer"] not in value.layers:
            matrix["reason"] = "layer_not_found"
        else:
            data = (
                value.X
                if declared["layer"] is None
                else value.layers[declared["layer"]]
            )
            matrix["exists"] = data is not None
            matrix["axesAligned"] = data is not None and tuple(data.shape) == tuple(
                shape
            )
            matrix["reason"] = (
                "" if matrix["axesAligned"] else "matrix_missing_or_misaligned"
            )
        kind = "anndata.AnnData"
    else:
        if type(value) is not pd.DataFrame:
            raise _InspectionError("unsupported_type")
        shape = list(value.shape)
        metadata = [value, pd.DataFrame(index=value.columns)]
        metadata_types = (pd.DataFrame,)
        roles = ["row", "column"]
        matrix["exists"] = declared["assay"] is None and declared["layer"] is None
        matrix["axesAligned"] = matrix["exists"]
        matrix["reason"] = "" if matrix["exists"] else "assay_or_layer_not_supported"
        kind = "pandas.DataFrame"
    if any(type(item) is not int or item < 0 for item in shape) or len(shape) != 2:
        raise _InspectionError("invalid_dimensions")
    if any(type(item) not in metadata_types for item in metadata):
        raise _InspectionError("unsupported_metadata")
    return kind, shape, metadata, roles, matrix


def _identity(metadata, spec, codec):
    if spec["kind"] == "index":
        identity_keys = ["index"]
        columns = [metadata.index]
    else:
        identity_keys = codec["keys"](spec["names"])
        if not metadata.columns.is_unique or any(
            name not in metadata.columns for name in identity_keys
        ):
            raise _InspectionError("identity_columns_missing_or_ambiguous")
        columns = [metadata[name].array for name in identity_keys]

    def row_at(index):
        values = [column[index] for column in columns]
        codec["tuple_bytes"](values)
        return values

    return identity_keys, row_at


def _axis_file(directory, axis, metadata, spec, codec):
    keys, row_at = _identity(metadata, spec, codec)
    path = directory / (axis + ".ids")
    temporary = directory / (axis + "." + secrets.token_hex(8) + ".tmp")
    try:
        with _writing(temporary) as stream:
            codec["write_rows"](
                stream, keys, (row_at(index) for index in range(len(metadata)))
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return keys, row_at


def _table(directory, axis, metadata, keys, row_at, codec, warnings):
    columns = list(keys)
    sources = []
    if metadata.columns.is_unique:
        for name in metadata.columns:
            if len(columns) == 20:
                _warning(warnings, "metadata_columns_limited")
                break
            try:
                codec["text"](name, 128)
            except ValueError:
                _warning(warnings, "metadata_column_name_omitted")
                continue
            if name not in columns:
                columns.append(name)
                sources.append(metadata[name].array)
    else:
        _warning(warnings, "ambiguous_metadata_columns_omitted")
    path = directory / "table.rows.jsonl"
    temporary = directory / ("table." + secrets.token_hex(8) + ".tmp")
    size = 0
    try:
        with _writing(temporary) as stream:
            for index in range(len(metadata)):
                identity = row_at(index)
                values = identity + [
                    _scalar(column[index], warnings) for column in sources
                ]
                row = {"id": identity, "values": values}
                encoded = (
                    json.dumps(
                        row, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    )
                    + "\n"
                ).encode("utf-8")
                if len(encoded) > 16384:
                    _warning(warnings, "metadata_row_limited")
                    row["values"] = identity + [None] * len(sources)
                    encoded = (
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                size += len(encoded)
                if (
                    len(encoded) > 16384
                    or size > 64 * 1024 * 1024
                    or index >= 1_000_000
                ):
                    raise _InspectionError("table_limit")
                stream.write(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "axis": axis,
        "columns": columns,
        "rowsFile": path.name,
        "rowCount": len(metadata),
    }


def inspect_object(namespace, request, output_directory, codec):
    """Publish bounded observations of a supported object in the current kernel."""
    directory = Path(output_directory)
    try:
        _request(request, codec)
        if type(namespace) is not dict or request["symbol"] not in namespace:
            raise _InspectionError("object_not_found")
        kind, shape, metadata, roles, matrix = _objects(
            namespace[request["symbol"]], request
        )
        axes, readers, warnings = [], {}, []
        for index, axis in enumerate(("row", "column")):
            spec = request[axis + "Identity"]
            identity_keys = (
                ["index"] if spec["kind"] == "index" else codec["keys"](spec["names"])
            )
            entry = {
                "axis": axis,
                "role": roles[index],
                "count": shape[index],
                "keys": identity_keys,
                "idsFile": None,
                "reason": "",
            }
            try:
                identity_keys, row_at = _axis_file(
                    directory, axis, metadata[index], spec, codec
                )
                entry["idsFile"] = axis + ".ids"
                readers[axis] = (metadata[index], identity_keys, row_at)
            except (_InspectionError, ValueError) as error:
                reason = (
                    str(error)
                    if type(error) in (_InspectionError, ValueError)
                    else "invalid_identity"
                )
                entry["reason"] = (
                    reason
                    if reason
                    in {
                        "invalid_identity",
                        "identity_limit",
                        "identity_columns_missing_or_ambiguous",
                    }
                    else "invalid_identity"
                )
            axes.append(entry)
        table = None
        if len(readers) == 2:
            table = _table(
                directory,
                request["tableAxis"],
                *readers[request["tableAxis"]],
                codec,
                warnings,
            )
        result = {
            "schemaVersion": 1,
            "status": "ok",
            "type": kind,
            "shape": shape,
            "axes": axes,
            "matrix": matrix,
            "table": table,
        }
        if warnings:
            result["warnings"] = warnings
    except _InspectionError as error:
        result = {"schemaVersion": 1, "status": "error", "reason": str(error)}
    except Exception:  # noqa: BLE001 — keep third-party exceptions out of the manifest
        result = {"schemaVersion": 1, "status": "error", "reason": "inspection_failed"}
    _atomic_json(directory / "inspection.json", result)
    return result


def write_selection(keys, rows, output_directory, codec):
    """Write canonical, unique selection members for owner-side registration."""
    keys = codec["keys"](keys)
    directory = Path(output_directory)
    name = "selection-" + secrets.token_hex(16)
    path = directory / (name + ".ids")
    temporary = directory / (name + ".tmp")
    seen = set()

    def unique_rows():
        for row in rows:
            encoded = codec["tuple_bytes"](row)
            if encoded in seen:
                raise ValueError("duplicate_identity")
            seen.add(encoded)
            yield row

    try:
        with _writing(temporary) as stream:
            count = codec["write_rows"](stream, keys, unique_rows())
        if not count:
            raise ValueError("empty_selection")
        os.replace(temporary, path)
        descriptor = {
            "schemaVersion": 1,
            "file": path.name,
            "keys": keys,
            "count": count,
        }
        _atomic_json(directory / (name + ".json"), descriptor)
        return descriptor
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
