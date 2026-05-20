"""Unit tests for Hungarian label alignment."""

from __future__ import annotations

import numpy as np
import pytest

from omicsclaw.runtime.consensus.operators.alignment import align_labels


def test_align_permutation_recovers_reference() -> None:
    reference = np.array([0, 0, 1, 1, 2, 2])
    permuted = np.array([2, 2, 0, 0, 1, 1])  # 0→1, 1→2, 2→0
    aligned = align_labels(reference, permuted)
    np.testing.assert_array_equal(aligned, reference)


def test_align_string_labels() -> None:
    reference = np.array(["L1", "L1", "L2", "L2", "L3"])
    source = np.array(["x", "x", "y", "y", "z"])
    aligned = align_labels(reference, source)
    np.testing.assert_array_equal(aligned, reference)


def test_align_source_with_extra_cluster() -> None:
    reference = np.array([0, 0, 1, 1])
    source = np.array([0, 1, 2, 2])
    aligned = align_labels(reference, source)
    # 0 and 1 should map to themselves or swap; 2 is "extra" with no ref match.
    unique_aligned = set(aligned.tolist())
    assert any(str(v).startswith("_extra_") for v in unique_aligned)


def test_align_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError):
        align_labels(np.array([0, 0, 1]), np.array([0, 1]))


def test_align_already_matching_is_identity() -> None:
    reference = np.array([0, 1, 2, 0, 1, 2])
    aligned = align_labels(reference, reference)
    np.testing.assert_array_equal(aligned, reference)


def test_align_idempotent_under_re_alignment() -> None:
    reference = np.array([0, 0, 1, 1, 2, 2, 2])
    permuted = np.array([1, 1, 2, 2, 0, 0, 0])
    aligned_once = align_labels(reference, permuted)
    aligned_twice = align_labels(reference, aligned_once)
    np.testing.assert_array_equal(aligned_once, aligned_twice)
