"""Continuous (rank-gauge) consensus operators + math (ADR 0031).

The single-cell analog of :mod:`operators.categorical` for per-cell
**pseudotime/score vector** consensus. A pseudotime is only defined *up to a
monotone reparameterisation and a direction flip*, so members are made comparable
by **rank-normalisation** (cancels the monotone gauge) + a **direction safeguard**
(cancels the flip), then aggregated by a per-cell ``median`` (default) or
agreement-``weighted`` mean, and **re-ranked** so the consensus is itself a clean
``[0, 1]`` pseudotime. Per-cell dispersion (``2·MAD`` + ``range``) is the
continuous analog of categorical per-cell vote support.

Every function is pure + deterministic given its inputs (no RNG); ``seed`` is
accepted for call symmetry with the categorical operators.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import rankdata, spearmanr


def rank_normalize(values: np.ndarray) -> np.ndarray:
    """Empirical rank in ``[0, 1]``: ``(avg_rank_1based − 1) / (n − 1)``.

    Ties → average rank. Monotone-invariant, so it cancels the residual
    (post-root) pseudotime gauge — the continuous analog of label-matching.
    Raises ``ValueError`` for ``n < 2`` (a single cell has no rank scale).
    """
    arr = np.asarray(values, dtype=float)
    n = arr.size
    if n < 2:
        raise ValueError("rank_normalize needs >= 2 values")
    ranks = rankdata(arr, method="average")  # 1-based, ties averaged
    return (ranks - 1.0) / (n - 1.0)


def is_degenerate(values: np.ndarray) -> bool:
    """True if the vector cannot define a rank ordering: non-finite or ``< 2`` unique.

    Spearman correlation and rank-normalisation are undefined/degenerate for a
    constant or non-finite vector, so such a member is dropped whole upstream
    (ADR 0031 hardening) rather than poisoning the agreement matrix.
    """
    arr = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(arr)):
        return True
    return np.unique(arr).size < 2


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman ρ, coerced to a finite float (0.0 if undefined)."""
    rho = spearmanr(a, b).correlation
    return float(rho) if np.isfinite(rho) else 0.0


def align_directions(
    rank_by_member: dict[str, np.ndarray],
) -> tuple[dict[str, np.ndarray], str, list[str]]:
    """Direction safeguard (single pass, non-circular). ADR 0031 §4.

    The shared root already orients every member, so this is a guard, not the
    primary mechanism. The **anchor** is the member with the highest mean
    pairwise ``|ρ|`` (the most central member — a reference that does *not*
    depend on any flip); ties break by lowest member name for reproducibility.
    In one pass, any member whose rank vector anti-correlates (``ρ < 0``) with
    the anchor is flipped (``rank → 1 − rank``). Returns
    ``(aligned, anchor, flipped)``.
    """
    members = list(rank_by_member.keys())
    mean_abs: dict[str, float] = {}
    for m in members:
        others = [rank_by_member[o] for o in members if o != m]
        mean_abs[m] = (
            float(np.mean([abs(_spearman(rank_by_member[m], o)) for o in others]))
            if others
            else 0.0
        )
    # max() over a name-sorted iteration returns the lowest-named member among
    # ties on mean |ρ| → deterministic anchor tie-break.
    anchor = max(sorted(members), key=lambda m: mean_abs[m])
    anchor_rank = rank_by_member[anchor]

    aligned: dict[str, np.ndarray] = {}
    flipped: list[str] = []
    for m in members:
        r = rank_by_member[m]
        if m != anchor and _spearman(r, anchor_rank) < 0:
            aligned[m] = 1.0 - r
            flipped.append(m)
        else:
            aligned[m] = r
    return aligned, anchor, flipped


def pairwise_spearman(aligned: dict[str, np.ndarray]) -> pd.DataFrame:
    """Symmetric pairwise Spearman matrix over the (aligned) member vectors."""
    members = list(aligned.keys())
    n = len(members)
    mat = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            rho = _spearman(aligned[members[i]], aligned[members[j]])
            mat[i, j] = mat[j, i] = rho
    return pd.DataFrame(mat, index=members, columns=members)


def weak_agreement_stats(agreement: pd.DataFrame, *, threshold: float = 0.5) -> dict:
    """Cohort + worst-pair agreement for the weak-agreement guard (ADR 0031 §9).

    The cohort mean of all off-diagonal pairwise ρ can hide one bad pair, so the
    worst pair (``min_pairwise_spearman`` + the pair) and every sub-threshold
    pair are surfaced too. ``diverged`` is the report-only warning condition.
    """
    members = list(agreement.columns)
    pairs: list[tuple[str, str, float]] = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            pairs.append((members[i], members[j], float(agreement.iloc[i, j])))
    if not pairs:
        return {
            "cohort_mean_spearman": 1.0,
            "min_pairwise_spearman": 1.0,
            "min_pair": [],
            "weak_pairs": [],
            "diverged": False,
            "threshold": threshold,
        }
    vals = [p[2] for p in pairs]
    cohort_mean = float(np.mean(vals))
    worst = min(pairs, key=lambda p: p[2])
    weak = [[a, b, round(v, 4)] for a, b, v in pairs if v < threshold]
    return {
        "cohort_mean_spearman": cohort_mean,
        "min_pairwise_spearman": worst[2],
        "min_pair": [worst[0], worst[1]],
        "weak_pairs": weak,
        "diverged": cohort_mean < threshold,
        "threshold": threshold,
    }


@dataclass(frozen=True)
class ContinuousConsensusResult:
    """Aggregated continuous consensus + per-cell dispersion (ADR 0031)."""

    pseudotime: pd.Series       # consensus, re-ranked to [0, 1], indexed by obs
    pseudotime_mad: pd.Series   # clip(2·MAD, 0, 1) — majority-support dispersion
    value_range: pd.Series      # max − min of aligned ranks — full-disagreement companion
    tie_fraction: float         # fraction of tied consensus ranks (flatness visibility)
    operator: str
    seed: int
    n_voting: int


def _rerank(values: np.ndarray, index: pd.Index) -> pd.Series:
    return pd.Series(rank_normalize(values), index=index)


def _support(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-cell ``(pseudotime_mad, range)`` from the aligned (n_cells, n_members) matrix.

    MAD is a *majority*-support metric (with 3 members ``[0, 0, 1]`` has MAD 0
    yet one method disagrees completely), so the per-cell ``range`` is reported
    alongside as the full-disagreement companion — ADR 0031 §8.
    """
    med = np.median(matrix, axis=1)
    mad = np.median(np.abs(matrix - med[:, None]), axis=1)
    pseudotime_mad = np.clip(2.0 * mad, 0.0, 1.0)
    value_range = matrix.max(axis=1) - matrix.min(axis=1)
    return pseudotime_mad, value_range


def _tie_fraction(values: np.ndarray) -> float:
    n = values.size
    if n == 0:
        return 0.0
    return float(1.0 - np.unique(np.round(values, 12)).size / n)


def _finish(agg: np.ndarray, matrix: np.ndarray, index: pd.Index, *, operator: str, seed: int) -> ContinuousConsensusResult:
    pt = _rerank(agg, index)
    mad, rng = _support(matrix)
    return ContinuousConsensusResult(
        pseudotime=pt,
        pseudotime_mad=pd.Series(mad, index=index),
        value_range=pd.Series(rng, index=index),
        tie_fraction=_tie_fraction(pt.to_numpy()),
        operator=operator,
        seed=seed,
        n_voting=matrix.shape[1],
    )


def median_consensus(aligned_df: pd.DataFrame, *, seed: int = 0) -> ContinuousConsensusResult:
    """Per-cell median of the aligned rank vectors, then re-ranked. Robust default."""
    matrix = aligned_df.to_numpy(dtype=float)
    return _finish(np.median(matrix, axis=1), matrix, aligned_df.index, operator="median", seed=seed)


def weighted_consensus(
    aligned_df: pd.DataFrame, weights: dict[str, float], *, seed: int = 0
) -> ContinuousConsensusResult:
    """Per-cell agreement-weighted mean, then re-ranked.

    Weights are mapped to **non-negative** (``max(w, 0)``) and normalised; a
    negative-weighted mean of pseudotimes is meaningless, so a member that
    anti-correlates with the cohort gets weight 0. If every weight is 0 the
    operator **falls back to the median** (ADR 0031 hardening).
    """
    members = list(aligned_df.columns)
    matrix = aligned_df.to_numpy(dtype=float)
    w = np.array([max(float(weights.get(m, 0.0)), 0.0) for m in members], dtype=float)
    agg = np.median(matrix, axis=1) if w.sum() <= 0 else matrix @ (w / w.sum())
    return _finish(agg, matrix, aligned_df.index, operator="weighted", seed=seed)
