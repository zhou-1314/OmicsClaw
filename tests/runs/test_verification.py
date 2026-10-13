from omicsclaw.runs.verification import verify_file, verify_json
def test_missing_file_fails(tmp_path):
    assert verify_file(tmp_path/"missing").verdict == "fail"
def test_json_schema_warn(tmp_path):
    p=tmp_path/"x.json"; p.write_text("{}")
    assert verify_json(p, ["required"]).verdict == "warn"


def test_h5ad_missing_optional_dependency_is_inconclusive(monkeypatch, tmp_path):
    from omicsclaw.runs import verification
    monkeypatch.setitem(__import__("sys").modules, "anndata", None)
    result = verification.verify_h5ad(tmp_path / "missing.h5ad")
    assert result.verdict == "inconclusive"


def test_h5ad_runner_is_selected_by_artifact_suffix(tmp_path):
    from omicsclaw.runs import verification
    result = verification.verify_artifact(tmp_path / "x.h5ad")
    assert result.check == "h5ad_schema"
