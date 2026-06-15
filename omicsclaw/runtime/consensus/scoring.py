"""Composite member score for base-clustering (BC) selection.

Implements the SACCELERATOR-style ``02_BC_ranking`` formula. This layer is
metric-agnostic: it takes one ``intrinsic_quality`` float per member, whatever
its source. Spatial-domains runs feed a normalized multi-metric panel
(chaos/pas/mlami, ADR 0028) as that float; sc-clustering feeds
``silhouette_score``; the panel-disabled spatial fallback is ``mean_local_purity``.

The score is deterministic; the evaluation-chair LLM may only veto members
or rebalance ``alpha``/``beta`` within ±0.2, never invent scores. See
ADR 0011 for the contract and ADR 0010 for the surrounding vocabulary.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np
from sklearn.metrics import normalized_mutual_info_score

ALPHA_DEFAULT = 0.6
BETA_DEFAULT = 0.4
MAX_CLASS_FRAC_CAP_DEFAULT = 0.8


@dataclass(frozen=True)
class MemberScore:
    """One row of the BC-ranking output.

    ``selected`` / ``selection_reason`` are populated by the driver *after* BC
    selection (scoring itself doesn't know top-K); they default to a not-yet-
    selected state so ``score_member`` callers stay unchanged.
    """

    member: str
    composite: float
    cross_nmi_mean: float
    intrinsic: float
    max_class_frac: float
    filtered: bool
    filter_reason: str | None = None
    selected: bool = False
    selection_reason: str | None = None


def _max_class_fraction(labels: np.ndarray) -> float:
    if labels.size == 0:
        return 1.0
    counts = Counter(labels.tolist())
    largest = max(counts.values())
    return largest / labels.size


def _pairwise_mean_nmi(target: np.ndarray, others: Iterable[np.ndarray]) -> float:
    """Mean pairwise NMI of ``target`` against each sibling.

    Shape mismatches raise ``ValueError`` rather than getting silently
    skipped — see ADR-0011 review I3. ``_gather_labels`` upstream uses
    inner-join semantics that guarantee equal length on the happy path,
    so a mismatch here is a real data-pipeline bug that should fail loud.
    """
    scores: list[float] = []
    for other in others:
        if other.shape != target.shape:
            raise ValueError(
                f"sibling label vector has shape {other.shape} but target has "
                f"shape {target.shape}; cannot compute pairwise NMI on misaligned data"
            )
        scores.append(normalized_mutual_info_score(target, other))
    if not scores:
        return 0.0
    return float(np.mean(scores))


def score_member(
    member: str,
    member_labels: np.ndarray,
    sibling_labels: Mapping[str, np.ndarray],
    intrinsic_quality: float,
    *,
    alpha: float = ALPHA_DEFAULT,
    beta: float = BETA_DEFAULT,
    max_class_frac_cap: float = MAX_CLASS_FRAC_CAP_DEFAULT,
) -> MemberScore:
    """Return the composite score for one member.

    Members exceeding ``max_class_frac_cap`` (default ``0.8``) on their largest
    cluster are hard-filtered: ``composite = -inf`` so any top-K selection
    automatically excludes them and ``filtered=True`` carries the reason
    into the report.

    ``sibling_labels`` should NOT include the target member itself; the caller
    is responsible for excluding it.
    """
    if intrinsic_quality is None or math.isnan(float(intrinsic_quality)):
        intrinsic_value = 0.0
        intrinsic_warning = "intrinsic_quality=NaN treated as 0.0"
    else:
        intrinsic_value = float(intrinsic_quality)
        intrinsic_warning = None

    max_frac = _max_class_fraction(member_labels)
    if max_frac > max_class_frac_cap:
        return MemberScore(
            member=member,
            composite=float("-inf"),
            cross_nmi_mean=0.0,
            intrinsic=intrinsic_value,
            max_class_frac=max_frac,
            filtered=True,
            filter_reason=f"max_class_frac={max_frac:.3f} > {max_class_frac_cap}",
        )

    cross = _pairwise_mean_nmi(member_labels, list(sibling_labels.values()))
    composite = alpha * cross + beta * intrinsic_value
    return MemberScore(
        member=member,
        composite=composite,
        cross_nmi_mean=cross,
        intrinsic=intrinsic_value,
        max_class_frac=max_frac,
        filtered=False,
        filter_reason=intrinsic_warning,
    )


def score_all_members(
    labels_by_member: Mapping[str, np.ndarray],
    intrinsic_by_member: Mapping[str, float],
    *,
    alpha: float = ALPHA_DEFAULT,
    beta: float = BETA_DEFAULT,
    max_class_frac_cap: float = MAX_CLASS_FRAC_CAP_DEFAULT,
) -> list[MemberScore]:
    """Score every member using the others as siblings.

    Returns a list sorted by descending ``composite``; filtered members
    appear last (since their score is ``-inf``).
    """
    scores: list[MemberScore] = []
    for member, labels in labels_by_member.items():
        siblings = {m: lbl for m, lbl in labels_by_member.items() if m != member}
        intrinsic = intrinsic_by_member.get(member, 0.0)
        scores.append(
            score_member(
                member=member,
                member_labels=labels,
                sibling_labels=siblings,
                intrinsic_quality=intrinsic,
                alpha=alpha,
                beta=beta,
                max_class_frac_cap=max_class_frac_cap,
            )
        )
    scores.sort(key=lambda s: s.composite, reverse=True)
    return scores


def top_k_by_score(scores: list[MemberScore], k: int) -> list[str]:
    """Pick the top-K unfiltered member names from a sorted score list."""
    return [s.member for s in scores if not s.filtered][:k]
