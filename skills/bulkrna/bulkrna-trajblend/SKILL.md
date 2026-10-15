---
name: bulkrna-trajblend
description: Load when placing bulk RNA-seq samples on a single-cell reference's pseudotime axis (NNLS
  deconvolution + nearest-neighbour mapping). Skip when plain cell-type proportions (use bulkrna-deconvolution);
  native single-cell trajectory inference (use sc-pseudotime).
trigger: trajblend, trajectory, bulk to single cell, interpolation, bulk2single, VAE, deconvolution trajectory
tags:
- bulkrna
- trajectory
- pseudotime
- deconvolution
- single-cell
---

# Bulk-to-Single-Cell Trajectory Projection (`bulkrna-trajblend`)

## 1. Method Decision Matrix & When to Use

Select the bulk-to-single-cell trajectory alignment method based on reference data availability:

| Biological Goal & Reference Input | Method | Upstream Package | Why |
|---|---|---|---|
| **Place bulk time-course / patient cohorts onto an annotated scRNA-seq pseudotime axis** (`AnnData` with `obs["pseudotime"]` and `obs["cell_type"]`) | Joint standardized PCA + $k$-NN inverse-distance pseudotime projection + NNLS deconvolution | `scikit-learn` (`PCA`, `NearestNeighbors`) + `scipy.optimize.nnls` | Simultaneously estimates continuous sample maturation (`pseudotime_mean`, `pseudotime_std`) and underlying cell-type composition |
| **Generate synthetic single-cell profiles from bulk RNA-seq** | Deep variational autoencoder / GAN interpolation (`Bulk2Single` / `TrajBlend`) | `PyTorch` / `scvi-tools` | Deconvolves bulk mixtures into single-cell resolution states along developmental trajectories |
| **Infer pseudotime de novo on single-cell data first** | Diffusion Pseudotime (DPT) / Palantir / Monocle3 | `scanpy.tl.dpt` (`sc-pseudotime`) | Computes the reference single-cell trajectory before projecting bulk samples with `bulkrna-trajblend` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `anndata` + `scikit-learn` Joint PCA / $k$-NN Trajectory Projection

```python
import anndata as ad
import numpy as np
import pandas as pd
from scipy.optimize import nnls
from sklearn.decomposition import PCA
from sklearn.neighbors import NearestNeighbors

# 1. Load bulk expression (samples x genes) and annotated scRNA-seq reference (.h5ad)
bulk = pd.read_csv("data/bulk_samples_x_genes.csv", index_col=0)
adata_ref = ad.read_h5ad("data/sc_reference.h5ad")
ref_df = adata_ref.to_df()

# 2. Align on shared genes (>= 50 shared genes required)
shared = bulk.columns.intersection(ref_df.columns)
B, R = bulk[shared].to_numpy(dtype=float), ref_df[shared].to_numpy(dtype=float)

# 3. Standardize log1p expression per modality, project reference PCA, and query k=15 neighbors
B_z = (np.log1p(B) - np.log1p(B).mean(0)) / np.maximum(np.log1p(B).std(0), 1e-8)
R_z = (np.log1p(R) - np.log1p(R).mean(0)) / np.maximum(np.log1p(R).std(0), 1e-8)

pca = PCA(n_components=min(15, R_z.shape[0] - 1, len(shared)), random_state=42)
R_pcs = pca.fit_transform(R_z)
B_pcs = pca.transform(B_z)

nbrs = NearestNeighbors(n_neighbors=15).fit(R_pcs)
dists, idx = nbrs.kneighbors(B_pcs)
weights = 1.0 / np.maximum(dists, 1e-6)
weights /= weights.sum(axis=1, keepdims=True)
ref_pt = adata_ref.obs["pseudotime"].to_numpy(dtype=float)
bulk_pt = (weights * ref_pt[idx]).sum(axis=1)
```

### Pattern B: Lightweight Micro-Helper via `use_skill("bulkrna-trajblend")`

```python
# Calling use_skill("bulkrna-trajblend") auto-injects kernel.py helpers into the python kernel:
pt_df, fractions_df = project_bulk_pseudotime(
    bulk,
    reference=ref_df,
    cell_types=adata_ref.obs["cell_type"],
    pseudotime=adata_ref.obs["pseudotime"],
    k=15,
)
```

## 3. Output Format & Verification Contract

- **Bulk Pseudotime Estimates** (`tables/pseudotime_estimates.csv`):
  - Indexed by `sample` with columns `pseudotime_mean`, `pseudotime_std`, `dominant_cell_type`.
- **Deconvolved Cell-Type Fractions** (`tables/cell_fractions.csv`):
  - Indexed by `sample` with one column per reference `cell_type`; row sums equal $1.0 \pm 10^{-6}$.
- **Verification Invariants**:
  - At least 50 shared genes exist between `bulk` and `reference`.
  - Every `pseudotime_mean` falls within the observed `[pseudotime.min(), pseudotime.max()]` range of the single-cell reference.

## 4. Gotchas & Troubleshooting

1. **Sample-by-Gene vs. Gene-by-Sample Matrix Orientation**:
   - Single-cell `AnnData` objects (`adata.to_df()`) store **cells (observations) as rows and genes as columns**, whereas standard bulk RNA-seq tables often store **genes as rows and samples as columns**. Always transpose bulk matrices (`bulk = bulk_genes_x_samples.T`) so both inputs have genes as columns before calling `project_bulk_pseudotime`.
2. **Modality-Specific $Z$-Score Standardization Before Joint PCA**:
   - Directly concatenating unscaled bulk TPMs and sparse single-cell UMI counts causes PC1 to separate technology (bulk vs. scRNA-seq) rather than developmental progression. Standardizing $\log_1p$ features within each dataset aligns their centroids along the principal trajectory axes.
3. **Never Fabricate Reference Pseudotime**:
   - `bulkrna-trajblend` requires a genuine pre-computed pseudotime vector (`adata_ref.obs["pseudotime"]`). If the reference `.h5ad` lacks pseudotime annotations, run `sc-pseudotime` (`scanpy.tl.dpt`) on the single-cell reference first.
4. **Interpreting `pseudotime_std` on Asynchronous Bulk Mixtures**:
   - A bulk sample containing both early progenitors and late differentiated cells will project midway between the two states and exhibit high `pseudotime_std` across its $k$ nearest reference cells. Inspect `tables/cell_fractions.csv` alongside `pseudotime_mean` to distinguish a pure intermediate state from a bimodal mixture.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `anndata`, `scikit-learn`, `scipy`
