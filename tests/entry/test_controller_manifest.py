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


@pytest.mark.parametrize(
    "relative", ["skills/sc-filter/algorithm.py", "omicsclaw_science_worker.py"]
)
def test_component_manifest_refuses_algorithm_payload_and_retired_entry(
    tmp_path,
    relative,
):
    from omicsclaw.entry.ssh.manifest import verified_source_metadata

    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    rows = [
        {
            "path": relative,
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


def test_scientific_worker_entry_is_packaged_without_skill_algorithm_source():
    from pathlib import Path

    from scripts.build_controller import source_closure

    closure = source_closure(Path(__file__).parents[2])
    assert "omicsclaw.entry.ssh.science_worker" in closure
    assert "omicsclaw_science_worker" not in closure
    assert not any(name == "skills" or name.startswith("skills.") for name in closure)


def test_finite_execution_wrapper_is_in_the_fixed_controller_archive():
    from pathlib import Path

    from scripts.build_controller import source_closure

    closure = source_closure(Path(__file__).parents[2])
    assert "omicsclaw.remote.deadline" in closure


def test_registered_probe_runs_from_the_archive_without_the_checkout(tmp_path):
    import subprocess
    import sys
    import zipfile
    from pathlib import Path

    from scripts.build_controller import build

    artifact = tmp_path / "artifact"
    manifest = build(Path(__file__).parents[2], artifact, allow_dirty=True)
    source = "omicsclaw/skillenv/registered_probe.py"
    assert any(row["path"] == source for row in manifest["files"])
    assert {"omicsclaw/skillenv/notebook_inspect.py", "omicsclaw/skillenv/notebook_identity.py",
            "omicsclaw/skillenv/notebook_inspect.R", "omicsclaw/skillenv/notebook_identity.R"} <= {row["path"] for row in manifest["files"]}
    packaged = {row["path"] for row in manifest["files"]}
    assert {"omicsclaw/entry/ssh/notebook_owner.py", "omicsclaw/entry/ssh/notebook_ipc.py",
            "omicsclaw/entry/notebook/runtime.py", "omicsclaw/entry/notebook/policy.py"} <= packaged
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(artifact / "controller.zip") as archive:
        archive.extract(source, extracted)
        sidecar = "skills/singlecell/scrna/sc-filter/kernel.py"
        assert sidecar in packaged
        archive.extract(sidecar, extracted)
    # Helper definitions load from the payload without importing any scientific package.
    helper_source = (extracted / sidecar).read_text()
    from omicsclaw.skills.sidecar import extract_kernel_exports, validate_kernel_ast

    assert not validate_kernel_ast(helper_source)
    namespace = {}
    exec(compile(helper_source, str(extracted / sidecar), "exec"), namespace)
    assert all(callable(namespace[name]) for name, _, _ in extract_kernel_exports(helper_source))
    # Only this file is extracted; isolated Python cannot import the checkout.
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(extracted / source), '["json"]'],
        cwd=extracted,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["prefix"] == sys.prefix
    assert observed["missing"] == []
