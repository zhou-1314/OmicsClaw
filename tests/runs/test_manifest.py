from omicsclaw.runs.manifest import build_manifest
def test_manifest_file_metadata(tmp_path):
    p=tmp_path/"x.json"; p.write_text("{}")
    a=build_manifest(p, code_ref="c")
    assert a.size==2 and a.checksum and a.content_type=="application/json" and a.code_ref=="c"
