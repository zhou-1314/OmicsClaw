---
name: scatac-preprocessing
description: Load when preprocessing a single-cell ATAC peak × cell AnnData via Signac-style TF-IDF +
  LSI + Leiden, producing a clustered UMAP-ready object. Skip when input is fragments; BAM (peak calling
  not implemented here); scRNA preprocessing (use sc-preprocessing).
trigger: scATAC preprocessing, single-cell ATAC preprocessing, ATAC TF-IDF LSI, chromatin accessibility clustering, scATAC UMAP Leiden
tags:
- singlecell
- scatac
- atac
- preprocessing
- tfidf
- lsi
- clustering
- leiden
---

# Single-Cell ATAC-seq Peak Preprocessing (`scatac-preprocessing`)

## 1. Method Decision Matrix & When to Use

Use this playbook to transform a sparse cell $\times$ peak chromatin accessibility matrix (`AnnData`) into a latent semantic indexing (`X_lsi`) embedding, neighbor graph, UMAP, and Leiden partition.

| Workflow Stage | Standard Method | Alternative Approach | Upstream Library | Input Assumptions |
|---|---|---|---|---|
| **Peak / Cell QC Filtering** | Retain cells with $\ge 200$ accessible peaks and peaks open in $\ge 5$ cells | Fragment-level TSS enrichment + nucleosome signal filtering | `scanpy.pp.filter_cells`, `scanpy.pp.filter_genes` / `muon.atac` | Non-negative count or binary accessibility in `adata.X` |
| **Feature Selection** | Top $10,000\text{--}50,000$ most accessible peaks (`n_top_peaks`) | Iterative LSI variable peak selection | `numpy` / `episcanpy.pp.select_var_feature` | Filtered cell $\times$ peak count matrix |
| **Normalization** | Signac-style log-TF-IDF: $\log(1 + \text{TF} \times \text{IDF} \times 10^4)$ | Binarization + standard TF-IDF (`muon.atac.pp.tfidf`) | `scipy.sparse` / `muon.atac.pp.tfidf` | Raw accessibility counts saved in `adata.layers["counts"]` |
| **Dimensionality Reduction** | Truncated SVD / LSI (`n_lsi=30..50`), **dropping LSI component 1** | Spectral embedding (`SnapATAC2`) or PeakVI (`scvi.external.PEAKVI`) | `sklearn.decomposition.TruncatedSVD` | Log-TF-IDF matrix in `adata.X` |
| **Graph & Clustering** | $k$NN on `X_lsi[:, 1:]` + UMAP + Leiden (`flavor="igraph"`) | Batch-corrected Harmony on `X_lsi[:, 1:]` | `scanpy.pp.neighbors`, `scanpy.tl.leiden` | Depth-decorrelated LSI components |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Signac-Style Log-TF-IDF + TruncatedSVD LSI (`sklearn` + `scanpy`)

```python
import numpy as np
import scipy.sparse as sp
from sklearn.decomposition import TruncatedSVD
import scanpy as sc

# 1. Filter sparse cells and rare peaks; select top accessible peaks
sc.pp.filter_cells(adata, min_genes=200)
sc.pp.filter_genes(adata, min_cells=5)
peak_totals = np.asarray(adata.X.sum(axis=0)).ravel()
if adata.n_vars > 10000:
    top_idx = np.argsort(peak_totals)[::-1][:10000]
    adata = adata[:, np.sort(top_idx)].copy()

# 2. Preserve raw peak counts and compute Signac-style log(1 + TF * IDF * 1e4)
adata.layers["counts"] = adata.X.copy()
mat = adata.X.tocsr().astype(np.float32)
cell_sums = np.maximum(np.asarray(mat.sum(axis=1)).ravel(), 1.0)
tf = sp.diags(1.0 / cell_sums) @ mat
peak_freq = np.maximum(np.asarray((mat > 0).sum(axis=0)).ravel(), 1.0)
idf = np.log1p(adata.n_obs / peak_freq)
tfidf = (tf @ sp.diags(idf)) * 1e4
tfidf.data = np.log1p(tfidf.data)
adata.X = tfidf

# 3. Fit TruncatedSVD (LSI) and exclude component 1 (sequencing depth) from neighbor graph
svd = TruncatedSVD(n_components=min(30, adata.n_obs - 1, adata.n_vars - 1), random_state=0)
adata.obsm["X_lsi"] = svd.fit_transform(adata.X)
adata.obsm["X_lsi_retained"] = adata.obsm["X_lsi"][:, 1:]

sc.pp.neighbors(adata, use_rep="X_lsi_retained", n_neighbors=15, random_state=0)
sc.tl.umap(adata, random_state=0)
sc.tl.leiden(adata, resolution=0.8, key_added="leiden", flavor="igraph", n_iterations=2, random_state=0)
```

### Pattern B: Multiome / Peak Matrix Loading (`10x` H5 or MTX)

```python
import scanpy as sc

# Load 10x Multiome H5 and subset explicitly to Chromatin Accessibility Peaks
adata_all = sc.read_10x_h5("filtered_feature_bc_matrix.h5", gex_only=False)
adata_atac = adata_all[:, adata_all.var["feature_types"] == "Peaks"].copy()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("scatac-preprocessing")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `preprocess_atac(adata, min_peaks=200, min_cells=5, n_top_peaks=10000, n_lsi=30, n_neighbors=15, resolution=0.8, random_state=0)`
- `tfidf_normalize(X, scale_factor=1e4)`
- `atac_summary(adata, cluster_key="leiden")`

## 3. Output Format & Verification Contract

- **Preprocessed scATAC AnnData** (`intermediate/processed.h5ad`):
  - `adata.X`: log1p-TF-IDF normalized accessibility matrix (`float32`, non-negative).
  - `adata.layers["counts"]`: raw peak accessibility counts on the retained peaks.
  - `adata.obsm["X_lsi"]`: full LSI matrix of shape `(n_obs, n_lsi)`.
  - `adata.obsm["X_umap"]`: 2D UMAP coordinates of shape `(n_obs, 2)` with zero `NaN` values.
  - `adata.obs["leiden"]`: categorical chromatin accessibility cluster assignments.
- **Summary Tables** (`tables/cluster_summary.csv`, `tables/lsi_variance_ratio.csv`, `tables/peak_summary.csv`):
  - Cluster sizes (`cluster`, `n_cells`, `pct`), per-component explained variance ratio (`component`, `variance_ratio`, `cumulative_variance_ratio`), and top accessible peaks (`peak`, `total_counts`, `n_cells`).
- **Verification Invariants**:
  - Confirm `adata.n_obs > 0`, `adata.obsm["X_lsi"].shape[1] >= 2`, and `sc.pp.neighbors` was constructed without LSI component 1 (`|corr(X_lsi[:, 0], total_counts)|` is typically $> 0.8$).

## 4. Gotchas & Troubleshooting

1. **Always Drop the First LSI Component (`X_lsi[:, 0]`) Before Neighbor Graph Construction**:
   - In uncentered TruncatedSVD on sparse scATAC TF-IDF matrices, the first singular vector (`X_lsi[:, 0]`) almost exclusively captures technical library size (total fragment depth per cell) rather than biological chromatin state. Including component 1 clusters cells by sequencing depth.
2. **Do Not Use scRNA-seq Log-CP10K + Centered PCA on Peak Matrices**:
   - scATAC peak matrices are $95\text{--}99\%$ zero and nearly binary ($0, 1, 2$ transposase cuts per locus). Standard centering (`sc.pp.scale`) densifies a $100,000$-peak matrix and destroys memory; always use sparse TF-IDF + uncentered `TruncatedSVD`.
3. **10x Multiome Feature Type Separation (`gex_only=False`)**:
   - `sc.read_10x_h5` defaults to `gex_only=True`, which drops all `Peaks` features in 10x Multiome datasets. Always pass `gex_only=False` and filter `adata.var["feature_types"] == "Peaks"`.
4. **Top-Peak Selection Subsets `adata.var`**:
   - Retaining the top $10,000\text{--}50,000$ accessible peaks speeds up SVD and improves signal-to-noise, but drops rare enhancer peaks from `adata.var`. Keep an unsubsetted copy if downstream cis-regulatory element linking requires the full peak universe.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `igraph`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scikit-learn`, `scipy`, `seaborn`
