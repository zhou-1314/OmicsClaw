"""Spatial-aware clustering metrics — MLAMI, CHAOS, PAS.

Pure Python ports of three established metrics from the spatial omics
community. Used by the consensus runtime (and DLPFC hero benchmark) to
augment the agreement-with-GT panel (ARI / AMI / V-measure) with signals
that actually probe spatial structure.

Attribution
-----------
- ``mlami``: adapted from nichecompass v1.x
  (``src/nichecompass/benchmarking/mlami.py``,
  Sebastian Birk · Carlos Talavera-López · Mohammad Lotfollahi,
  BSD 3-Clause Copyright 2024). Simplified for the OmicsClaw use case:
  the input is the final consensus *cluster labels* directly, not a
  learned latent — so the latent-side Leiden sweep is dropped and the
  function returns ``max AMI(labels, spatial-Leiden(res_i))`` over a
  small resolution sweep on the spatial k-NN graph.
- ``chaos`` / ``pas``: Python equivalents of SACCELERATOR
  (``consensus/02_Smoothness_entropy``-family scripts, MIT-0). One-hop
  spatial label-agreement aggregates.

LICENSE notes
-------------
nichecompass is BSD 3-Clause — redistribution requires preserving the
copyright notice + disclaimer. OmicsClaw is Apache 2.0, which is
compatible inbound. SACCELERATOR is MIT-0 (no attribution required, but
preserved here as a courtesy). Full BSD-3 copyright + disclaimer is
reproduced below per the redistribution clause.

::

  Copyright (c) 2024, Sebastian Birk, Carlos Talavera-López, Mohammad Lotfollahi
  All rights reserved.

  Redistribution and use in source and binary forms, with or without
  modification, are permitted provided that the following conditions are met:

  1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.
  2. Redistributions in binary form must reproduce the above copyright notice,
     this list of conditions and the following disclaimer in the documentation
     and/or other materials provided with the distribution.
  3. Neither the name of the copyright holder nor the names of its contributors
     may be used to endorse or promote products derived from this software
     without specific prior written permission.

  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
  POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _validate(labels: np.ndarray, coords: np.ndarray) -> None:
    if labels.shape[0] != coords.shape[0]:
        raise ValueError(
            f"labels has length {labels.shape[0]} but coords has {coords.shape[0]} rows; "
            "labels[i] must describe coords[i]"
        )
    if coords.ndim != 2:
        raise ValueError(f"coords must be 2-D (n_obs, n_spatial_dim); got shape {coords.shape}")


def _spatial_knn_indices(coords: np.ndarray, k: int) -> np.ndarray:
    """Return ``(n_obs, k)`` index array of the k nearest *non-self* spatial neighbors."""
    from sklearn.neighbors import NearestNeighbors

    n = coords.shape[0]
    knn = NearestNeighbors(n_neighbors=min(k + 1, n)).fit(coords)
    _, idx = knn.kneighbors(coords)
    # Column 0 is the spot itself (distance 0); drop it.
    return idx[:, 1 : k + 1]


def chaos(labels: np.ndarray, coords: np.ndarray, k: int = 10) -> float:
    """Mean fraction of k spatial neighbors that share a spot's label.

    Higher = clusters are spatially coherent. ``1.0`` means every spot's
    label matches every spatial neighbor; uniform random labels with
    ``C`` clusters approach ``1/C``.

    Parameters
    ----------
    labels :
        Per-observation cluster label array, shape ``(n_obs,)``. Any dtype
        with equality semantics works (int, str, np.object_).
    coords :
        Per-observation spatial coordinates, shape ``(n_obs, n_dim)``.
        Typically 2-D (Visium / Xenium) but n_dim is not constrained.
    k :
        Number of spatial neighbors to consider (excluding self).

    Returns
    -------
    chaos :
        Mean spatial label-agreement, in ``[0, 1]``.
    """
    labels = np.asarray(labels)
    coords = np.asarray(coords)
    _validate(labels, coords)
    idx = _spatial_knn_indices(coords, k)
    neighbor_labels = labels[idx]  # shape (n, k)
    same = (neighbor_labels == labels[:, None])
    return float(same.mean())


def pas(
    labels: np.ndarray,
    coords: np.ndarray,
    k: int = 10,
    threshold: float = 0.5,
) -> float:
    """Percentage of "abnormal" spots — those whose label differs from
    the majority of their k spatial neighbors.

    Lower = better. ``0.0`` means every spot is locally consistent;
    ``1.0`` means every spot disagrees with most of its neighbors.

    Parameters
    ----------
    labels :
        Per-observation cluster label array.
    coords :
        Per-observation spatial coordinates.
    k :
        Number of spatial neighbors to consider.
    threshold :
        A spot is "abnormal" when *strictly less than* ``threshold``
        of its neighbors share its label. Default ``0.5`` (i.e. a spot
        is abnormal when neighbors are mostly different).

    Returns
    -------
    pas :
        Fraction of abnormal spots, in ``[0, 1]``.
    """
    labels = np.asarray(labels)
    coords = np.asarray(coords)
    _validate(labels, coords)
    idx = _spatial_knn_indices(coords, k)
    neighbor_labels = labels[idx]
    same_rate = (neighbor_labels == labels[:, None]).mean(axis=1)
    return float((same_rate < threshold).mean())


def mlami(
    labels: np.ndarray,
    coords: np.ndarray,
    *,
    n_neighbors: int = 15,
    min_res: float = 0.1,
    max_res: float = 1.0,
    res_num: int = 3,
    seed: int = 0,
) -> float:
    """Maximum Leiden Adjusted Mutual Info (MLAMI) — multi-scale spatial coherence.

    Builds a spatial k-NN graph on ``coords``, runs Leiden community
    detection at ``res_num`` resolutions linearly spaced in
    ``[min_res, max_res]``, and returns the maximum
    ``adjusted_mutual_info_score(labels, spatial_leiden_at_res_i)``
    across all resolutions. Higher = the input labels match the spatial
    organisation at *some* scale.

    Unlike CHAOS / PAS which only look at 1-hop neighborhoods, MLAMI
    captures multi-scale agreement with the spatial graph.

    Parameters
    ----------
    labels :
        Per-observation cluster label array, shape ``(n_obs,)``.
    coords :
        Per-observation spatial coordinates, shape ``(n_obs, n_dim)``.
    n_neighbors :
        k for the spatial graph used as Leiden input.
    min_res, max_res, res_num :
        Linearly spaced Leiden resolutions to sweep.
    seed :
        Random seed forwarded to scanpy / Leiden for reproducibility.

    Returns
    -------
    mlami :
        Maximum AMI across the resolution sweep, typically in ``[0, 1]``;
        AMI can occasionally dip slightly below 0 for highly adversarial
        label assignments.
    """
    labels = np.asarray(labels)
    coords = np.asarray(coords)
    _validate(labels, coords)

    import scanpy as sc
    from anndata import AnnData
    from sklearn.metrics import adjusted_mutual_info_score

    n = coords.shape[0]
    adata = AnnData(X=np.zeros((n, 1), dtype=np.float32))
    adata.obsm["spatial"] = coords.astype(np.float32)
    sc.pp.neighbors(
        adata,
        n_neighbors=min(n_neighbors, n - 1),
        use_rep="spatial",
        random_state=seed,
    )

    best = -1.0
    for res in np.linspace(min_res, max_res, res_num):
        leiden_kwargs: dict[str, Any] = {
            "resolution": float(res),
            "random_state": seed,
            "key_added": "_mlami_spatial",
        }
        # scanpy 1.10+ emits a FutureWarning about ``flavor`` and ``n_iterations``;
        # ignore quietly to keep the metric output deterministic.
        try:
            sc.tl.leiden(adata, flavor="igraph", n_iterations=2, directed=False, **leiden_kwargs)
        except TypeError:
            sc.tl.leiden(adata, **leiden_kwargs)
        spatial_clusters = adata.obs["_mlami_spatial"].astype(str).to_numpy()
        ami = adjusted_mutual_info_score(labels, spatial_clusters)
        if ami > best:
            best = float(ami)

    return best
