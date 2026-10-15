---
name: spatial-statistics
description: Load when running spatial autocorrelation / hotspot / co-occurrence / neighbourhood-enrichment
  / Ripley K stats on a clustered spatial AnnData via squidpy. Skip when ranking spatially variable genes
  (use spatial-genes); tissue domain detection (use spatial-domains).
trigger: spatial statistics, Moran, Geary, Ripley, co-occurrence, Getis-Ord, local Moran, centrality
tags:
- spatial
- statistics
- moran
- geary
- ripley
- co-occurrence
- nhood-enrichment
- getis-ord
- squidpy
---

# Spatial Point-Pattern & Graph Statistics (`spatial-statistics`)

## 1. Method Decision Matrix & When to Use

Use this playbook to quantify spatial tissue organization, cell-type co-localization, multi-scale clustering vs. dispersion, graph topology, and local spatial hotspots on an annotated or clustered spatial `AnnData`:

| Analysis Goal | Upstream Function | Statistical Metric | Best Used When |
|---|---|---|---|
| **Cell-Type Contact Enrichment** (`nhood_enrichment`) | `squidpy.gr.nhood_enrichment` | Permutation $z$-score matrix $(K \times K)$ of direct neighbor contacts | Testing whether two cell types or niches physically contact each other more (or less) than random permutation |
| **Distance-Stratified Co-Occurrence** (`co_occurrence`) | `squidpy.gr.co_occurrence` | Conditional probability ratio $P(\text{type } B \mid \text{type } A, d) / P(\text{type } B, d)$ across radial intervals | Measuring how cell-type attraction or exclusion decays over continuous physical distances |
| **Multi-Scale Point Process** (`ripley`) | `squidpy.gr.ripley` (`mode="L"`) | Ripley's $L(r)$ vs. Poisson CSR envelope | Determining whether a cell population forms focal aggregates ($L(r) > r$) or uniform spacing ($L(r) < r$) |
| **Graph Topology Centrality** (`centrality_scores`) | `squidpy.gr.centrality_scores` | Degree centrality, closeness centrality, average clustering coefficient | Quantifying hub vs. insular tissue architecture per cell type |
| **Local Hotspot / LISA Analysis** (`getis_ord` / `local_moran`) | `esda.getisord.G_Local` / `esda.moran.Moran_Local` | Per-spot $G_i^*$ $z$-score or Local Moran quadrant (`HH`, `LL`, `HL`, `LH`) | Pinpointing statistically significant spatial hotspots of a marker gene or pathway score |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Neighborhood Contact Enrichment & Radial Co-Occurrence (`squidpy`)

```python
import numpy as np
import pandas as pd
import squidpy as sq

# 1. Ensure cluster_key is strictly Categorical (required by squidpy!)
cluster_key = "cell_type"
adata.obs[cluster_key] = adata.obs[cluster_key].astype(str).astype("category")

# 2. Build spatial neighbor graph (pass library_key="slide_id" for multi-slide cohorts)
sq.gr.spatial_neighbors(adata, coord_type="generic", delaunay=True)

# 3. Compute permutation neighborhood enrichment z-scores
sq.gr.nhood_enrichment(adata, cluster_key=cluster_key, n_perms=500, seed=0)
zmat = pd.DataFrame(
    adata.uns[f"{cluster_key}_nhood_enrichment"]["zscore"],
    index=adata.obs[cluster_key].cat.categories,
    columns=adata.obs[cluster_key].cat.categories,
)

# 4. Compute distance-stratified co-occurrence ratio across radial intervals
sq.gr.co_occurrence(adata, cluster_key=cluster_key, n_splits=1, interval=50)
```

### Pattern B: Ripley's $L$ Function & Graph Centrality (`squidpy`)

```python
import squidpy as sq

# Ripley's L statistic (L(r) > 0 indicates spatial clustering beyond Complete Spatial Randomness)
sq.gr.ripley(adata, cluster_key=cluster_key, mode="L", max_dist=500, n_simulations=100, seed=0)
ripley_df = adata.uns[f"{cluster_key}_ripley_L"]["L_stat"].copy()

# Network centrality scores per cluster
sq.gr.centrality_scores(adata, cluster_key=cluster_key)
centrality_df = adata.uns[f"{cluster_key}_centrality_scores"].copy()
```

### Pattern C: Local Spatial Hotspot Detection — Getis-Ord $G_i^*$ (`libpysal` + `esda`)

```python
import numpy as np
from libpysal.weights import KNN
from esda.getisord import G_Local

# Build row-standardized KNN spatial weights from 2D coordinates
w = KNN.from_array(np.asarray(adata.obsm["spatial"], dtype=float), k=6)
w.transform = "R"

# Compute local Gi* hotspot z-scores for a target gene or signature
y = np.asarray(adata[:, "CXCL13"].X.toarray() if hasattr(adata.X, "toarray") else adata[:, "CXCL13"].X).ravel()
g_local = G_Local(y, w, transform="R", star=True, permutations=199)
adata.obs["hotspot_zscore"] = g_local.Zs
adata.obs["hotspot_pval"] = g_local.p_sim
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-statistics")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `nhood_enrichment_table(adata, cluster_key="leiden", n_perms=100, seed=0, library_key=None)`
- `spatial_centrality_table(adata, cluster_key="leiden")`

## 3. Output Format & Verification Contract

- **Neighborhood Enrichment Table** (`tables/nhood_enrichment_zscore.csv`):
  - Columns: `source`, `target`, `zscore`, `count` (or $K \times K$ symmetric matrix).
- **Centrality & Ripley Tables** (`tables/centrality_scores.csv`, `tables/ripley_L.csv`):
  - `degree_centrality`, `average_clustering`, `closeness_centrality` per cluster.
- **Verification**:
  - Verify `adata.obs[cluster_key].dtype.name == "category"` and check that `zscore` matrix dimensions match `len(adata.obs[cluster_key].cat.categories)`.

## 4. Gotchas & Troubleshooting

1. **`squidpy` Requires `pd.CategoricalDtype` on `adata.obs[cluster_key]`**:
   - Passing a string or integer column to `sq.gr.nhood_enrichment`, `sq.gr.co_occurrence`, `sq.gr.ripley`, or `sq.gr.centrality_scores` raises `TypeError: Expected ... to be categorical`. Always cast `adata.obs[cluster_key] = adata.obs[cluster_key].astype(str).astype("category")` and call `.cat.remove_unused_categories()` after subsetting.
2. **Pairwise Distance Matrix Memory Explosion in `sq.gr.co_occurrence`**:
   - `sq.gr.co_occurrence` computes pairwise Euclidean distances between cells. For Xenium/MERFISH slides with $>50,000$ cells, set `n_splits=10` (or subsample) to prevent $O(N^2)$ RAM exhaustion.
3. **Zero-Variance / Single-Cell Clusters Produce `NaN` $z$-Scores**:
   - If a rare cluster has $<5$ cells across the slide, permutation variance in `nhood_enrichment` can be zero, yielding `NaN` in the $z$-score matrix. Filter or merge tiny clusters before running permutation tests.

## Dependencies

`anndata`, `esda`, `libpysal`, `matplotlib`, `networkx`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`, `squidpy`
