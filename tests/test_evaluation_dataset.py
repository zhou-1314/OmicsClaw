from __future__ import annotations

from pathlib import Path

import pytest

import omicsclaw.skill.evaluation_dataset as evaluation_dataset
from omicsclaw.skill.evaluation_dataset import (
    DatasetIntegrityError,
    digest_dataset_selection,
    digest_dataset_tree,
    resolve_repository_dataset,
)
from omicsclaw.skill.schema import EvaluationDatasetRef


def test_dataset_tree_digest_is_deterministic_and_content_bound(tmp_path):
    dataset = tmp_path / "repo" / "data" / "case"
    dataset.mkdir(parents=True)
    (dataset / "a.txt").write_text("alpha", encoding="utf-8")
    (dataset / "nested").mkdir()
    (dataset / "nested" / "b.txt").write_text("beta", encoding="utf-8")

    first = digest_dataset_tree(dataset)
    assert first == digest_dataset_tree(dataset)
    assert first.startswith("sha256:")

    (dataset / "nested" / "b.txt").write_text("changed", encoding="utf-8")
    assert digest_dataset_tree(dataset) != first


def test_repository_dataset_resolution_verifies_declared_digest(tmp_path):
    repo = tmp_path / "repo"
    dataset = repo / "data" / "case"
    dataset.mkdir(parents=True)
    (dataset / "fixture.txt").write_text("fixture", encoding="utf-8")
    digest = digest_dataset_tree(dataset)
    ref = EvaluationDatasetRef(
        store="repository",
        path="data/case",
        content_sha256=digest,
    )

    resolved, observed = resolve_repository_dataset(repo, ref)
    assert resolved == dataset.resolve()
    assert observed == digest

    drifted = ref.model_copy(
        update={"content_sha256": "sha256:" + "0" * 64}
    )
    with pytest.raises(DatasetIntegrityError, match="digest mismatch"):
        resolve_repository_dataset(repo, drifted)


def test_dataset_tree_rejects_symlinked_content(tmp_path):
    dataset = tmp_path / "case"
    dataset.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = dataset / "alias.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(DatasetIntegrityError, match="symbolic link"):
        digest_dataset_tree(dataset)


def test_dataset_digest_excludes_generated_python_bytecode(tmp_path):
    dataset = tmp_path / "case"
    cache = dataset / "tests" / "__pycache__"
    cache.mkdir(parents=True)
    (dataset / "tests" / "grader.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = digest_dataset_tree(dataset)

    (cache / "grader.cpython-311.pyc").write_bytes(b"generated cache")
    assert digest_dataset_tree(dataset) == before


def test_dataset_digest_fails_closed_when_a_subtree_cannot_be_enumerated(
    tmp_path, monkeypatch
):
    dataset = tmp_path / "case"
    dataset.mkdir()

    def unreadable_walk(_root, *, followlinks, onerror=None):
        assert followlinks is False
        assert onerror is not None
        onerror(PermissionError("subtree cannot be read"))
        return iter(())

    monkeypatch.setattr(evaluation_dataset.os, "walk", unreadable_walk)
    with pytest.raises(DatasetIntegrityError, match="cannot enumerate dataset"):
        digest_dataset_tree(dataset)


def test_selected_member_digest_binds_only_declared_case_content(tmp_path):
    repo = tmp_path / "repo"
    suite = repo / "data" / "suite"
    (suite / "inputs").mkdir(parents=True)
    (suite / "graders").mkdir()
    (suite / "unrelated").mkdir()
    (suite / "inputs" / "case.h5").write_bytes(b"fixture")
    (suite / "graders" / "case.py").write_text("CHECKS = 3\n", encoding="utf-8")
    (suite / "unrelated" / "other.bin").write_bytes(b"other")
    members = ["graders/case.py", "inputs/case.h5"]
    digest = digest_dataset_selection(suite, members)
    ref = EvaluationDatasetRef(
        store="repository",
        path="data/suite",
        members=members,
        content_sha256=digest,
    )

    resolved, observed = resolve_repository_dataset(repo, ref)
    assert resolved == suite.resolve()
    assert observed == digest

    (suite / "unrelated" / "other.bin").write_bytes(b"unrelated drift")
    assert resolve_repository_dataset(repo, ref)[1] == digest

    (suite / "graders" / "case.py").write_text("CHECKS = 4\n", encoding="utf-8")
    with pytest.raises(DatasetIntegrityError, match="digest mismatch"):
        resolve_repository_dataset(repo, ref)


def test_selected_member_digest_rejects_cross_member_snapshot_drift(
    tmp_path, monkeypatch
):
    suite = tmp_path / "suite"
    suite.mkdir()
    first = suite / "a.txt"
    second = suite / "b.txt"
    first.write_text("a-before", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    real_digest = evaluation_dataset.digest_dataset_tree

    def mutate_after_hash(path):
        digest = real_digest(path)
        if Path(path).name == "a.txt":
            first.write_text("a-after", encoding="utf-8")
        return digest

    monkeypatch.setattr(evaluation_dataset, "digest_dataset_tree", mutate_after_hash)
    with pytest.raises(DatasetIntegrityError, match="selection changed while hashing"):
        digest_dataset_selection(suite, ["a.txt", "b.txt"])


def test_dataset_member_contract_rejects_overlap_and_symlink(tmp_path):
    with pytest.raises(ValueError, match="unique"):
        EvaluationDatasetRef(
            store="repository",
            path="data/suite",
            members=["case/input.h5", "case/input.h5"],
            content_sha256="sha256:" + "0" * 64,
        )

    with pytest.raises(ValueError, match="overlapping"):
        EvaluationDatasetRef(
            store="repository",
            path="data/suite",
            members=["case", "case/input.h5"],
            content_sha256="sha256:" + "0" * 64,
        )

    suite = tmp_path / "suite"
    suite.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = suite / "input.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(DatasetIntegrityError, match="symbolic link"):
        digest_dataset_selection(suite, ["input.txt"])
