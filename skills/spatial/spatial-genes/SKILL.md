---
name: spatial-genes
description: Load when ranking spatially variable genes with Moran's I, SpatialDE, SPARK-X, or FlashS. Skip when detecting tissue domains (use spatial-domains) or differential expression between groups (use spatial-de).
trigger: spatially variable gene, spatial gene, SVG, SpatialDE, SPARK-X, spatial pattern, Moran, spatial autocorrelation
tags:
- spatial
- svg
- spatially-variable-genes
- morans-i
- spatialde
- sparkx
- flashs
---

# Spatially Variable Gene (SVG) Discovery (`spatial-genes`)

## 1. Method Decision Matrix & When to Use

Use this playbook to rank **spatially variable genes (SVGs)** whose expression exhibits non-random spatial patterns across 2D/3D tissue coordinates, independent of cluster labels. For differential expression between predefined clusters or conditions, use `spatial-de` or `spatial-condition`.

| Method (`method=`) | Upstream Package | Statistical Test & Metric | Input Expression Layer | Best Used When |
|---|---|---|---|---|
| **Moran's $I$** (`morans`) | `squidpy.gr.spatial_autocorr(mode="moran")` | Global spatial autocorrelation $I \in [-1, 1]$ + analytical/permutation FDR | Log-normalized `adata.X` | Default fast, scalable SVG discovery across thousands of spots/cells |
| **Geary's $C$** (`gearys`) | `squidpy.gr.spatial_autocorr(mode="geary")` | Local squared-difference autocorrelation $C \in [0, 2]$ ($C < 1$ positive clustering) | Log-normalized `adata.X` | Detecting sharp local boundary gradients and focal hot-spots |
| **SpatialDE** (`spatialde`) | `SpatialDE` + `NaiveDE` | Gaussian Process decomposition of spatial vs. non-spatial variance (`FSV`, `LLR`) | Raw counts (`adata.layers["counts"]`) stabilized via `NaiveDE` | Estimating characteristic spatial length scales ($l$) and automatic expression histology (AEH) |
| **SPARK-X** (`sparkx`) | R `SPARK` (`sparkx`) | Non-parametric covariance kernel trace test | Raw integer counts + coordinates | Ultra-large spatial datasets ($>50,000$ spots/cells; Slide-seq, Stereo-seq, HD) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Moran's $I$ Spatial Autocorrelation (`squidpy`)

```python
import scanpy as sc
import squidpy as sq

# 1. Build spatial neighbor graph from adata.obsm["spatial"]
# Use coord_type="grid", n_neighs=6 for Visium; coord_type="generic", delaunay=True for single-cell spatial
sq.gr.spatial_neighbors(adata, coord_type="generic", delaunay=True)

# 2. Restrict candidate genes to expressed / highly variable genes to avoid zero-variance NaNs
candidate_genes = (
    adata.var_names[adata.var["highly_variable"]].tolist()
    if "highly_variable" in adata.var
    else adata.var_names.tolist()
)

# 3. Compute Moran's I and BH-adjusted p-values
sq.gr.spatial_autocorr(
    adata,
    mode="moran",
    genes=candidate_genes,
    n_perms=100,
    n_jobs=1,
)
svg_df = adata.uns["moranI"].reset_index().rename(columns={"index": "gene"})
sig_svgs = svg_df[svg_df["pval_norm_fdr_bh"] < 0.05].sort_values("I", ascending=False)
```

### Pattern B: Gaussian Process SVG Discovery (`SpatialDE` + `NaiveDE`)

```python
import numpy as np
import pandas as pd
import NaiveDE
import SpatialDE

# Extract raw integer counts for genes expressed in >= 10 spots
raw_mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
dense_counts = pd.DataFrame(
    raw_mat.toarray() if hasattr(raw_mat, "toarray") else np.asarray(raw_mat),
    index=adata.obs_names,
    columns=adata.var_names,
)
dense_counts = dense_counts.loc[:, (dense_counts > 0).sum(axis=0) >= 10]

sample_info = pd.DataFrame(adata.obsm["spatial"][:, :2], index=adata.obs_names, columns=["x", "y"])
sample_info["total_counts"] = dense_counts.sum(axis=1).clip(lower=1)

# Anscombe variance stabilization and library-size regression
norm_expr = NaiveDE.stabilize(dense_counts.T).T
resid_expr = NaiveDE.regress_out(sample_info, norm_expr.T, "np.log(total_counts)").T

results = SpatialDE.run(sample_info[["x", "y"]], resid_expr)
svg_df = results.sort_values("qval", ascending=True).rename(columns={"g": "gene"})
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-genes")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `morans_svg(adata, genes=None, n_perms=100, coord_type="generic", n_neighs=6, library_key=None)`
- `svg_summary(df, fdr=0.05, fdr_col="pval_norm_fdr_bh")`

## 3. Output Format & Verification Contract

- **SVG Ranking Table** (`tables/svg_results.csv`):
  - For Moran's $I$: `gene`, `I`, `pval_norm`, `pval_norm_fdr_bh` (and `pval_sim_fdr_bh` when `n_perms > 0`).
  - For SpatialDE: `gene` (`g`), `FSV` (fraction of spatial variance), `l` (length scale), `LLR`, `pval`, `qval`.
- **Verification**:
  - Confirm `I` values lie in $[-1, 1]$ with zero `NaN` rows among top-ranked genes, and visually confirm spatial gradients via `sq.pl.spatial_scatter(adata, color=top_genes[:4])`.

## 4. Gotchas & Troubleshooting

1. **Zero-Variance Genes Produce `NaN` in `sq.gr.spatial_autocorr`**:
   - Genes with 0 counts across all filtered spots have zero variance, yielding `I = NaN`. Always run `sc.pp.filter_genes(adata, min_cells=10)` or pass `genes=candidate_genes` before computing Moran's $I$.
2. **`SpatialDE` Quadratic Complexity ($O(N^3)$ GP Cholesky)**:
   - `SpatialDE` scales cubically in the number of spatial locations $N$ and linearly in genes. For slides with $>5,000$ spots/cells, use `squidpy` Moran's $I$ or `SPARK-X` instead of `SpatialDE`.
3. **Multi-Slide Datasets Require `library_key` in `sq.gr.spatial_neighbors`**:
   - If `adata` contains multiple tissue sections, pass `library_key="slide_id"` when building `spatial_neighbors` so autocorrelation is computed strictly within each tissue section's graph.
4. **Native Score Directionality Across Methods**:
   - Higher Moran's $I$ ($I \to 1$) and higher SpatialDE `LLR` indicate stronger spatial clustering, whereas **lower** Geary's $C$ ($C \to 0$) indicates stronger positive spatial autocorrelation.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`, `SpatialDE`, `squidpy`, `statsmodels`

SpatialDE also imports NaiveDE. SPARK-X requires the R package SPARK.
