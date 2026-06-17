"""Member scoring for the continuous (rank-gauge) consensus template (ADR 0031).

v1 is **agreement-only** (``β = 0``): a member's composite score is its mean
pairwise Spearman agreement with the *other* members (the row mean of the
pairwise-ρ matrix). There is no intrinsic panel and no ``n_clusters`` — the
categorical ``scoring.py`` scaffold (``cross_NMI`` + ``MemberScore.n_clusters``)
does not apply here, so this is a parallel, smaller score type.

``ContinuousMemberScore`` deliberately carries ``member`` / ``composite`` /
``filtered`` so the categorical ``scoring.top_k_by_score`` (the one genuinely
reusable BC-selection helper) works unchanged on a continuous score list.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ContinuousMemberScore:
    """One row of the continuous BC-ranking output.

    ``selected`` / ``selection_reason`` are populated by the driver *after* BC
    selection (scoring itself doesn't know top-K), mirroring ``MemberScore``.
    ``composite == agreement_mean`` because v1 scores on agreement only (β=0).
    """

    member: str
    composite: float
    agreement_mean: float
    filtered: bool = False
    filter_reason: str | None = None
    selected: bool = False
    selection_reason: str | None = None


def score_continuous_members(agreement: pd.DataFrame) -> list[ContinuousMemberScore]:
    """Score every member by its mean pairwise Spearman vs the others (row mean).

    ``agreement`` is the symmetric pairwise-ρ matrix (diagonal = 1.0). Returns a
    list sorted by descending composite, ready for ``top_k_by_score``.
    """
    members = list(agreement.columns)
    scores: list[ContinuousMemberScore] = []
    for m in members:
        others = [c for c in members if c != m]
        am = float(agreement.loc[m, others].mean()) if others else 0.0
        scores.append(ContinuousMemberScore(member=m, composite=am, agreement_mean=am))
    scores.sort(key=lambda s: s.composite, reverse=True)
    return scores
