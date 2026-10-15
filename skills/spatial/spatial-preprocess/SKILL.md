---
name: spatial-preprocess
description: Load when running the foundational spatial QC + filtering + normalisation + HVG + PCA +
  neighbour-graph + Leiden pipeline on a Visium / Xenium / generic AnnData. Skip when raw FASTQs
  need converting first (use spatial-raw-processing); domain detection on preprocessed data (use
  spatial-domains).
trigger: preprocess, spatial preprocessing, spatial QC, normalize, visium, xenium, merfish, slide-seq, load spatial data, leiden, umap
tags:
- spatial
- visium
- xenium
- preprocessing
- qc
- normalization
- hvg
- pca
- leiden
---

# Spatial Transcriptomics Preprocessing (`spatial-preprocess`)

## 1. Method Decision Matrix & When to Use

Use this playbook for foundational quality control, count preservation, normalization, HVG selection, PCA, $k$NN graph construction, UMAP, and baseline Leiden clustering on spatial transcriptomics `AnnData` (10x Visium, Visium HD, Xenium, MERFISH, Slide-seq).

| Platform | Typical Spot / Cell Scale | Recommended QC Thresholds | Normalization Standard |
|---|---|---|---|
| **10x Visium** (55 $\mu$m spots) | $2,000 - 5,000$ spots; whole transcriptome | `min_genes=200`, `min_cells=3`, `max_mt_pct=20..30%` | `normalize_total(target_sum=1e4)` + `log1p` |
| **10x Xenium / MERFISH** (targeted panel) | $50,000 - 500,000$ cells; $300 - 5,000$ genes | `min_counts=10`, `min_genes=5`; skip `n_top_hvg=2000` if panel $<1,000$ genes | `normalize_total` + `log1p` (or total-transcript scaling) |
| **Slide-seqV2 / Stereo-seq** | $10,000 - 100,000$ beads/bins | `min_genes=100`, `min_cells=3`, `max_mt_pct=20%` | `normalize_total(target_sum=1e4)` + `log1p` |

## 2. How to Run (Idiomatic Upstream Patterns)

```python
import scanpy as sc
import pandas as pd

# 1. Annotate mitochondrial genes and compute QC metrics
adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)

# 2. Filter low-quality spots/cells and genes (automatically subsets obsm["spatial"] in-place)
sc.pp.filter_cells(adata, min_genes=200)
sc.pp.filter_genes(adata, min_cells=3)
if "pct_counts_mt" in adata.obs:
    adata = adata[adata.obs["pct_counts_mt"] < 20.0].copy()

# 3. Preserve raw integer counts before log-normalization
adata.layers["counts"] = adata.X.copy()

# 4. Normalize and log1p-transform
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)

# 5. Select HVGs (cap n_top_genes at adata.n_vars for targeted spatial panels!)
n_hvg = min(2000, adata.n_vars)
sc.pp.highly_variable_genes(adata, n_top_genes=n_hvg, layer="counts", flavor="seurat_v3")

# 6. PCA, expression kNN graph, UMAP, and baseline Leiden clustering
n_pcs = min(30, adata.n_obs - 1, adata.n_vars - 1)
sc.tl.pca(adata, n_comps=n_pcs, random_state=0)
sc.pp.neighbors(adata, n_neighbors=15, n_pcs=n_pcs, random_state=0)
sc.tl.umap(adata, random_state=0)
sc.tl.leiden(adata, resolution=0.5, key_added="leiden", flavor="igraph", n_iterations=2, random_state=0)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-preprocess")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `preprocess(adata, min_genes=0, min_cells=0, max_mt_pct=20.0, n_top_hvg=2000, n_pcs=30, n_neighbors=15, leiden_resolution=0.5, species="human", random_state=0)`
- `cluster_summary(adata, key="leiden")`

## 3. Output Format & Verification Contract

- **Preprocessed Spatial AnnData** (`intermediate/processed.h5ad`):
  - `adata.X`: log1p-normalized expression matrix.
  - `adata.layers["counts"]`: filtered raw integer counts.
  - `adata.obsm["spatial"]`: 2D spatial coordinates of shape `(n_obs, 2)` aligned with filtered observations.
  - `adata.obs["leiden"]`: baseline expression cluster labels.
- **Cluster Summary Table** (`tables/clusters.csv`): spot/cell counts per cluster.

## 4. Gotchas & Troubleshooting

1. **Targeted Spatial Panels (Xenium / CosMx / MERFISH) Have $<2,000$ Genes**:
   - Calling `sc.pp.highly_variable_genes(adata, n_top_genes=2000)` when `adata.n_vars == 350` can raise an error or behave unexpectedly in `seurat_v3`. Always clamp `n_top_genes=min(2000, adata.n_vars)` or use all panel genes when `adata.n_vars <= 500`.
2. **Verify `in_tissue == 1` for Raw SpaceRanger Visium Outputs**:
   - If loading raw Visium matrices that still include background barcodes outside the tissue capture area (`adata.obs["in_tissue"] == 0`), filter to `adata = adata[adata.obs["in_tissue"] == 1].copy()` before computing QC metrics or HVGs.
3. **Expression Graph vs. Spatial Graph**:
   - `spatial-preprocess` builds the **transcriptomic** $k$NN graph (`sc.pp.neighbors`). For spatially informed tissue domain detection using coordinate graphs (`sq.gr.spatial_neighbors`, Banksy, SpaGCN, CellCharter), follow up with `spatial-domains`.

## Dependencies

Check these packages before starting a run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`, `scikit-misc`, `igraph`, `umap-learn`
