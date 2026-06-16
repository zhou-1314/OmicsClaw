"""Multi-metric integration intrinsic-quality panel (consensus L2).

The single-cell analog of :mod:`spatial_panel` (ADR 0028). For an
``sc-consensus-integration`` run the members are clusterings on different
batch-correction representations (unintegrated ``X_pca`` baseline, Harmony,
Scanorama, scVI, ...). A good member integrates *across* batches while
preserving *within*-batch biological structure — so a single number cannot
judge it. This module combines several **unsupervised** (no ground-truth
cell-type labels — only the clustering, the embedding, and the batch key)
metrics into one comparable ``[0, 1]`` intrinsic scalar for the BC-ranking
``beta`` term, plus the per-metric breakdown for the report.

Why no ground-truth labels: at consensus voting time we have batch labels but
not curated cell types, so scIB-style bio-conservation metrics (cLISI,
NMI/ARI-vs-truth) are unavailable — exactly as the spatial panel uses only
labels + coords. The two **scored** axes are therefore:

- ``ilisi_norm``            — iLISI batch-neighbourhood diversity, mapped to
  ``[0, 1]`` by its theoretical range ``[1, n_batches]`` as
  ``(iLISI - 1) / (n_batches - 1)``. Higher = better mixing. **Undefined for a
  single batch** (the panel is then not applicable).
- ``knn_preservation_norm`` — fraction of each cell's *within-batch* ``X_pca``
  nearest neighbours that remain neighbours in the member's integrated
  embedding. Within-batch by construction, so it is a clean bio-structure
  signal (same batch ⇒ no batch effect between the cells) and a direct
  over-integration probe: a method that mashes everything together destroys
  within-batch neighbourhoods. Reference is the external ``X_pca``, not the
  member's own labels, so it is **not circular** (unlike a same-label
  silhouette). Higher = better.

Two further metrics are **computed and reported but carry weight 0** (pure
diagnostics, never part of the score) — a label-vs-embedding silhouette is
partly self-fulfilling (the labels came from that embedding's graph) and a
batch silhouette double-counts mixing, so neither should drive selection:

- ``batch_asw_norm``    — ``1 - |silhouette(embedding, batch)|`` (batch ASW
  near 0 is ideal; both strong separation and pathological anti-clustering are
  penalised).
- ``cluster_asw_norm``  — ``(silhouette(embedding, cluster_labels) + 1) / 2``
  (label compactness; reported only — circular, see above).

Comparability (the crux): every metric is direction-aligned (higher = better)
and mapped to ``[0, 1]`` by its **theoretical** range — never a data-snooped or
hallucinated threshold — then combined as a weighted mean. Weights are explicit
knobs (like ADR 0011's ``alpha``/``beta``) and are **experimental** — they are
recorded in ``plan.json`` and the report, not presented as validated.

Determinism + fail-soft: every metric is pure given its inputs (no RNG). Any
metric that raises is dropped and the weights renormalise over the survivors;
if none compute, the panel scalar is ``0.0``.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

#: Canonical metric order — the stable column order for
#: ``member_intrinsic_panel.csv`` and the compute order, decoupled from the
#: weights dict so reweighting can't silently reorder the artifact. The last two
#: are diagnostics (default weight 0); they appear in the CSV but not the score.
PANEL_METRICS: tuple[str, ...] = (
    "ilisi_norm",
    "knn_preservation_norm",
    "batch_asw_norm",
    "cluster_asw_norm",
)

#: Default panel weights — **experimental**, not empirically calibrated (ADR
#: 0029). Two orthogonal scored axes: batch mixing (``ilisi``) balanced against
#: within-batch structure preservation (``knn_preservation``), so that both
#: over-integration (mixing high, preservation low) and under-integration
#: (preservation high, mixing low) are penalised. ``batch_asw``/``cluster_asw``
#: are diagnostics with weight 0.
DEFAULT_PANEL_WEIGHTS: dict[str, float] = {
    "ilisi_norm": 0.5,
    "knn_preservation_norm": 0.5,
}

#: Metrics whose raw value is "lower = better" and must be flipped on normalise.
#: All integration-panel metrics are pre-normalised higher-better, so empty.
_LOWER_BETTER: frozenset[str] = frozenset()


def _normalise_one(name: str, raw: float) -> float:
    """Direction-align + clip one metric value into ``[0, 1]`` (higher=better).

    Integration-panel metrics are already mapped into ``[0, 1]`` higher-better
    by their compute step, so this is a plain clip (kept structurally parallel
    to :func:`spatial_panel._normalise_one`). No data-derived min/max is used.
    """
    v = float(np.clip(raw, 0.0, 1.0))
    return 1.0 - v if name in _LOWER_BETTER else v


def combine_panel(
    raw: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> float:
    """Combine per-metric values into one ``[0, 1]`` intrinsic scalar.

    Only finite metrics present in both ``raw`` and the weights with a positive
    weight contribute; the weights renormalise over those present, so a dropped
    (failed) or diagnostic-only (weight 0) metric does not bias the result.
    Returns ``0.0`` when nothing usable is present.
    """
    w = dict(weights or DEFAULT_PANEL_WEIGHTS)
    contributions: list[tuple[float, float]] = []  # (weight, normalised value)
    for name, weight in w.items():
        value = raw.get(name)
        if value is None or not np.isfinite(value) or weight <= 0:
            continue
        contributions.append((weight, _normalise_one(name, float(value))))
    total = sum(weight for weight, _ in contributions)
    if total <= 0:
        return 0.0
    combined = sum(weight * value for weight, value in contributions) / total
    return float(np.clip(combined, 0.0, 1.0))


# --------------------------------------------------------------------------- #
# Per-metric kernels (pure; lazy heavy imports; raise on bad input)           #
# --------------------------------------------------------------------------- #

def _ilisi_norm(embedding: np.ndarray, batch_labels: np.ndarray, perplexity: float) -> float:
    """Normalised iLISI: ``(mean_iLISI - 1) / (n_batches - 1)`` in ``[0, 1]``.

    Raises ``ValueError`` for a single batch (integration is not assessable)
    so the metric is dropped fail-soft rather than scoring a no-op as good.
    """
    import pandas as pd
    from harmonypy import compute_lisi

    n_batches = int(np.unique(batch_labels).size)
    if n_batches < 2:
        raise ValueError("iLISI undefined for a single batch")
    embedding = np.asarray(embedding, dtype=float)
    n = embedding.shape[0]
    # harmonypy uses ``n_neighbors = perplexity * 3`` and sklearn requires an
    # int <= n_samples, so coerce to int and cap perplexity below n/3.
    perp = int(min(int(perplexity), max(1, (n - 1) // 3)))
    metadata = pd.DataFrame({"batch": np.asarray(batch_labels).astype(str)})
    ilisi = compute_lisi(embedding, metadata, ["batch"], perplexity=perp)
    mean_ilisi = float(np.mean(ilisi[:, 0]))
    return float(np.clip((mean_ilisi - 1.0) / (n_batches - 1.0), 0.0, 1.0))


def _knn_preservation_norm(
    embedding: np.ndarray, x_pca: np.ndarray, batch_labels: np.ndarray, k: int
) -> float:
    """Mean fraction of each cell's within-batch ``X_pca`` kNN retained in ``embedding``.

    Computed per batch on the batch submatrix, so neighbours are within-batch by
    construction (no batch effect between them). The reference is the external
    ``X_pca`` baseline, not the member's own labels — non-circular. ``[0, 1]``,
    higher = more within-batch structure preserved.
    """
    from sklearn.neighbors import NearestNeighbors

    embedding = np.asarray(embedding, dtype=float)
    x_pca = np.asarray(x_pca, dtype=float)
    batch_labels = np.asarray(batch_labels)
    if embedding.shape[0] != x_pca.shape[0] or embedding.shape[0] != batch_labels.shape[0]:
        raise ValueError("embedding, x_pca and batch_labels must share n_obs")

    per_cell: list[float] = []
    for batch in np.unique(batch_labels):
        mask = batch_labels == batch
        n_b = int(mask.sum())
        if n_b < 3:
            continue  # too few cells in this batch to define a neighbourhood
        kk = min(k, n_b - 1)
        pca_idx = NearestNeighbors(n_neighbors=kk + 1).fit(x_pca[mask]).kneighbors(
            x_pca[mask], return_distance=False
        )[:, 1:]
        emb_idx = NearestNeighbors(n_neighbors=kk + 1).fit(embedding[mask]).kneighbors(
            embedding[mask], return_distance=False
        )[:, 1:]
        for a, b in zip(pca_idx, emb_idx):
            per_cell.append(len(np.intersect1d(a, b, assume_unique=False)) / float(kk))
    if not per_cell:
        raise ValueError("no batch had enough cells for kNN preservation")
    return float(np.clip(np.mean(per_cell), 0.0, 1.0))


def _batch_asw_norm(embedding: np.ndarray, batch_labels: np.ndarray) -> float:
    """``1 - |silhouette(embedding, batch)|`` — batch ASW near 0 is ideal. ``[0, 1]``."""
    from sklearn.metrics import silhouette_score

    batch_labels = np.asarray(batch_labels).astype(str)
    if np.unique(batch_labels).size < 2:
        raise ValueError("batch ASW undefined for a single batch")
    asw = float(silhouette_score(np.asarray(embedding, dtype=float), batch_labels))
    return float(np.clip(1.0 - abs(asw), 0.0, 1.0))


def _cluster_asw_norm(embedding: np.ndarray, cluster_labels: np.ndarray) -> float:
    """``(silhouette(embedding, clusters) + 1) / 2`` — label compactness. ``[0, 1]``.

    Diagnostic only (weight 0): partly circular because the labels were derived
    from this embedding's graph.
    """
    from sklearn.metrics import silhouette_score

    cluster_labels = np.asarray(cluster_labels).astype(str)
    if np.unique(cluster_labels).size < 2:
        raise ValueError("cluster ASW undefined for a single cluster")
    asw = float(silhouette_score(np.asarray(embedding, dtype=float), cluster_labels))
    return float(np.clip((asw + 1.0) / 2.0, 0.0, 1.0))


def intrinsic_integration_panel(
    cluster_labels: np.ndarray,
    embedding: np.ndarray,
    batch_labels: np.ndarray,
    x_pca: np.ndarray,
    *,
    weights: Mapping[str, float] | None = None,
    k: int = 15,
    perplexity: float = 30.0,
    seed: int = 0,  # noqa: ARG001 — accepted for driver-call symmetry; no RNG here
) -> tuple[float, dict[str, float]]:
    """Compute the integration intrinsic panel for one member.

    Parameters
    ----------
    cluster_labels :
        Per-cell cluster labels the member produced, shape ``(n_obs,)``.
    embedding :
        The representation the member clustered on (e.g. ``X_harmony``), shape
        ``(n_obs, n_dim)``. For the unintegrated baseline this is ``X_pca``.
    batch_labels :
        Per-cell batch ids, shape ``(n_obs,)``.
    x_pca :
        Pre-integration ``X_pca`` baseline, shape ``(n_obs, n_pca)`` — the
        external reference for within-batch structure preservation.
    weights :
        Per-metric weights; defaults to :data:`DEFAULT_PANEL_WEIGHTS`.
    k :
        Neighbour count for ``knn_preservation``.
    perplexity :
        Local neighbourhood size forwarded to harmonypy's ``compute_lisi``.

    Returns
    -------
    (scalar, raw) :
        ``scalar`` is the combined intrinsic in ``[0, 1]``; ``raw`` maps each
        successfully-computed metric name to its normalised value (for the
        report). Metrics that raise are omitted from ``raw`` and the
        combination (fail-soft).
    """
    cluster_labels = np.asarray(cluster_labels)
    embedding = np.asarray(embedding)
    batch_labels = np.asarray(batch_labels)
    x_pca = np.asarray(x_pca)

    metric_calls = {
        "ilisi_norm": lambda: _ilisi_norm(embedding, batch_labels, perplexity),
        "knn_preservation_norm": lambda: _knn_preservation_norm(embedding, x_pca, batch_labels, k),
        "batch_asw_norm": lambda: _batch_asw_norm(embedding, batch_labels),
        "cluster_asw_norm": lambda: _cluster_asw_norm(embedding, cluster_labels),
    }
    raw: dict[str, float] = {}
    for name in PANEL_METRICS:
        try:
            raw[name] = float(metric_calls[name]())
        except Exception:  # noqa: BLE001 — fail-soft: drop a metric, keep the panel
            continue

    return combine_panel(raw, weights), raw
