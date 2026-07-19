from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import subprocess
from threading import Barrier
from types import SimpleNamespace

import pytest

import omicsclaw.common.output_claim as output_claim
import omicsclaw.skill.execution.output_ownership as execution_ownership
from omicsclaw.common.output_claim import atomic_write_owned_output_text
from omicsclaw.skill.execution.output_ownership import (
    OUTPUT_CLAIM_FILENAME,
    OutputDirectoryClaimError,
    bind_output_claim_audit_identity,
    claim_fresh_output_directory,
    is_contained_output_path,
    is_output_claim_artifact,
    is_output_claim_path,
    is_scientific_output_file,
)
from omicsclaw.skill.result import SkillRunAuditIdentity


@pytest.mark.parametrize(
    "value",
    [
        ".omicsclaw-run-claim.json",
        "nested/.omicsclaw-run-claim.json",
        r"nested\.omicsclaw-run-claim.json",
    ],
)
def test_internal_claim_name_is_separator_independent(value: str) -> None:
    assert is_output_claim_path(Path(value)) is True


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_runtime_claim_identity_rejects_filesystem_aliases(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    alias = output_dir / "artifact.json"
    if alias_kind == "symlink":
        alias.symlink_to(claim)
    else:
        alias.hardlink_to(claim)

    assert is_output_claim_artifact(alias, output_root=output_dir) is True
    ordinary = output_dir / "ordinary.json"
    ordinary.write_text("{}\n", encoding="utf-8")
    assert is_output_claim_artifact(ordinary, output_root=output_dir) is False


def test_runtime_claim_identity_indexes_nested_run_leaves(tmp_path: Path) -> None:
    composite_root = tmp_path / "pipeline"
    leaf = composite_root / "step-a"
    leaf.mkdir(parents=True)
    claim = leaf / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    alias = leaf / "artifact.json"
    alias.hardlink_to(claim)

    assert is_output_claim_artifact(alias, output_root=composite_root) is True


def test_claim_identity_scan_does_not_descend_windows_reparse_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "out"
    junction = output_dir / "junction"
    junction.mkdir(parents=True)
    (junction / OUTPUT_CLAIM_FILENAME).write_text("{}\n", encoding="utf-8")
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)
    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    assert output_claim.collect_output_claim_identities(output_dir) == frozenset()


def test_scientific_output_file_rejects_escaping_symlink(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    external = tmp_path / "external.h5ad"
    external.write_text("external", encoding="utf-8")
    linked = output_dir / "processed.h5ad"
    linked.symlink_to(external)

    assert is_scientific_output_file(linked, output_root=output_dir) is False

    internal = output_dir / "internal.h5ad"
    internal.write_text("internal", encoding="utf-8")
    internal_link = output_dir / "internal-link.h5ad"
    internal_link.symlink_to(internal)
    assert is_scientific_output_file(internal_link, output_root=output_dir) is False

    external_hardlink = output_dir / "external-hardlink.h5ad"
    external_hardlink.hardlink_to(external)
    assert is_scientific_output_file(external_hardlink, output_root=output_dir) is False


def test_scientific_output_file_rejects_contained_file_symlink(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    payload = output_dir / "payload.json"
    payload.write_text('{"status": "completed"}\n', encoding="utf-8")
    marker = output_dir / "result.json"
    marker.symlink_to(payload.name)

    assert is_scientific_output_file(marker, output_root=output_dir) is False


def test_scientific_output_file_rejects_contained_symlink_parent(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    real_dir = output_dir / "real"
    real_dir.mkdir(parents=True)
    payload = real_dir / "result.json"
    payload.write_text('{"status": "completed"}\n', encoding="utf-8")
    alias_dir = output_dir / "alias"
    alias_dir.symlink_to(real_dir.name, target_is_directory=True)

    assert (
        is_scientific_output_file(
            alias_dir / payload.name,
            output_root=output_dir,
        )
        is False
    )


def test_scientific_output_file_rejects_symlink_erased_by_parent_reference(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    real_dir = output_dir / "real"
    (real_dir / "deep").mkdir(parents=True)
    payload = real_dir / "result.json"
    payload.write_text('{"status": "completed"}\n', encoding="utf-8")
    alias_dir = output_dir / "jump"
    alias_dir.symlink_to(real_dir / "deep", target_is_directory=True)
    tainted_candidate = alias_dir / ".." / payload.name

    # Containment answers only where the path resolves.  Scientific ownership
    # must additionally retain and reject the pre-normalisation alias evidence.
    assert is_contained_output_path(tainted_candidate, output_root=output_dir)
    assert not is_scientific_output_file(
        tainted_candidate,
        output_root=output_dir,
    )


def test_scientific_output_file_rejects_alias_only_in_output_root(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    payload = output_dir / "result.json"
    payload.write_text('{"status": "completed"}\n', encoding="utf-8")
    deep = tmp_path / "deep"
    deep.mkdir()
    jump = tmp_path / "jump"
    jump.symlink_to(deep, target_is_directory=True)
    tainted_root = jump / ".." / output_dir.name

    assert is_contained_output_path(payload, output_root=tainted_root)
    assert not is_scientific_output_file(payload, output_root=tainted_root)


def test_common_alias_predicate_rejects_windows_reparse_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junction = tmp_path / "junction"
    junction.mkdir()
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)

    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    assert output_claim.first_filesystem_alias_component(
        junction / "result.json"
    ) == junction


@pytest.mark.parametrize(
    ("file_attributes", "reparse_tag"),
    [
        (0x00000400, 0),
        (0, 0xA0000003),  # mount-point/junction tag carries NAME_SURROGATE
    ],
)
def test_windows_alias_detection_covers_reparse_and_name_surrogate_signals(
    file_attributes: int,
    reparse_tag: int,
) -> None:
    entry_stat = SimpleNamespace(
        st_file_attributes=file_attributes,
        st_reparse_tag=reparse_tag,
    )

    assert output_claim._is_windows_reparse_point(entry_stat)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_common_alias_predicate_detects_real_windows_junction(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    try:
        assert output_claim.first_filesystem_alias_component(
            junction / "result.json"
        ) == junction
    finally:
        os.rmdir(junction)


def test_scientific_output_file_rejects_windows_reparse_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "out"
    junction = output_dir / "junction"
    junction.mkdir(parents=True)
    result = junction / "result.json"
    result.write_text("{}\n", encoding="utf-8")
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)
    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    assert not is_scientific_output_file(result, output_root=output_dir)


def test_scientific_output_file_fails_closed_when_alias_inspection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = output_dir / "result.json"
    result.write_text("{}\n", encoding="utf-8")

    def _deny_inspection(_path: str | Path) -> Path | None:
        raise PermissionError("injected lstat denial")

    monkeypatch.setattr(
        output_claim,
        "first_filesystem_alias_component",
        _deny_inspection,
    )

    assert not is_scientific_output_file(result, output_root=output_dir)


@pytest.mark.parametrize("use_relative_paths", [False, True])
def test_scientific_output_file_preserves_normal_path_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_relative_paths: bool,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    payload = output_dir / "result.json"
    payload.write_text('{"status": "completed"}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    root = Path("out") if use_relative_paths else output_dir
    candidate = root / payload.name

    assert is_scientific_output_file(candidate, output_root=root)


def test_claim_accepts_an_existing_empty_directory_and_persists_marker(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    claimed = claim_fresh_output_directory(output_dir, owner="skill:test")

    assert claimed == output_dir.resolve()
    payload = json.loads(
        (claimed / OUTPUT_CLAIM_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == 1
    assert payload["owner"] == "skill:test"
    assert len(payload["claim_id"]) == 32


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_claim_fsyncs_marker_and_directory_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_kinds: list[str] = []
    real_fsync = execution_ownership.os.fsync

    def observed_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(execution_ownership.os, "fsync", observed_fsync)

    claim_fresh_output_directory(tmp_path / "out", owner="skill:test")

    assert fsync_kinds == ["file", "directory"]


def test_claim_audit_binding_atomically_records_actual_runtime_identity(
    tmp_path: Path,
) -> None:
    output_dir = claim_fresh_output_directory(
        tmp_path / "out",
        owner="skill:test-skill",
    )
    identity = SkillRunAuditIdentity(
        skill_id="test-skill",
        skill_version="1.2.3",
        skill_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        environment_id="env:" + "c" * 20,
    )

    bind_output_claim_audit_identity(
        output_dir,
        owner="skill:test-skill",
        audit_identity=identity,
        runtime_source="venv:abc123",
    )

    payload = json.loads(
        (output_dir / OUTPUT_CLAIM_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["owner"] == "skill:test-skill"
    assert payload["audit_identity"] == identity.to_dict()
    assert payload["runtime_source"] == "venv:abc123"
    assert not list(output_dir.glob(".*.tmp"))


def test_claim_audit_binding_rejects_claim_filesystem_alias(
    tmp_path: Path,
) -> None:
    output_dir = claim_fresh_output_directory(
        tmp_path / "out",
        owner="skill:test-skill",
    )
    claim_path = output_dir / OUTPUT_CLAIM_FILENAME
    real_claim = tmp_path / "relocated-claim.json"
    claim_path.replace(real_claim)
    claim_path.symlink_to(real_claim)
    identity = SkillRunAuditIdentity(
        skill_id="test-skill",
        skill_version="1.2.3",
        skill_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        environment_id="env:" + "c" * 20,
    )

    with pytest.raises(OutputDirectoryClaimError, match="symbolic link"):
        bind_output_claim_audit_identity(
            output_dir,
            owner="skill:test-skill",
            audit_identity=identity,
            runtime_source="base",
        )

    assert "audit_identity" not in json.loads(real_claim.read_text(encoding="utf-8"))


def test_claim_accepts_relative_existing_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    claimed = claim_fresh_output_directory(Path("out"), owner="skill:test")

    assert claimed == output_dir.resolve()
    assert (claimed / OUTPUT_CLAIM_FILENAME).is_file()


def test_claim_accepts_nested_directories_that_do_not_exist(tmp_path: Path) -> None:
    output_dir = tmp_path / "new-parent" / "nested" / "out"

    claimed = claim_fresh_output_directory(output_dir, owner="skill:test")

    assert claimed == output_dir.resolve()
    assert (claimed / OUTPUT_CLAIM_FILENAME).is_file()


def test_claim_rejects_prior_artifacts_without_mutating_them(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    stale = output_dir / "result.json"
    stale.write_text('{"status":"ok"}\n', encoding="utf-8")

    with pytest.raises(OutputDirectoryClaimError, match="fresh output directory"):
        claim_fresh_output_directory(output_dir, owner="skill:test")

    assert stale.read_text(encoding="utf-8") == '{"status":"ok"}\n'
    assert not (output_dir / OUTPUT_CLAIM_FILENAME).exists()


def test_claim_rejects_symlinked_output_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(OutputDirectoryClaimError, match="symbolic link"):
        claim_fresh_output_directory(linked, owner="skill:test")

    assert list(target.iterdir()) == []


def test_claim_rejects_symlinked_output_directory_ancestor(tmp_path: Path) -> None:
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target_parent, target_is_directory=True)
    output_dir = linked_parent / "out"

    with pytest.raises(OutputDirectoryClaimError, match="symbolic link"):
        claim_fresh_output_directory(output_dir, owner="skill:test")

    assert list(target_parent.iterdir()) == []


def test_claim_rejects_windows_reparse_output_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junction = tmp_path / "junction"
    junction.mkdir()
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)
    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    with pytest.raises(OutputDirectoryClaimError, match="reparse point"):
        claim_fresh_output_directory(junction / "out", owner="skill:test")

    assert list(junction.iterdir()) == []


@pytest.mark.parametrize("use_relative_path", [False, True])
def test_claim_rejects_symlink_erased_by_parent_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_relative_path: bool,
) -> None:
    lexical_parent = tmp_path / "lexical"
    lexical_parent.mkdir()
    outside_parent = tmp_path / "outside"
    deep_target = outside_parent / "deep"
    deep_target.mkdir(parents=True)
    jump = lexical_parent / "jump"
    jump.symlink_to(deep_target, target_is_directory=True)
    if use_relative_path:
        monkeypatch.chdir(lexical_parent)
        output_dir = Path("jump") / ".." / "out"
    else:
        output_dir = jump / ".." / "out"

    with pytest.raises(OutputDirectoryClaimError, match="symbolic link"):
        claim_fresh_output_directory(output_dir, owner="skill:test")

    assert not (outside_parent / "out").exists()


def test_concurrent_claim_has_exactly_one_winner(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    barrier = Barrier(2)

    def attempt(owner: str) -> str:
        barrier.wait()
        try:
            claim_fresh_output_directory(output_dir, owner=owner)
        except OutputDirectoryClaimError:
            return "rejected"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("skill:a", "skill:b")))

    assert sorted(outcomes) == ["claimed", "rejected"]
    assert (output_dir / OUTPUT_CLAIM_FILENAME).is_file()


def test_session_store_rejects_claim_alias_result_json(tmp_path: Path) -> None:
    from omicsclaw.common.session import OmicsSession
    from omicsclaw.skill.runner import _store_result_in_session

    session_path = tmp_path / "session.json"
    OmicsSession(session_id="test", primary_data_path="original.h5ad").save(
        session_path
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text('{"summary": {"method": "forged"}}\n', encoding="utf-8")
    (output_dir / "result.json").hardlink_to(claim)

    _store_result_in_session(str(session_path), "demo-skill", output_dir)

    session = OmicsSession.load(session_path)
    assert session.get_skill_result("demo-skill") is None
    assert session.h5ad_path == "original.h5ad"


def test_session_store_rejects_claim_alias_primary_h5ad(tmp_path: Path) -> None:
    from omicsclaw.common.session import OmicsSession
    from omicsclaw.skill.runner import _store_result_in_session

    session_path = tmp_path / "session.json"
    OmicsSession(session_id="test", primary_data_path="original.h5ad").save(
        session_path
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (output_dir / "processed.h5ad").hardlink_to(claim)
    (output_dir / "result.json").write_text(
        '{"summary": {"method": "valid"}}\n',
        encoding="utf-8",
    )

    _store_result_in_session(str(session_path), "demo-skill", output_dir)

    session = OmicsSession.load(session_path)
    assert session.get_skill_result("demo-skill") is not None
    assert session.h5ad_path == "original.h5ad"
    assert session.is_step_done("demo-skill") is False


def test_result_status_helpers_reject_claim_alias(tmp_path: Path) -> None:
    from omicsclaw.common.report import mark_result_status, read_result_status

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text('{"status": "ok"}\n', encoding="utf-8")
    (output_dir / "result.json").hardlink_to(claim)

    assert read_result_status(output_dir) is None
    assert mark_result_status(output_dir, "failed") is False
    assert json.loads(claim.read_text(encoding="utf-8")) == {"status": "ok"}


def test_result_writer_does_not_overwrite_claim_alias(tmp_path: Path) -> None:
    from omicsclaw.common.report import write_result_json

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (output_dir / "result.json").hardlink_to(claim)

    with pytest.raises(RuntimeError, match="ownership metadata"):
        write_result_json(
            output_dir,
            skill="demo",
            version="1.0.0",
            summary={},
            data={},
        )

    assert claim.read_text(encoding="utf-8") == "{}\n"


def test_owned_text_writer_rejects_in_tree_symlink_parent(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    real_dir = output_dir / "real"
    real_dir.mkdir(parents=True)
    (output_dir / "reproducibility").symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic-link output parent"):
        atomic_write_owned_output_text(
            output_dir / "reproducibility" / "analysis_notebook.ipynb",
            output_root=output_dir,
            text="{}\n",
            label="analysis notebook",
        )

    assert not (real_dir / "analysis_notebook.ipynb").exists()


def test_owned_text_writer_rejects_windows_reparse_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "out"
    junction = output_dir / "junction"
    junction.mkdir(parents=True)
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)
    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    with pytest.raises(RuntimeError, match="Windows reparse point"):
        atomic_write_owned_output_text(
            junction / "result.json",
            output_root=output_dir,
            text="{}\n",
            label="result envelope",
        )

    assert list(junction.iterdir()) == []


def test_owned_text_writer_rejects_symlink_ancestor_of_output_root(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    output_dir = real_parent / "out"
    output_dir.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic-link output ancestor"):
        atomic_write_owned_output_text(
            linked_parent / "out" / "result.json",
            output_root=linked_parent / "out",
            text="{}\n",
            label="result envelope",
        )

    assert not (output_dir / "result.json").exists()


def test_owned_text_writer_rejects_symlink_before_dotdot_in_output_root(
    tmp_path: Path,
) -> None:
    lexical_parent = tmp_path / "lexical"
    lexical_parent.mkdir()
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    victim_dir = outside / "victim"
    victim_dir.mkdir()
    (lexical_parent / victim_dir.name).mkdir()
    (lexical_parent / "jump").symlink_to(
        outside / "deep",
        target_is_directory=True,
    )
    tainted_root = lexical_parent / "jump" / ".." / victim_dir.name

    with pytest.raises(RuntimeError, match="symbolic-link output ancestor"):
        atomic_write_owned_output_text(
            tainted_root / "result.json",
            output_root=tainted_root,
            text="{}\n",
            label="result envelope",
        )

    assert not (victim_dir / "result.json").exists()


def test_owned_text_writer_rejects_symlink_before_dotdot_in_candidate_parent(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    (output_dir / "real" / "deep").mkdir(parents=True)
    (output_dir / "jump").symlink_to(
        output_dir / "real" / "deep",
        target_is_directory=True,
    )
    victim = output_dir / "real" / "result.json"
    tainted_candidate = output_dir / "jump" / ".." / victim.name

    with pytest.raises(RuntimeError, match="symbolic-link output parent"):
        atomic_write_owned_output_text(
            tainted_candidate,
            output_root=output_dir,
            text="{}\n",
            label="result envelope",
        )

    assert not victim.exists()


@pytest.mark.parametrize("use_relative_paths", [False, True])
def test_owned_text_writer_preserves_normal_contained_path_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_relative_paths: bool,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    output_root = Path("out") if use_relative_paths else output_dir
    destination = output_root / "result.json"

    assert atomic_write_owned_output_text(
        destination,
        output_root=output_root,
        text='{"generation": 1}\n',
    ) == destination
    atomic_write_owned_output_text(
        destination,
        output_root=output_root,
        text='{"generation": 2}\n',
    )

    assert (output_dir / "result.json").read_text(encoding="utf-8") == (
        '{"generation": 2}\n'
    )


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_owned_text_writer_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    fsync_kinds: list[str] = []
    real_fsync = output_claim.os.fsync

    def observed_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(output_claim.os, "fsync", observed_fsync)

    atomic_write_owned_output_text(
        output_dir / "result.json",
        output_root=output_dir,
        text='{"status": "ok"}\n',
    )

    assert fsync_kinds == ["file", "directory"]


def test_result_writer_atomic_failure_preserves_existing_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.common.output_claim as output_claim
    from omicsclaw.common.report import write_result_json

    output_dir = tmp_path / "out"
    original = write_result_json(
        output_dir,
        skill="demo",
        version="1.0.0",
        summary={"generation": "old"},
        data={},
    ).read_bytes()

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(output_claim.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        write_result_json(
            output_dir,
            skill="demo",
            version="1.0.0",
            summary={"generation": "new"},
            data={},
        )

    assert (output_dir / "result.json").read_bytes() == original


def test_output_guide_refuses_symlink_destination(tmp_path: Path) -> None:
    from omicsclaw.common.report import write_output_readme

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("do not replace\n", encoding="utf-8")
    (output_dir / "README.md").symlink_to(outside)

    with pytest.raises(RuntimeError, match="unowned output guide"):
        write_output_readme(output_dir, skill_alias="demo")

    assert outside.read_text(encoding="utf-8") == "do not replace\n"


def test_notebook_export_refuses_symlink_reproducibility_directory(
    tmp_path: Path,
) -> None:
    from omicsclaw.common import notebook_export

    if not notebook_export._NBFORMAT_AVAILABLE:
        pytest.skip("nbformat is not installed")

    output_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    output_dir.mkdir()
    outside.mkdir()
    (output_dir / "reproducibility").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic-link reproducibility directory"):
        notebook_export.write_analysis_notebook(
            output_dir,
            skill_alias="demo",
        )

    assert not (outside / "analysis_notebook.ipynb").exists()
