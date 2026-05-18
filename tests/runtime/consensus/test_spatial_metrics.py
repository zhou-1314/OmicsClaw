"""Tests for ported spatial-aware metrics — MLAMI, CHAOS, PAS.

The metrics are checked on synthetic spatial datasets with known structure:
labels perfectly matching spatial clusters should score at one extreme,
random labels at the other.
"""

from __future__ import annotations

import numpy as np
import pytest


# ---------- helpers -------------------------------------------------------- #

def _three_well_separated_clusters(n_per: int = 10, jitter: float = 0.1) -> np.ndarray:
    """Return ``3 * n_per`` 2-D points sampled from 3 tight Gaussian clusters."""
    rng = np.random.default_rng(0)
    centers = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]])
    blobs = [rng.normal(c, jitter, size=(n_per, 2)) for c in centers]
    return np.vstack(blobs)


def _perfect_labels(n_per: int = 10) -> np.ndarray:
    return np.repeat(np.array([0, 1, 2]), n_per)


# ---------- CHAOS ---------------------------------------------------------- #

def test_chaos_perfect_alignment_scores_near_one() -> None:
    from omicsclaw.runtime.consensus.spatial_metrics import chaos

    coords = _three_well_separated_clusters(n_per=10, jitter=0.05)
    labels = _perfect_labels(n_per=10)
    score = chaos(labels, coords, k=5)
    assert score > 0.95, f"expected CHAOS ≈ 1 for perfectly aligned labels, got {score}"


def test_chaos_random_labels_scores_low() -> None:
    from omicsclaw.runtime.consensus.spatial_metrics import chaos

    rng = np.random.default_rng(0)
    coords = rng.uniform(0, 10, (60, 2))
    labels = rng.integers(0, 3, 60)
    score = chaos(labels, coords, k=5)
    # Uniform random labels on a uniform spatial field expect ~1/3 ≈ 0.33;
    # accept anything significantly below the 0.95 "perfect" mark.
    assert score < 0.6, f"expected CHAOS low for random labels, got {score}"


def test_chaos_value_in_unit_interval() -> None:
    from omicsclaw.runtime.consensus.spatial_metrics import chaos

    coords = _three_well_separated_clusters(n_per=10)
    labels = _perfect_labels(n_per=10)
    score = chaos(labels, coords, k=5)
    assert 0.0 <= score <= 1.0


# ---------- PAS ------------------------------------------------------------ #

def test_pas_perfect_alignment_scores_near_zero() -> None:
    from omicsclaw.runtime.consensus.spatial_metrics import pas

    coords = _three_well_separated_clusters(n_per=10, jitter=0.05)
    labels = _perfect_labels(n_per=10)
    score = pas(labels, coords, k=5, threshold=0.5)
    assert score < 0.05, f"expected PAS ≈ 0 for perfectly aligned labels, got {score}"


def test_pas_random_labels_scores_high() -> None:
    from omicsclaw.runtime.consensus.spatial_metrics import pas

    rng = np.random.default_rng(0)
    coords = rng.uniform(0, 10, (60, 2))
    labels = rng.integers(0, 3, 60)
    score = pas(labels, coords, k=5, threshold=0.5)
    assert score > 0.4, f"expected PAS high for random labels, got {score}"


def test_pas_value_in_unit_interval() -> None:
    from omicsclaw.runtime.consensus.spatial_metrics import pas

    coords = _three_well_separated_clusters(n_per=10)
    labels = _perfect_labels(n_per=10)
    score = pas(labels, coords, k=5)
    assert 0.0 <= score <= 1.0


# ---------- MLAMI ---------------------------------------------------------- #

def test_mlami_perfect_alignment_scores_high() -> None:
    pytest.importorskip("scanpy")
    from omicsclaw.runtime.consensus.spatial_metrics import mlami

    coords = _three_well_separated_clusters(n_per=20, jitter=0.05)
    labels = _perfect_labels(n_per=20)
    score = mlami(labels, coords, n_neighbors=10, seed=0)
    # Perfect spatial separation + matching labels → Leiden at SOME resolution
    # should find the same 3 clusters → AMI ≈ 1.
    assert score > 0.85, f"expected MLAMI ≈ 1 for perfectly aligned labels, got {score}"


def test_mlami_random_labels_scores_low() -> None:
    pytest.importorskip("scanpy")
    from omicsclaw.runtime.consensus.spatial_metrics import mlami

    rng = np.random.default_rng(0)
    # Uniform random labels on a uniform spatial field — no spatial structure
    # the labels can match.
    coords = rng.uniform(0, 10, (90, 2))
    labels = rng.integers(0, 3, 90)
    score = mlami(labels, coords, n_neighbors=10, seed=0)
    assert score < 0.3, f"expected MLAMI low for random labels, got {score}"


def test_mlami_value_in_unit_interval() -> None:
    pytest.importorskip("scanpy")
    from omicsclaw.runtime.consensus.spatial_metrics import mlami

    coords = _three_well_separated_clusters(n_per=20)
    labels = _perfect_labels(n_per=20)
    score = mlami(labels, coords, n_neighbors=10, seed=0)
    # AMI can be slightly negative for adversarial cases; bound loosely.
    assert -0.05 <= score <= 1.0


def test_mlami_deterministic_under_same_seed() -> None:
    pytest.importorskip("scanpy")
    from omicsclaw.runtime.consensus.spatial_metrics import mlami

    coords = _three_well_separated_clusters(n_per=15)
    labels = _perfect_labels(n_per=15)
    a = mlami(labels, coords, n_neighbors=10, seed=42)
    b = mlami(labels, coords, n_neighbors=10, seed=42)
    assert a == b


# ---------- input validation ----------------------------------------------- #

@pytest.mark.parametrize(
    "fn_name",
    ["chaos", "pas", "mlami"],
)
def test_rejects_shape_mismatch(fn_name: str) -> None:
    from omicsclaw.runtime.consensus import spatial_metrics as sm

    fn = getattr(sm, fn_name)
    coords = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    labels = np.array([0, 1])  # one short
    with pytest.raises(ValueError):
        fn(labels, coords)
