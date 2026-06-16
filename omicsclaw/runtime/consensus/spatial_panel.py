"""Multi-metric spatial intrinsic-quality panel (consensus L2).

A single spatial-coherence number (``mean_local_purity``) judges a domain
clustering from only one angle. This module combines several **unsupervised**
(labels + spatial coords only) spatial-domain quality metrics into one
comparable ``[0, 1]`` intrinsic scalar for the BC-ranking ``beta`` term, plus the
per-metric breakdown for the report.

Panel (all from ``spatial_metrics.py``, no ground truth required):

- ``chaos``  — mean fraction of k spatial neighbours sharing a spot's label
  (1-hop coherence). Higher is better.
- ``pas``    — fraction of "abnormal" spots whose label disagrees with most of
  their neighbours (anomaly rate). **Lower** is better.
- ``mlami``  — max AMI of the labels against a spatial-graph Leiden sweep
  (multi-scale structure). Higher is better.

Comparability (the crux): each metric is direction-aligned (so higher = better)
and mapped to ``[0, 1]`` by its **theoretical** range — never a data-snooped or
hallucinated threshold — then combined as a weighted mean. ``chaos``/``mlami``
are already in ``[0, 1]`` and higher-better; ``pas`` is flipped (``1 - pas``);
``mlami`` (an AMI) is clipped at ``0`` so a worse-than-chance clustering earns no
credit. Weights are explicit knobs (like ADR 0011's ``alpha``/``beta``), not
derived from the data.

Determinism: ``mlami`` runs a seeded Leiden sweep. Fail-soft: any metric that
raises is dropped and the weights renormalise over the survivors; if none
compute, the panel scalar is ``0.0``.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

#: Canonical metric order — the stable column order for ``member_intrinsic_panel.csv``
#: and the compute order, decoupled from the weights dict so reweighting can't
#: silently reorder the artifact.
PANEL_METRICS: tuple[str, ...] = ("chaos", "pas", "mlami")

#: Default panel weights. Topology-biased: ``chaos`` (1-hop) and ``mlami``
#: (multi-scale) are the two strong, orthogonal coherence axes; ``pas`` is an
#: anomaly-rate refinement. Tunable like ADR 0011's alpha/beta.
DEFAULT_PANEL_WEIGHTS: dict[str, float] = {"chaos": 0.4, "pas": 0.2, "mlami": 0.4}

#: Metrics whose raw value is "lower = better" and must be flipped on normalise.
_LOWER_BETTER = frozenset({"pas"})


def _normalise_one(name: str, raw: float) -> float:
    """Direction-align + clip one raw metric value into ``[0, 1]`` (higher=better).

    All current panel metrics are theoretically bounded in ``[0, 1]`` once an
    AMI (``mlami``) is clipped at 0, so the map is a plain clip (+ a flip for
    lower-better metrics). No data-derived min/max is used.
    """
    v = float(np.clip(raw, 0.0, 1.0))
    return 1.0 - v if name in _LOWER_BETTER else v


def combine_panel(
    raw: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> float:
    """Combine per-metric raw values into one ``[0, 1]`` intrinsic scalar.

    Only finite metrics present in both ``raw`` and the weights contribute; the
    weights renormalise over those present, so a dropped (failed) metric does not
    bias the result. Returns ``0.0`` when nothing usable is present.
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


def intrinsic_spatial_panel(
    labels: np.ndarray,
    coords: np.ndarray,
    *,
    weights: Mapping[str, float] | None = None,
    k: int = 10,
    seed: int = 0,
) -> tuple[float, dict[str, float]]:
    """Compute the spatial intrinsic panel for one member.

    Parameters
    ----------
    labels :
        Per-observation cluster labels, shape ``(n_obs,)``.
    coords :
        Per-observation spatial coordinates, shape ``(n_obs, n_dim)``.
    weights :
        Per-metric weights; defaults to :data:`DEFAULT_PANEL_WEIGHTS`.
    k :
        Spatial-neighbour count for ``chaos``/``pas``.
    seed :
        Forwarded to ``mlami``'s Leiden sweep for reproducibility.

    Returns
    -------
    (scalar, raw) :
        ``scalar`` is the combined intrinsic in ``[0, 1]``; ``raw`` maps each
        successfully-computed metric name to its raw value (for the report).
        Metrics that raise are omitted from ``raw`` and the combination.
    """
    from omicsclaw.runtime.consensus import spatial_metrics

    labels = np.asarray(labels)
    coords = np.asarray(coords)

    metric_calls = {
        "chaos": lambda: spatial_metrics.chaos(labels, coords, k=k),
        "pas": lambda: spatial_metrics.pas(labels, coords, k=k),
        "mlami": lambda: spatial_metrics.mlami(labels, coords, seed=seed),
    }
    raw: dict[str, float] = {}
    for name, call in metric_calls.items():
        try:
            raw[name] = float(call())
        except Exception:  # noqa: BLE001 — fail-soft: drop a metric, keep the panel
            continue

    return combine_panel(raw, weights), raw
