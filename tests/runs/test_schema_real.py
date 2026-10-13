"""Small synthetic files cover optional schema validators when installed."""

import json
from types import SimpleNamespace

import pytest

from omicsclaw.runs.verification import verify_artifact, verify_h5ad, verify_json


def test_h5ad_real_shape_and_columns(tmp_path):
    anndata = pytest.importorskip("anndata")
    import numpy as np

    target = tmp_path / "synthetic.h5ad"
    data = anndata.AnnData(np.zeros((2, 3)))
    data.obs["sample"] = ["a", "b"]
    data.var["feature"] = ["x", "y", "z"]
    data.write_h5ad(target)
    assert (
        verify_artifact(
            target,
            schema={
                "required_obs": ["sample"],
                "required_vars": ["feature"],
                "min_shape": [2, 3],
            },
        ).verdict
        == "pass"
    )
    assert verify_h5ad(target, min_shape=(3, 3)).verdict == "fail"
    assert verify_h5ad(target, required_obs=("missing",)).verdict == "warn"
    target.write_bytes(b"corrupt")
    assert verify_h5ad(target).verdict == "fail"


def test_h5ad_closes_backed_file_on_validation_error(monkeypatch, tmp_path):
    import sys

    closed = []
    data = SimpleNamespace(
        obs={},
        var={},
        shape=(2, 3),
        file=SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setitem(
        sys.modules, "anndata", SimpleNamespace(read_h5ad=lambda *a, **k: data)
    )
    assert verify_h5ad(tmp_path / "fake.h5ad", min_shape=("bad", 3)).verdict == "fail"
    assert closed == [True]


def test_json_schema_types_and_unavailable_validator(tmp_path, monkeypatch):
    pytest.importorskip("jsonschema")
    target = tmp_path / "result.json"
    target.write_text(json.dumps({"count": "wrong"}))
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    assert verify_json(target, schema=schema).verdict == "fail"
    target.write_text('{"count":2}')
    assert verify_json(target, schema=schema).verdict == "pass"
    import sys

    monkeypatch.setitem(sys.modules, "jsonschema", None)
    assert verify_json(target, schema=schema).verdict == "inconclusive"


def test_remote_artifact_is_not_a_local_missing_file():
    result = verify_artifact("remote://localhost/tmp/output.h5ad")
    assert result.verdict == "inconclusive"
    assert "not been fetched" in result.details
