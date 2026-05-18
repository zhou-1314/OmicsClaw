"""Unit tests for ADR 0011 composite member score."""

from __future__ import annotations

import math

import numpy as np
import pytest

from omicsclaw.runtime.consensus.scoring import (
    ALPHA_DEFAULT,
    BETA_DEFAULT,
    MAX_CLASS_FRAC_CAP_DEFAULT,
    MemberScore,
    score_all_members,
    score_member,
    top_k_by_score,
)


def _label_block(n: int, k: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(low=0, high=k, size=n)


def test_class_imbalance_hard_filter_excludes_member() -> None:
    skewed = np.array([0] * 81 + [1] * 19)  # 81% in cluster 0
    siblings = {"a": _label_block(100, 5, 1), "b": _label_block(100, 5, 2)}
    score = score_member(
        member="bad",
        member_labels=skewed,
        sibling_labels=siblings,
        intrinsic_quality=0.9,
    )
    assert score.filtered is True
    assert score.composite == float("-inf")
    assert score.max_class_frac > 0.8
    assert score.filter_reason is not None
    assert "max_class_frac" in score.filter_reason


def test_class_imbalance_exact_cap_passes() -> None:
    edge = np.array([0] * 80 + [1] * 20)  # exactly 80%
    siblings = {"a": np.array([0] * 80 + [1] * 20)}
    score = score_member(
        member="edge",
        member_labels=edge,
        sibling_labels=siblings,
        intrinsic_quality=0.5,
    )
    assert score.filtered is False
    assert math.isfinite(score.composite)


def test_composite_formula_matches_hand_computation() -> None:
    labels_a = np.array([0, 0, 1, 1, 2, 2])
    labels_b = np.array([0, 0, 1, 1, 2, 2])  # identical → NMI=1
    score = score_member(
        member="a",
        member_labels=labels_a,
        sibling_labels={"b": labels_b},
        intrinsic_quality=0.5,
        alpha=0.6,
        beta=0.4,
    )
    expected = 0.6 * 1.0 + 0.4 * 0.5
    assert abs(score.composite - expected) < 1e-9
    assert abs(score.cross_nmi_mean - 1.0) < 1e-9


def test_score_all_members_sorts_descending() -> None:
    labels = {
        "good": np.array([0, 0, 1, 1, 2, 2]),
        "okay": np.array([0, 0, 1, 1, 2, 2]),
        "weak": np.array([0, 1, 2, 0, 1, 2]),
    }
    intrinsic = {"good": 0.9, "okay": 0.7, "weak": 0.3}
    scores = score_all_members(labels, intrinsic)
    composites = [s.composite for s in scores]
    assert composites == sorted(composites, reverse=True)
    assert scores[0].member in {"good", "okay"}


def test_top_k_excludes_filtered_members() -> None:
    scores = [
        MemberScore("a", 0.9, 0.9, 0.9, 0.3, False),
        MemberScore("b", 0.7, 0.7, 0.7, 0.4, False),
        MemberScore("c", float("-inf"), 0.0, 0.5, 0.9, True, "filtered"),
        MemberScore("d", 0.5, 0.5, 0.5, 0.5, False),
    ]
    picked = top_k_by_score(scores, k=3)
    assert "c" not in picked
    assert picked == ["a", "b", "d"]


def test_defaults_match_adr_0011() -> None:
    assert ALPHA_DEFAULT == 0.6
    assert BETA_DEFAULT == 0.4
    assert MAX_CLASS_FRAC_CAP_DEFAULT == 0.8


def test_intrinsic_nan_treated_as_zero() -> None:
    labels = np.array([0, 0, 1, 1])
    siblings = {"sib": np.array([0, 1, 0, 1])}
    score = score_member(
        member="x",
        member_labels=labels,
        sibling_labels=siblings,
        intrinsic_quality=float("nan"),
    )
    assert score.intrinsic == 0.0
    assert score.filter_reason is not None
    assert "NaN" in score.filter_reason


def test_alpha_beta_override_works() -> None:
    labels_a = np.array([0, 0, 1, 1])
    labels_b = np.array([0, 0, 1, 1])
    s = score_member(
        member="a",
        member_labels=labels_a,
        sibling_labels={"b": labels_b},
        intrinsic_quality=0.4,
        alpha=0.2,
        beta=0.8,
    )
    expected = 0.2 * 1.0 + 0.8 * 0.4
    assert abs(s.composite - expected) < 1e-9


def test_score_member_raises_on_mismatched_sibling_shape() -> None:
    """I3 regression: a sibling label vector of a different length is
    almost always a data-pipeline bug (e.g. members fanned out on different
    inputs). Silently dropping it from cross-NMI computation hides the bug.
    Must raise so the caller fixes the actual problem.
    """
    target = np.array([0, 0, 1, 1, 2])
    siblings = {
        "ok": np.array([0, 0, 1, 1, 2]),
        "wrong_length": np.array([0, 1, 0]),  # mismatched
    }
    with pytest.raises(ValueError, match="shape"):
        score_member(
            member="x",
            member_labels=target,
            sibling_labels=siblings,
            intrinsic_quality=0.5,
        )


@pytest.mark.parametrize("k,expected_count", [(0, 0), (1, 1), (2, 2), (5, 3)])
def test_top_k_caps_at_unfiltered_count(k: int, expected_count: int) -> None:
    scores = [
        MemberScore("a", 0.9, 0.9, 0.9, 0.3, False),
        MemberScore("b", 0.7, 0.7, 0.7, 0.4, False),
        MemberScore("c", 0.5, 0.5, 0.5, 0.5, False),
    ]
    picked = top_k_by_score(scores, k=k)
    assert len(picked) == expected_count
