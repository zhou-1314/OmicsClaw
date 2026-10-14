"""Hello identifies the exact immutable source payload; mismatches refuse production attach."""

import hashlib
import json

import pytest


def test_component_manifest_verifies_files_and_refuses_changed_payload(tmp_path):
    from omicsclaw.entry.ssh.manifest import verified_source_metadata

    source = tmp_path / "omicsclaw/__init__.py"
    source.parent.mkdir()
    source.write_text("")
    rows = [
        {
            "path": "omicsclaw/__init__.py",
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    ]
    manifest = {
        "componentVersion": "0.2.0",
        "protocolVersion": 1,
        "sourceCommit": "a" * 40,
        "sourceCommitVerified": True,
        "files": rows,
        "sourceBytes": 0,
        "archiveSha256": "b" * 64,
        "sourceFilesSha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (tmp_path / "controller-manifest.json").write_text(json.dumps(manifest))
    metadata = verified_source_metadata(tmp_path)
    assert (
        metadata.get("sourceManifestHash")
        == hashlib.sha256(
            (tmp_path / "controller-manifest.json").read_bytes()
        ).hexdigest()
    )
    assert (
        metadata["sourceArchiveHash"] == "b" * 64
        and metadata["sourceCommitVerified"] is True
    )
    source.write_text("changed")
    with pytest.raises(ValueError, match="source file mismatch"):
        verified_source_metadata(tmp_path)


def test_component_manifest_allows_declared_catalogue_data_but_not_algorithm_payload(
    tmp_path,
):
    from omicsclaw.entry.ssh.manifest import verified_source_metadata

    path = tmp_path / "skills/sc-filter/algorithm.py"
    path.parent.mkdir(parents=True)
    path.write_text("")
    rows = [
        {
            "path": "skills/sc-filter/algorithm.py",
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    ]
    manifest = {
        "componentVersion": "0.2.0",
        "protocolVersion": 1,
        "sourceCommit": "a" * 40,
        "sourceCommitVerified": True,
        "files": rows,
        "sourceBytes": 0,
        "archiveSha256": "b" * 64,
        "sourceFilesSha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (tmp_path / "controller-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="source path"):
        verified_source_metadata(tmp_path)
