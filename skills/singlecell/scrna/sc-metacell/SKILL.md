---
name: sc-metacell
description: Load when aggregating single cells into metacells (sample-aware coarse-grained pseudo-cells)
  on a normalised scRNA AnnData via SEACells or KMeans on a low-D embedding. Skip when ranking marker
  genes per cluster (use sc-markers); trajectory pseudotime ordering (use sc-pseudotime).
tags:
- singlecell
- scrna
- metacell
- seacells
- aggregation
- pseudo-cells
---

# Single-Cell Metacell Aggregation via SEACells / KMeans (`sc-metacell`)

## 1. Method Decision Matrix & When to Use

Use this skill to aggregate transcriptionally homogeneous single cells into compact, denoised **metacells** (typically $50\text{--}100$ cells per metacell) that overcome single-cell dropout while preserving continuous manifold geometry.

| Analytical Goal | Recommended Method | Upstream Library | Input Embedding & Assumptions |
|---|---|---|---|
| **Manifold-preserving archetype metacells** (recommended for trajectories & GRNs) | **SEACells** (`seacells`) | `SEACells.core.SEACells` | Low-D embedding (`obsm["X_pca"]`, `obsm["X_scVI"]`, or `obsm["X_svd"]`); kernel archetypal analysis |
| **Fast Euclidean centroid partitioning** (no `SEACells` dependency) | **KMeans Metacells** (`kmeans`) | `sklearn.cluster.KMeans` (`kernel.py`) | Low-D embedding (`obsm["X_pca"]`); deterministic with `random_state` |
| **Donor-level DE across conditions** | **Sample-Level Pseudobulk** | `sc-de` (`pydeseq2`) | Sums raw integer counts per `(sample, cell_type)` (do **not** use `sc-metacell` as donor replicates) |

> **Decision Rule**: Choose `n_metacells` at a compaction ratio of roughly $1$ metacell per $50\text{--}75$ single cells (e.g., $\sim 100$ metacells for $7,500$ cells), and always verify $2 \le \text{n\_metacells} < \text{n\_obs}$.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Archetypal Metacell Inference (`SEACells`)

```python
import SEACells
import scanpy as sc

# 1. Ensure PCA or batch-corrected latent space exists
if "X_pca" not in adata.obsm:
    sc.tl.pca(adata, n_comps=30)

# 2. Fit SEACells model on latent representation
n_metacells = max(2, adata.n_obs // 75)
model = SEACells.core.SEACells(
    adata,
    build_kernel_on="X_pca",
    n_SEACells=n_metacells,
    n_waypoint_eigs=10,
    convergence_epsilon=1e-5,
)
model.construct_kernel_matrix()
model.initialize_archetypes()
model.fit(min_iter=10, max_iter=50)

# 3. Summarize into a metacell-level AnnData (adata.obs["SEACell"] holds assignments)
madata = SEACells.core.summarize_by_SEACell(adata, SEACells_label="SEACell", summarize_layer="counts")
```

### Pattern B: Fast KMeans Metacell Aggregation (`scikit-learn`)

```python
import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.cluster import KMeans

# 1. Partition cells on X_pca into n_metacells centroids
km = KMeans(n_clusters=30, random_state=0, n_init=10)
labels = pd.Series([f"MC_{i}" for i in km.fit_predict(adata.obsm["X_pca"])], index=adata.obs_names)
adata.obs["metacell"] = pd.Categorical(labels)

# 2. Aggregate mean expression per metacell
mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
mc_ids = sorted(adata.obs["metacell"].unique())
rows, meta = [], []
for mc in mc_ids:
    idx = adata.obs["metacell"] == mc
    sub = mat[idx.to_numpy()]
    rows.append(np.asarray(sub.mean(axis=0)).ravel())
    dom = adata.obs.loc[idx, "leiden"].mode().iloc[0] if "leiden" in adata.obs else "unknown"
    meta.append({"metacell": mc, "n_cells": int(idx.sum()), "dominant_label": str(dom)})

madata = ad.AnnData(X=np.vstack(rows), obs=pd.DataFrame(meta).set_index("metacell"), var=adata.var.copy())
madata.layers["mean_expression"] = madata.X.copy()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-metacell")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `metacells(adata, method="kmeans", use_rep="X_pca", n_metacells=30, random_state=0, celltype_key="leiden")`
- `aggregate_metacells(adata, labels=None, celltype_key="leiden")`
- `metacell_summary(madata)`

## 3. Output Format & Verification Contract

- **Annotated Single-Cell AnnData** (`processed.h5ad` / `metacells_annotated.h5ad`):
  - `adata.obs["metacell"]`: categorical metacell assignment (`MC_0`, `MC_1`, ...) for every cell.
- **Aggregated Metacell AnnData** (`intermediate/metacells.h5ad` or `tables/metacells.h5ad`):
  - Shape `(n_metacells, n_vars)` with `madata.layers["mean_expression"]`, `madata.obs["n_cells"]`, and `madata.obs["dominant_label"]`.
- **Tabular Outputs** (`tables/metacell_summary.csv`, `tables/cell_to_metacell.csv`):
  - `metacell_summary.csv`: `metacell`, `n_cells`, `dominant_label`.
  - `cell_to_metacell.csv`: `cell`, `metacell`.
- **Verification Invariants**:
  - Assert `madata.obs["n_cells"].sum() == adata.n_obs` and `(madata.obs["n_cells"] > 0).all()`.
  - Assert `adata.obs["metacell"].isna().sum() == 0`.

## 4. Gotchas & Troubleshooting

1. **Mean Aggregation vs. Integer Pseudobulk Sums**:
   - `sc-metacell` averages expression across cells in each metacell (`aggregation="mean"`). Do **not** feed mean-aggregated metacells directly into negative-binomial count models (`pydeseq2`, `edgeR`) that require raw integer sums across independent biological donors.
2. **Metacells Are Not Independent Biological Replicates**:
   - Partitioning 10,000 cells from one donor into 100 metacells does not create $N=100$ biological replicates. Never run condition-level differential expression across metacells without accounting for donor identity (`sample_key`).
3. **Cross-Sample / Cross-Condition Mixing**:
   - If you build metacells on a joint batch-integrated embedding (`X_scVI`, `X_pca_harmony`), a single metacell can merge cells from both control and treated samples. Partition per sample or inspect sample purity per metacell before condition comparisons.
4. **Processed Cell-Level H5AD vs. Aggregate H5AD**:
   - In CLI outputs, `processed.h5ad` retains original single cells with `obs["metacell"]`, whereas `tables/metacells.h5ad` stores the compressed `(n_metacells, n_vars)` AnnData object.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scikit-learn`, `scipy`, `SEACells`
