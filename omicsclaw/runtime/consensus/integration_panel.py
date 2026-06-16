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
labels + coords.

Calibration (ADR 0029, revised after panc8 real-data validation). The **single
scored axis** is batch mixing:

- ``ilisi_norm``  — iLISI batch-neighbourhood diversity, mapped to ``[0, 1]`` by
  its theoretical range ``[1, n_batches]`` as ``log(iLISI) / log(n_batches)``.
  Higher = better mixing. **Undefined for a single batch** (the panel is then
  not applicable). The log map (vs the linear ``(iLISI-1)/(n_batches-1)``) gives
  real-world iLISI — which sits near 1 — usable dynamic range so good and poor
  integrations are actually distinguishable (B3).

The other three metrics are **computed and reported but carry weight 0** (pure
diagnostics, never part of the score):

- ``knn_preservation_norm`` — fraction of each cell's *within-batch* ``X_pca``
  nearest neighbours retained in the member's integrated embedding. It was
  *intended* as a bio-structure / over-integration probe, but on panc8 (5
  technologies, ground-truth cell types) it **anti-correlated** with cell-type
  recovery (Spearman ``r=-0.74`` vs ARI) while ``ilisi`` correlated (``r=+0.99``):
  a method that legitimately reorganises the embedding to merge cell types across
  batches lowers within-batch ``X_pca`` neighbour overlap (those neighbourhoods
  carry technical variation, not only biology), so the metric penalises the best
  integrator. Demoted to a diagnostic until a *validated* GT-free structure
  metric exists (graph connectivity, deferred). It still flags over-integration
  *in the report* — it just no longer drives selection (B1).
- ``batch_asw_norm``    — ``1 - |silhouette(embedding, batch)|`` (batch ASW near
  0 is ideal); a second, weaker mixing signal — diagnostic only.
- ``cluster_asw_norm``  — ``(silhouette(embedding, cluster_labels) + 1) / 2``
  (label compactness; circular — labels came from this embedding's graph).

Comparability (the crux): every metric is direction-aligned (higher = better)
and mapped to ``[0, 1]`` by its **theoretical** range — never a data-snooped or
hallucinated threshold — then combined as a weighted mean. The weight is an
explicit knob recorded in ``plan.json`` and the report. ``ilisi`` is the one
axis validated against ground truth (panc8); treat the score as a *relative
mixing rank* and read ``knn_preservation`` alongside it to catch over-integration.

Determinism + fail-soft: every metric is pure given its inputs (no RNG). Any
metric that raises is dropped and the weights renormalise over the survivors;
if none compute, the panel scalar is ``0.0``.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

#: Canonical metric order — the stable column order for
#: ``member_intrinsic_panel.csv`` and the compute order, decoupled from the
#: weights dict so reweighting can't silently reorder the artifact. Only the
#: first (``ilisi_norm``) is scored; the last three are weight-0 diagnostics that
#: appear in the CSV but not the score.
PANEL_METRICS: tuple[str, ...] = (
    "ilisi_norm",
    "knn_preservation_norm",
    "batch_asw_norm",
    "cluster_asw_norm",
)

#: Default panel weights (ADR 0029, revised after panc8 real-data validation).
#: ``ilisi_norm`` is the **single scored axis** — the one metric validated to
#: track ground-truth cell-type recovery (Spearman ``r=+0.99`` vs ARI on panc8).
#: ``knn_preservation``/``batch_asw``/``cluster_asw`` are weight-0 diagnostics:
#: ``knn_preservation`` *anti*-correlated with recovery (``r=-0.74``), so it must
#: not drive selection (it is reported to flag over-integration, not to score).
#: A validated GT-free structure axis (graph connectivity) is deferred.
DEFAULT_PANEL_WEIGHTS: dict[str, float] = {
    "ilisi_norm": 1.0,
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

def _scale_ilisi(mean_ilisi: float, n_batches: int) -> float:
    """Map mean iLISI in ``[1, n_batches]`` to ``[0, 1]`` via ``log/log``.

    Pure (no heavy deps) so the normalization can be unit-tested without
    harmonypy. ``log(mean_iLISI)/log(n_batches)`` is preferred over the linear
    ``(iLISI-1)/(n_batches-1)`` because real-world iLISI clusters near 1
    (e.g. ~1.5/5 for a decent integration), so the linear map compresses every
    method into the bottom of ``[0, 1]`` and barely separates good from poor
    integration (B3); the log map restores usable dynamic range. It is strictly
    monotone in iLISI, so it changes spacing, not member ranking. Endpoints:
    ``iLISI=1 → 0``, ``iLISI=n_batches → 1``. Raises ``ValueError`` for a single
    batch (iLISI is undefined). The input is clamped to ``[1, n_batches]`` first.
    """
    import math

    if n_batches < 2:
        raise ValueError("iLISI undefined for a single batch")
    v = min(max(float(mean_ilisi), 1.0), float(n_batches))
    return float(np.clip(math.log(v) / math.log(n_batches), 0.0, 1.0))


def _ilisi_norm(embedding: np.ndarray, batch_labels: np.ndarray, perplexity: float) -> float:
    """Normalised iLISI: ``log(mean_iLISI) / log(n_batches)`` in ``[0, 1]``.

    Computes mean iLISI via harmonypy and rescales with :func:`_scale_ilisi`.
    Raises ``ValueError`` for a single batch so the metric is dropped fail-soft
    rather than scoring a no-op.
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
    return _scale_ilisi(float(np.mean(ilisi[:, 0])), n_batches)


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
