"""Categorical consensus operators — kmode (mode-vote) and weighted-majority.

Both share a Hungarian-alignment + frequency-relabel pre-step. After alignment
every member's labels live in the same space and per-observation voting is
well-defined.

This is the Python-equivalent of SACCELERATOR's ``Consensus_kmode.r`` and
``Consensus_weighted.r`` workflow steps. It is conceptually compatible but
not bit-exact: SACCELERATOR's kmode uses ``diceR::k_modes`` (iterative
refinement) and weighted uses EnSDD (NMF + Leiden). We pick the simpler
per-row mode / weighted-majority because (i) the LLM evaluation chair, not
the operator, is OmicsClaw's contribution, and (ii) the simpler operator is
deterministic, fast, and trivially auditable. See ADR 0011 for the rationale.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from omicsclaw.runtime.consensus.operators.alignment import align_labels


@dataclass(frozen=True)
class ConsensusResult:
    """Output of any typed-consensus operator."""

    labels: pd.Series
    aligned_labels: pd.DataFrame
    method: str
    n_clusters_returned: int
    seed: int | None


def normalize_by_frequency(labels: np.ndarray) -> np.ndarray:
    """Relabel by descending cluster frequency (most frequent → 1, 2, 3, …).

    Mirrors SACCELERATOR's "rank by frequency" step. Stable on ties (first
    occurrence wins), to keep behaviour deterministic across runs.
    """
    counts = Counter(labels.tolist())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    rank_map = {label: rank + 1 for rank, (label, _) in enumerate(ordered)}
    return np.array([rank_map[v] for v in labels])


def _pick_reference_member(labels_df: pd.DataFrame) -> str:
    """Reference = the member with the most clusters; ties → lexicographic first.

    Aligning everyone to the most-granular member preserves the maximum number
    of distinct cluster signals; coarser members fold into nearest matches via
    Hungarian.
    """
    n_clusters_per_member = {
        col: labels_df[col].nunique() for col in labels_df.columns
    }
    max_n = max(n_clusters_per_member.values())
    candidates = sorted(c for c, n in n_clusters_per_member.items() if n == max_n)
    return candidates[0]


def _align_all_to_reference(labels_df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Frequency-normalize every member then Hungarian-align to the reference."""
    normalized = pd.DataFrame(
        {col: normalize_by_frequency(labels_df[col].to_numpy()) for col in labels_df.columns},
        index=labels_df.index,
    )
    reference_col = _pick_reference_member(normalized)
    reference = normalized[reference_col].to_numpy()
    aligned_cols: dict = {reference_col: reference}
    for col in normalized.columns:
        if col == reference_col:
            continue
        aligned_cols[col] = align_labels(reference, normalized[col].to_numpy())
    aligned_df = pd.DataFrame(aligned_cols, index=labels_df.index)[list(labels_df.columns)]
    return aligned_df, reference_col


def _mode_with_tiebreak(row: np.ndarray, weights: np.ndarray | None) -> object:
    """Per-observation mode with deterministic tie-breaking.

    Ties are broken by:
      1. higher total weight (if ``weights`` is given), then
      2. the label whose column appears earliest in the row.

    The break is fully deterministic — same input always yields the same
    output, no RNG. ``ConsensusResult.seed`` on kmode/weighted is recorded
    for *traceability* only (audit logs, downstream provenance); it does
    not change the operator's behaviour. The LCA operator does honour
    seed via diceR's R-side EM initialisation.
    """
    if weights is None:
        weights = np.ones_like(row, dtype=float)
    label_score: dict = {}
    first_seen: dict = {}
    for i, label in enumerate(row):
        label_score[label] = label_score.get(label, 0.0) + float(weights[i])
        if label not in first_seen:
            first_seen[label] = i
    best_score = max(label_score.values())
    winners = [lbl for lbl, score in label_score.items() if score == best_score]
    if len(winners) == 1:
        return winners[0]
    # Two or more labels tied on score. ``first_seen`` is unique per label
    # (each row position holds exactly one label), so sorting by it gives
    # an unambiguous winner — earliest column wins.
    winners.sort(key=lambda l: first_seen[l])
    return winners[0]


def kmode_consensus(
    labels_df: pd.DataFrame,
    n_clusters: int | None = None,
    seed: int | None = None,
) -> ConsensusResult:
    """Per-observation mode of aligned member labels.

    ``n_clusters`` is informational: the operator returns however many
    distinct labels survive the mode vote, capped only when callers request
    a stricter post-merge (not implemented here). The reference choice
    bounds it at ``max(n_clusters_per_member)``.
    """
    if labels_df.shape[1] < 2:
        raise ValueError("kmode_consensus requires at least 2 members")

    aligned_df, _ = _align_all_to_reference(labels_df)
    matrix = aligned_df.to_numpy()
    consensus = np.empty(matrix.shape[0], dtype=object)
    for i in range(matrix.shape[0]):
        consensus[i] = _mode_with_tiebreak(matrix[i], None)
    labels = pd.Series(consensus, index=labels_df.index, name="consensus_kmode")
    return ConsensusResult(
        labels=labels,
        aligned_labels=aligned_df,
        method="kmode",
        n_clusters_returned=int(pd.unique(consensus).shape[0]),
        seed=seed,
    )


def weighted_consensus(
    labels_df: pd.DataFrame,
    weights: Mapping[str, float] | Sequence[float],
    n_clusters: int | None = None,
    seed: int | None = None,
) -> ConsensusResult:
    """Per-observation weighted-majority vote of aligned member labels.

    ``weights`` is keyed by member name (column header) or aligned positionally
    to ``labels_df.columns``. Negative or zero weights are allowed but will
    suppress the corresponding member.
    """
    if labels_df.shape[1] < 2:
        raise ValueError("weighted_consensus requires at least 2 members")

    if isinstance(weights, Mapping):
        try:
            weight_arr = np.array([weights[c] for c in labels_df.columns], dtype=float)
        except KeyError as exc:
            raise ValueError(f"weights missing member {exc.args[0]!r}") from exc
    else:
        if len(weights) != labels_df.shape[1]:
            raise ValueError(
                f"weights length {len(weights)} != n_members {labels_df.shape[1]}"
            )
        weight_arr = np.asarray(weights, dtype=float)

    aligned_df, _ = _align_all_to_reference(labels_df)
    matrix = aligned_df.to_numpy()
    consensus = np.empty(matrix.shape[0], dtype=object)
    for i in range(matrix.shape[0]):
        consensus[i] = _mode_with_tiebreak(matrix[i], weight_arr)
    labels = pd.Series(consensus, index=labels_df.index, name="consensus_weighted")
    return ConsensusResult(
        labels=labels,
        aligned_labels=aligned_df,
        method="weighted",
        n_clusters_returned=int(pd.unique(consensus).shape[0]),
        seed=seed,
    )
