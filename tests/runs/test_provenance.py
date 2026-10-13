from omicsclaw.runs.manifest import build_manifest
def test_version_parent_chain(tmp_path):
    p=tmp_path/"x"; p.write_text("x")
    a=build_manifest(p, artifact_id="a", version=1)
    b=build_manifest(p, artifact_id="a:v2", version=2, parent_artifact_id=a.artifact_id)
    assert b.parent_artifact_id == a.artifact_id
