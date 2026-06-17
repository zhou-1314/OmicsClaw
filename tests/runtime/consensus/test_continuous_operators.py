"""Unit tests for the continuous (rank-gauge) operators + math (ADR 0031)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from omicsclaw.runtime.consensus.operators.continuous import (
    align_directions,
    is_degenerate,
    median_consensus,
    pairwise_spearman,
    rank_normalize,
    weak_agreement_stats,
    weighted_consensus,
)


# ---- rank_normalize -------------------------------------------------------- #

def test_rank_normalize_ties_average_and_bounds():
    out = rank_normalize(np.array([3.0, 1.0, 2.0, 2.0]))
    # 1-based avg ranks [4,1,2.5,2.5] -> (r-1)/3
    assert np.allclose(out, [1.0, 0.0, 0.5, 0.5])
    assert out.min() == 0.0 and out.max() == 1.0


def test_rank_normalize_monotone_invariant():
    base = np.array([0.1, 0.2, 0.9, 1.5, 3.0])
    # any strictly-monotone reparam yields the SAME rank vector
    assert np.allclose(rank_normalize(base), rank_normalize(np.exp(base)))
    assert np.allclose(rank_normalize(base), rank_normalize(base * 100 + 7))


def test_rank_normalize_rejects_single():
    with pytest.raises(ValueError):
        rank_normalize(np.array([1.0]))


# ---- is_degenerate --------------------------------------------------------- #

@pytest.mark.parametrize(
    "vals,expected",
    [
        ([1.0, 1.0, 1.0], True),                 # constant
        ([0.0, np.nan, 1.0], True),              # non-finite
        ([0.0, np.inf], True),                   # non-finite
        ([0.0, 1.0], False),                     # 2 unique
        ([0.0, 0.0, 1.0], False),                # >=2 unique
    ],
)
def test_is_degenerate(vals, expected):
    assert is_degenerate(np.array(vals, dtype=float)) is expected


# ---- align_directions ------------------------------------------------------ #

def test_align_flips_anticorrelated_member():
    n = 30
    base = np.arange(n, dtype=float)
    rk = {"a": rank_normalize(base), "b": rank_normalize(base), "c": rank_normalize(-base)}
    aligned, anchor, flipped = align_directions(rk)
    assert flipped == ["c"]                       # the anti-correlated member flips
    assert anchor in ("a", "b")
    # after the flip every pair agrees positively
    agg = pairwise_spearman(aligned)
    assert (agg.to_numpy() > 0).all()


def test_align_anchor_tie_break_is_deterministic_lowest_name():
    n = 20
    base = np.arange(n, dtype=float)
    # a and b identical, both maximally central -> tie -> lowest name "a" wins
    rk = {"b": rank_normalize(base), "a": rank_normalize(base), "c": rank_normalize(base)}
    _, anchor, _ = align_directions(rk)
    assert anchor == "a"


# ---- pairwise_spearman + weak guard --------------------------------------- #

def test_weak_agreement_flags_divergence_and_worst_pair():
    n = 40
    base = np.arange(n, dtype=float)
    rng = np.random.default_rng(0)
    # a,b agree; c is noise (low agreement with both)
    aligned = {
        "a": rank_normalize(base),
        "b": rank_normalize(base + rng.normal(0, 0.3, n)),
        "c": rank_normalize(rng.normal(0, 1, n)),
    }
    agg = pairwise_spearman(aligned)
    wa = weak_agreement_stats(agg, threshold=0.5)
    assert wa["min_pairwise_spearman"] <= wa["cohort_mean_spearman"]
    assert set(wa["min_pair"]) <= {"a", "b", "c"} and "c" in wa["min_pair"]
    # cohort mean dragged down by c -> likely diverged; worst pair involves c
    assert wa["diverged"] is (wa["cohort_mean_spearman"] < 0.5)


# ---- operators: median / weighted ----------------------------------------- #

def _aligned_df(cols: dict[str, list[float]]) -> pd.DataFrame:
    idx = [f"obs_{i}" for i in range(len(next(iter(cols.values()))))]
    return pd.DataFrame({k: np.array(v, dtype=float) for k, v in cols.items()}, index=idx)


def test_median_consensus_reranks_to_unit_interval():
    adf = _aligned_df({"a": [0.0, 0.5, 1.0], "b": [0.0, 0.5, 1.0]})
    res = median_consensus(adf)
    assert res.operator == "median" and res.n_voting == 2
    assert res.pseudotime.min() == 0.0 and res.pseudotime.max() == 1.0
    assert res.pseudotime_mad.between(0.0, 1.0).all()


def test_mad_majority_support_vs_range_companion():
    # one cell is [0, 0, 1]: MAD=0 (majority support) but range=1 (full disagreement)
    adf = _aligned_df({"a": [0.0, 0.0], "b": [0.0, 0.5], "c": [1.0, 1.0]})
    res = median_consensus(adf)
    assert res.pseudotime_mad.iloc[0] == pytest.approx(0.0)   # 2*MAD of [0,0,1]
    assert res.value_range.iloc[0] == pytest.approx(1.0)      # range catches the outlier


def test_weighted_clamps_negative_weight():
    # c anti-correlates; a,b agree on order. With c's weight clamped to 0 the
    # consensus follows a,b's (ascending) order, not c's.
    adf = _aligned_df({"a": [0.0, 0.5, 1.0], "b": [0.0, 0.5, 1.0], "c": [1.0, 0.5, 0.0]})
    res = weighted_consensus(adf, {"a": 0.9, "b": 0.8, "c": -0.5})
    assert res.operator == "weighted"
    assert res.pseudotime.iloc[0] < res.pseudotime.iloc[2]   # ascending, c ignored


def test_weighted_all_zero_falls_back_to_median():
    adf = _aligned_df({"a": [0.0, 0.5, 1.0], "b": [0.0, 0.5, 1.0]})
    w = weighted_consensus(adf, {"a": 0.0, "b": 0.0})
    m = median_consensus(adf)
    assert np.allclose(w.pseudotime.to_numpy(), m.pseudotime.to_numpy())


def test_tie_fraction_reports_flatness():
    # all members place every cell identically tied -> high tie fraction after rerank
    adf = _aligned_df({"a": [0.5, 0.5, 0.5], "b": [0.5, 0.5, 0.5]})
    res = median_consensus(adf)
    assert res.tie_fraction == pytest.approx(1.0 - 1.0 / 3.0)
