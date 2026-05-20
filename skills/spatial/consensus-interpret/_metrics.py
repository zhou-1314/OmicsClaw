"""4-axis evaluation panel per ADR 0012 §"4-axis evaluation panel".

Axis 1 — interpretation_faithfulness (invariant + soft regression metric;
         floor 1.00; default-CI always-run).
Axis 2 — marker_grounding_rate (Jaccard of LLM-claimed top-K markers
         vs DE-derived top-K markers; floor 0.60; stubbed default-CI,
         real-LLM gated).
Axis 3 — interpret_self_consistency (majority agreement across 3 LLM
         seeds; floor 0.70; env-gated by RUN_INTERPRET_CONSISTENCY).
Axis 4 — expert_concordance_hero (DLPFC 151673 ARI of mapped cluster
         labels vs Maynard et al. 2021 GT layers; floor 0.45;
         env-gated by RUN_INTERPRET_DLPFC).
"""

from __future__ import annotations

import re
from typing import Mapping, TYPE_CHECKING

import pandas as pd
from sklearn.metrics import adjusted_rand_score

if TYPE_CHECKING:
    from _llm import ClusterAnnotation


# ADR 0012 §"Pass rule" floors. Surfaced as constants so CI tests and
# consensus-interpret main() can reference one source of truth.
FAITHFULNESS_FLOOR: float = 1.00
MARKER_GROUNDING_FLOOR: float = 0.60
SELF_CONSISTENCY_FLOOR: float = 0.70
EXPERT_CONCORDANCE_FLOOR: float = 0.45


# --------------------------------------------------------------------------- #
# Axis 1 — interpretation_faithfulness                                        #
# --------------------------------------------------------------------------- #

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def compute_faithfulness(report_body: str, citation_pool: set[str]) -> float:
    """Fraction of sentences in ``report_body`` that contain at least one
    literal occurrence of an item from ``citation_pool``.

    ``citation_pool`` is the set of verbatim values from the typed run
    (cluster ids, NMI values, marker names, p-values, member names…)
    that any faithful narrative MUST cite.
    """
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(report_body.strip()) if s.strip()]
    if not sentences:
        return 0.0
    if not citation_pool:
        return 0.0

    cited = 0
    for s in sentences:
        if any(token in s for token in citation_pool):
            cited += 1
    return cited / len(sentences)


# --------------------------------------------------------------------------- #
# Axis 2 — marker_grounding_rate                                              #
# --------------------------------------------------------------------------- #

def compute_marker_grounding_rate(
    annotations: "list[ClusterAnnotation]",
    de_df: pd.DataFrame,
    *,
    top_k: int = 20,
) -> float:
    """For each interpreted cluster, |LLM markers ∩ DE top-K| / |LLM markers|.

    Returns the mean over clusters with ``cell_type != "Unknown"``.
    """
    interpreted = [a for a in annotations if a.cell_type != "Unknown"]
    if not interpreted:
        return 0.0

    de_by_cluster: dict[int, set[str]] = {}
    if not de_df.empty:
        for c, sub in de_df.groupby("cluster"):
            top = sub.sort_values("rank").head(top_k)
            de_by_cluster[int(c)] = set(top["gene"].astype(str))

    rates: list[float] = []
    for a in interpreted:
        llm_set = {m.gene for m in a.evidence_markers}
        if not llm_set:
            rates.append(0.0)
            continue
        de_set = de_by_cluster.get(a.cluster_id, set())
        overlap = len(llm_set & de_set)
        rates.append(overlap / len(llm_set))

    return sum(rates) / len(rates)


# --------------------------------------------------------------------------- #
# Axis 3 — interpret_self_consistency                                         #
# --------------------------------------------------------------------------- #

def compute_self_consistency(seed_assignments: list[Mapping[int, str]]) -> float:
    """Fraction of clusters where the majority of seeds agree on cell_type.

    Each ``seed_assignments[i]`` is ``cluster_id -> cell_type`` from one
    LLM seed/temperature. Clusters absent from any seed are skipped.
    """
    if len(seed_assignments) < 2:
        return 1.0  # trivially consistent

    all_clusters: set[int] = set()
    for a in seed_assignments:
        all_clusters.update(a.keys())
    if not all_clusters:
        return 0.0

    agreements = 0
    n = 0
    for cid in all_clusters:
        votes: dict[str, int] = {}
        for a in seed_assignments:
            if cid not in a:
                continue
            votes[a[cid]] = votes.get(a[cid], 0) + 1
        if not votes:
            continue
        n += 1
        top_count = max(votes.values())
        if top_count > len(seed_assignments) / 2:
            agreements += 1

    return agreements / n if n else 0.0


# --------------------------------------------------------------------------- #
# Axis 4 — expert_concordance_hero (DLPFC ARI)                                 #
# --------------------------------------------------------------------------- #

def compute_expert_concordance_ari(
    consensus_labels: pd.DataFrame,
    cluster_to_celltype: Mapping[int, str],
    gt_per_obs: pd.Series,
) -> float:
    """ARI between (consensus cluster -> mapped cell_type) and per-observation GT.

    Parameters
    ----------
    consensus_labels
        DataFrame with ``observation`` + ``consensus_<operator>`` columns
        (from a typed run).
    cluster_to_celltype
        ``cluster_id -> cell_type`` mapping the LLM produced.
    gt_per_obs
        ``observation -> GT cell type / layer`` from the hero benchmark
        ground truth (e.g. Maynard et al. 2021 DLPFC layer labels).

    Both label vectors are reduced to integers, then sklearn's ARI is
    computed on the aligned subset.
    """
    label_cols = [c for c in consensus_labels.columns if c.startswith("consensus_")]
    if not label_cols:
        raise KeyError("consensus_labels has no consensus_<operator> column")
    consensus_col = label_cols[0]

    pred_per_obs = (
        consensus_labels.set_index("observation")[consensus_col]
        .map(cluster_to_celltype)
        .dropna()
    )
    common = pred_per_obs.index.intersection(gt_per_obs.index)
    if not len(common):
        return 0.0

    pred = pred_per_obs.loc[common].astype("category").cat.codes.to_numpy()
    truth = gt_per_obs.loc[common].astype("category").cat.codes.to_numpy()
    return float(adjusted_rand_score(truth, pred))
