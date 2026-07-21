"""Unit tests for the ADR-0064 scientific-Memory scope vocabulary."""

import pytest

from omicsclaw.memory.scientific_scope import (
    DatasetPathError,
    MemoryScope,
    dataset_observation_identity,
    is_project_fenced,
    normalize_relative_path,
    provisional_dataset_observation_identity,
    scope_for_memory_type,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64


# --------------------------------------------------------------------------- #
# Scope classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("memory_type", "expected"),
    [
        ("preference", MemoryScope.OWNER),
        ("insight", MemoryScope.PROJECT),
        ("analysis", MemoryScope.PROJECT),
        ("autonomous_run", MemoryScope.PROJECT),
        ("project_context", MemoryScope.PROJECT),
        ("thread", MemoryScope.PROJECT),
        ("thread_source", MemoryScope.PROJECT),
        ("dataset", MemoryScope.WORKSPACE),
    ],
)
def test_scope_for_known_memory_types(memory_type, expected):
    assert scope_for_memory_type(memory_type) is expected


def test_scope_for_memory_type_tolerates_whitespace():
    assert scope_for_memory_type("  insight  ") is MemoryScope.PROJECT


def test_scope_for_unknown_type_fails_closed():
    # A novel scientific type must not silently default to some owner.
    assert scope_for_memory_type("some_future_type") is None
    assert scope_for_memory_type("") is None
    assert scope_for_memory_type(None) is None  # type: ignore[arg-type]


def test_only_project_scope_is_fenced():
    assert is_project_fenced(MemoryScope.PROJECT) is True
    for scope in (
        MemoryScope.OWNER,
        MemoryScope.WORKSPACE,
        MemoryScope.CONVERSATION,
        MemoryScope.RUN,
        MemoryScope.SYSTEM,
    ):
        assert is_project_fenced(scope) is False


def test_dataset_type_is_not_fenced():
    # A Workspace observation may be updated by an unassigned Run — not fenced.
    assert is_project_fenced(scope_for_memory_type("dataset")) is False


# --------------------------------------------------------------------------- #
# Relative-path normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("a/b.h5ad", "a/b.h5ad"),
        ("./a/b.h5ad", "a/b.h5ad"),
        ("a//b.h5ad", "a/b.h5ad"),
        ("a/./b.h5ad", "a/b.h5ad"),
        ("a/c/../b.h5ad", "a/b.h5ad"),
        ("  a/b.h5ad  ", "a/b.h5ad"),
        ("data\\sample.h5ad", "data/sample.h5ad"),
        ("sample.h5ad", "sample.h5ad"),
    ],
)
def test_normalize_relative_path_collapses_spellings(raw, normalized):
    assert normalize_relative_path(raw) == normalized


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/abs/path.h5ad",
        "\\windows\\abs.h5ad",
        "C:/drive/path.h5ad",
        "..",
        "../escape.h5ad",
        "a/../../escape.h5ad",
        "./..",
    ],
)
def test_normalize_relative_path_rejects_absolute_and_escapes(bad):
    with pytest.raises(DatasetPathError):
        normalize_relative_path(bad)


# --------------------------------------------------------------------------- #
# Settled dataset-observation identity
# --------------------------------------------------------------------------- #


def test_dataset_identity_is_stable_across_path_spellings():
    a = dataset_observation_identity(
        workspace_id="ws1", relative_path="data/sample.h5ad", content_sha256=_SHA_A
    )
    b = dataset_observation_identity(
        workspace_id="ws1", relative_path="./data//sample.h5ad", content_sha256=_SHA_A
    )
    assert a == b
    assert a.startswith("dataset-obs:")


def test_dataset_identity_differs_by_digest():
    a = dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", content_sha256=_SHA_A
    )
    b = dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", content_sha256=_SHA_B
    )
    assert a != b


def test_dataset_identity_differs_by_workspace():
    a = dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", content_sha256=_SHA_A
    )
    b = dataset_observation_identity(
        workspace_id="ws2", relative_path="s.h5ad", content_sha256=_SHA_A
    )
    assert a != b


def test_dataset_identity_same_name_different_path_does_not_dedup():
    # "Display filename alone never dedups a dataset" (ADR 0064 §2).
    a = dataset_observation_identity(
        workspace_id="ws1", relative_path="run1/sample.h5ad", content_sha256=_SHA_A
    )
    b = dataset_observation_identity(
        workspace_id="ws1", relative_path="run2/sample.h5ad", content_sha256=_SHA_A
    )
    assert a != b


def test_dataset_identity_requires_valid_digest():
    for bad in ("", "xyz", "A" * 64 + "z", "g" * 64):
        with pytest.raises(ValueError):
            dataset_observation_identity(
                workspace_id="ws1", relative_path="s.h5ad", content_sha256=bad
            )


def test_dataset_identity_uppercase_digest_is_normalized():
    lower = dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", content_sha256=_SHA_A
    )
    upper = dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", content_sha256=_SHA_A.upper()
    )
    assert lower == upper


def test_dataset_identity_requires_workspace():
    with pytest.raises(ValueError):
        dataset_observation_identity(
            workspace_id="", relative_path="s.h5ad", content_sha256=_SHA_A
        )


# --------------------------------------------------------------------------- #
# Provisional dataset identity
# --------------------------------------------------------------------------- #


def test_provisional_identity_never_equals_settled():
    settled = dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", content_sha256=_SHA_A
    )
    prov = provisional_dataset_observation_identity(
        workspace_id="ws1", relative_path="s.h5ad", observed_size=10, observed_mtime_ns=5
    )
    assert prov.startswith("dataset-prov:")
    assert prov != settled


def test_provisional_identity_differs_by_path():
    a = provisional_dataset_observation_identity(
        workspace_id="ws1", relative_path="a.h5ad", observed_size=10, observed_mtime_ns=5
    )
    b = provisional_dataset_observation_identity(
        workspace_id="ws1", relative_path="b.h5ad", observed_size=10, observed_mtime_ns=5
    )
    # Same size+mtime at a DIFFERENT path must not merge (ADR 0064 §2).
    assert a != b


def test_provisional_identity_differs_by_size_and_mtime():
    base = dict(workspace_id="ws1", relative_path="s.h5ad")
    a = provisional_dataset_observation_identity(**base, observed_size=10, observed_mtime_ns=5)
    b = provisional_dataset_observation_identity(**base, observed_size=11, observed_mtime_ns=5)
    c = provisional_dataset_observation_identity(**base, observed_size=10, observed_mtime_ns=6)
    assert a != b
    assert a != c
    assert b != c


def test_provisional_identity_is_stable():
    base = dict(
        workspace_id="ws1", relative_path="s.h5ad", observed_size=10, observed_mtime_ns=5
    )
    assert provisional_dataset_observation_identity(
        **base
    ) == provisional_dataset_observation_identity(**base)


def test_provisional_identity_rejects_negative_stat():
    with pytest.raises(ValueError):
        provisional_dataset_observation_identity(
            workspace_id="ws1", relative_path="s.h5ad", observed_size=-1, observed_mtime_ns=5
        )
