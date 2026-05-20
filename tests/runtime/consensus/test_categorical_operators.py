"""Unit tests for kmode + weighted consensus operators."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from omicsclaw.runtime.consensus.operators.categorical import (
    kmode_consensus,
    normalize_by_frequency,
    weighted_consensus,
)


def _build_labels_df(arrays: dict[str, list[int]]) -> pd.DataFrame:
    return pd.DataFrame(arrays, index=[f"obs_{i}" for i in range(len(next(iter(arrays.values()))))])


def test_normalize_by_frequency_ranks_descending() -> None:
    labels = np.array([5, 5, 5, 3, 3, 7])
    normalized = normalize_by_frequency(labels)
    # 5 most frequent (3 times) → 1; 3 second (2 times) → 2; 7 last → 3.
    np.testing.assert_array_equal(normalized, np.array([1, 1, 1, 2, 2, 3]))


def test_normalize_by_frequency_stable_on_ties() -> None:
    labels = np.array(["b", "a", "b", "a"])
    normalized = normalize_by_frequency(labels)
    # tie broken by label name ascending → "a" first → 1
    np.testing.assert_array_equal(normalized, np.array([2, 1, 2, 1]))


def test_kmode_unanimous_members_yields_their_labels() -> None:
    labels_df = _build_labels_df({
        "m1": [0, 0, 1, 1, 2, 2],
        "m2": [0, 0, 1, 1, 2, 2],
        "m3": [0, 0, 1, 1, 2, 2],
    })
    result = kmode_consensus(labels_df, seed=42)
    np.testing.assert_array_equal(result.labels.to_numpy(), np.array([1, 1, 2, 2, 3, 3]))
    assert result.method == "kmode"
    assert result.n_clusters_returned == 3


def test_kmode_aligns_permuted_members_before_voting() -> None:
    labels_df = _build_labels_df({
        "m1": [0, 0, 1, 1, 2, 2],
        "m2": [2, 2, 0, 0, 1, 1],  # 0→1, 1→2, 2→0 permutation
        "m3": [1, 1, 2, 2, 0, 0],
    })
    result = kmode_consensus(labels_df, seed=0)
    # all three actually agree after relabel-and-align; consensus should be 3 groups
    assert result.n_clusters_returned == 3
    groups = result.labels.groupby(result.labels.to_numpy()).indices
    assert {len(v) for v in groups.values()} == {2}


def test_kmode_outvotes_dissenter() -> None:
    labels_df = _build_labels_df({
        "m1": [0, 0, 1, 1, 2, 2],
        "m2": [0, 0, 1, 1, 2, 2],
        "m3": [0, 0, 1, 1, 2, 2],
        "outlier": [1, 1, 1, 1, 1, 1],  # all-one-cluster; relabels to all-1
    })
    result = kmode_consensus(labels_df, seed=0)
    # 3-vs-1 majority still gives 3 clusters
    assert result.n_clusters_returned == 3


def test_weighted_majority_dominates_when_heavy() -> None:
    labels_df = _build_labels_df({
        "m1": [0, 0, 0],
        "m2": [0, 0, 0],
        "heavy": [1, 1, 1],
    })
    result = weighted_consensus(
        labels_df,
        weights={"m1": 1.0, "m2": 1.0, "heavy": 10.0},
        seed=0,
    )
    # heavy member's labels (post-normalize → 1) should dominate
    assert (result.labels.nunique() == 1)


def test_weighted_rejects_missing_weight() -> None:
    labels_df = _build_labels_df({"m1": [0, 1, 1], "m2": [0, 1, 1]})
    with pytest.raises(ValueError, match="missing member"):
        weighted_consensus(labels_df, weights={"m1": 1.0}, seed=0)


def test_weighted_rejects_wrong_length_sequence() -> None:
    labels_df = _build_labels_df({"m1": [0, 1, 1], "m2": [0, 1, 1]})
    with pytest.raises(ValueError, match="length"):
        weighted_consensus(labels_df, weights=[1.0, 2.0, 3.0], seed=0)


def test_kmode_requires_two_members() -> None:
    labels_df = _build_labels_df({"only": [0, 1, 1]})
    with pytest.raises(ValueError, match="at least 2"):
        kmode_consensus(labels_df)


def test_kmode_deterministic_under_same_seed() -> None:
    labels_df = _build_labels_df({
        "m1": [0, 0, 1, 1],
        "m2": [1, 0, 1, 0],
        "m3": [0, 1, 0, 1],
    })
    a = kmode_consensus(labels_df, seed=7)
    b = kmode_consensus(labels_df, seed=7)
    np.testing.assert_array_equal(a.labels.to_numpy(), b.labels.to_numpy())


def test_kmode_tiebreak_is_seed_independent_when_ties_exist() -> None:
    """I4 regression: kmode's tiebreak is documented as ``earliest column wins``
    — deterministic and seed-independent.

    Even-numbered members with 2-2 disagreements force genuine ties at the
    per-observation mode step. Output must be identical across different seeds.
    """
    # 4 members, 2 clusters, constructed so multiple rows have a 2-2 split.
    labels_df = _build_labels_df({
        "a": [0, 1, 0, 1],
        "b": [1, 0, 0, 1],
        "c": [0, 0, 1, 1],
        "d": [1, 1, 1, 0],
    })
    seed0 = kmode_consensus(labels_df, seed=0)
    seed999 = kmode_consensus(labels_df, seed=999)
    np.testing.assert_array_equal(seed0.labels.to_numpy(), seed999.labels.to_numpy())
