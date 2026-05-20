"""Marker → cell-type candidate ranking (deterministic, pre-LLM).

This stage bounds LLM hallucination at the *input* level. For each
consensus cluster, the DE top-K markers are joined against the marker
DB; per-cell-type scores accumulate as ``Σ db.weight × 1/de_rank``.
The LLM later picks from this short ranked list — it does not invent
cell types from training data.

Returned ``RankedCandidate.supporting_markers`` carries the verbatim
``(gene, de_rank, weight)`` triples so Slice 5 LLM prompts can cite
specific evidence (one half of the ``marker_grounding`` invariant per
ADR 0012 §"T3 invariants").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class SupportingMarker:
    """One ``(gene, de_rank, weight)`` triple supporting a cell-type
    candidate. Slice 5 emits these into the LLM prompt; Slice 6 grep
    test asserts the LLM's response references at least one of them."""

    gene: str
    de_rank: int
    weight: float


@dataclass(frozen=True)
class RankedCandidate:
    """Per-cluster, per-cell-type aggregated candidate."""

    cell_type: str
    score: float
    supporting_markers: list[SupportingMarker] = field(default_factory=list)


@runtime_checkable
class _MarkerDBLike(Protocol):
    """Duck type — accept anything exposing ``candidates(gene)``."""

    def candidates(self, gene: str) -> list: ...  # noqa: E704


def rank_celltype_candidates(
    de_df: pd.DataFrame,
    marker_db: _MarkerDBLike,
    *,
    top_k: int = 5,
) -> dict[int, list[RankedCandidate]]:
    """Rank cell-type candidates per cluster by ``Σ weight / de_rank``.

    Returns
    -------
    dict[cluster_id, list[RankedCandidate]]
        Per-cluster ranked list, descending score. Ties broken by
        alphabetical ``cell_type``. Empty list for a cluster means
        no top-K marker matched any DB entry (T2 signal).
    """
    if de_df.empty:
        return {}

    out: dict[int, list[RankedCandidate]] = {}

    for cluster_id, sub in de_df.groupby("cluster"):
        # cell_type -> {"score": float, "markers": list[SupportingMarker]}
        accum: dict[str, dict] = {}
        # Iterate by rank ascending so supporting_markers stays sorted
        for _, row in sub.sort_values("rank").iterrows():
            gene = str(row["gene"])
            de_rank = int(row["rank"])
            for cand in marker_db.candidates(gene):
                cell_type = cand.cell_type
                weight = float(cand.weight)
                bucket = accum.setdefault(
                    cell_type, {"score": 0.0, "markers": []}
                )
                bucket["score"] += weight / de_rank
                bucket["markers"].append(
                    SupportingMarker(gene=gene, de_rank=de_rank, weight=weight)
                )

        # Sort: score desc, cell_type asc (deterministic tie-break)
        ranked = sorted(
            (
                RankedCandidate(
                    cell_type=ct,
                    score=v["score"],
                    supporting_markers=list(v["markers"]),
                )
                for ct, v in accum.items()
            ),
            key=lambda r: (-r.score, r.cell_type),
        )
        out[int(cluster_id)] = ranked[:top_k]

    return out
