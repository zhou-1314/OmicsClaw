---
name: sc-preprocessing
description: Load when normalising QC'd scRNA into a PCA-ready AnnData via scanpy / Seurat / SCTransform
  / Pearson residuals. Skip when QC thresholds are still undecided (use sc-qc); batch correction across
  samples (use sc-batch-integration).
trigger: single cell preprocess, scRNA preprocessing, normalize hvg pca, base preprocessing, Seurat preprocessing, SCTransform preprocessing
tags:
- singlecell
- scrna
- preprocessing
- normalization
- hvg
- pca
- scanpy
- seurat
- sctransform
- pearson_residuals
---

# Single-Cell RNA-seq Preprocessing (`sc-preprocessing`)

## 1. Method Decision Matrix & When to Use

Use this playbook to take raw single-cell UMI count matrices (`AnnData`) through quality control, count-layer preservation, library-size normalization, highly variable gene (HVG) selection, and principal component analysis (PCA).

| Workflow Stage | Standard Method | Alternative (Multi-Batch / Count Model) | Upstream Function |
|---|---|---|---|
| **QC Metrics** | Mitochondrial (`MT-`/`mt-`), ribosomal (`RPS`/`RPL`), `total_counts`, `n_genes_by_counts` | MAD-based adaptive outlier filtering (`scipy.stats.median_abs_deviation`) | `sc.pp.calculate_qc_metrics` |
| **Count Preservation** | Store raw integer UMI matrix in `adata.layers["counts"]` before normalization | Keep full unsubsetted object in `adata.raw` | `adata.layers["counts"] = adata.X.copy()` |
| **Normalization** | Shifted logarithm: `normalize_total(target_sum=1e4)` + `log1p` | Pearson residuals (`sc.experimental.pp.normalize_pearson_residuals`) | `sc.pp.normalize_total`, `sc.pp.log1p` |
| **HVG Selection** | `flavor="seurat_v3"` on `layer="counts"` (requires `scikit-misc`) or `flavor="seurat"` on log1p | Batch-aware HVG (`batch_key="sample"`) | `sc.pp.highly_variable_genes` |
| **Dimensionality Reduction** | Truncated SVD / PCA on HVGs (`n_comps=30..50`, `random_state=0`) | `scvi.model.SCVI` latent space (see `sc-batch-integration`) | `sc.tl.pca` |

## 2. How to Run (Idiomatic Upstream Patterns)

```python
import scanpy as sc

# 1. Annotate QC gene sets (human: "MT-", mouse: "mt-")
adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
adata.var["ribo"] = adata.var_names.str.startswith(("RPS", "RPL", "Rps", "Rpl"))
sc.pp.calculate_qc_metrics(adata, qc_vars=["mt", "ribo"], percent_top=None, log1p=False, inplace=True)

# 2. Filter low-quality cells and uninformative genes
sc.pp.filter_cells(adata, min_genes=200)
sc.pp.filter_genes(adata, min_cells=3)
adata = adata[adata.obs["pct_counts_mt"] < 20.0].copy()

# 3. Preserve raw integer counts BEFORE library-size normalization
adata.layers["counts"] = adata.X.copy()

# 4. Normalize to CP10K and log1p-transform
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)

# 5. Select Highly Variable Genes (HVGs) and compute PCA
sc.pp.highly_variable_genes(
    adata,
    n_top_genes=min(2000, adata.n_vars),
    layer="counts",
    flavor="seurat_v3",
    batch_key="batch" if "batch" in adata.obs else None,
)
sc.tl.pca(adata, n_comps=min(30, adata.n_obs - 1, adata.n_vars - 1), mask_var="highly_variable", random_state=0)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-preprocessing")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `preprocess(adata, min_genes=200, min_cells=3, max_mt_pct=20.0, n_top_hvg=2000, n_pcs=30, species="human")`
- `qc_summary(adata)`

## 3. Output Format & Verification Contract

- **Processed AnnData** (`intermediate/processed.h5ad`):
  - `adata.X`: log1p-normalized expression matrix (`float32`/`float64`).
  - `adata.layers["counts"]`: filtered raw integer UMI counts (same shape `(n_obs, n_vars)` as `adata.X`).
  - `adata.obs`: contains `n_genes_by_counts`, `total_counts`, `pct_counts_mt`.
  - `adata.var`: contains `highly_variable` boolean mask.
  - `adata.obsm["X_pca"]`: PCA coordinates of shape `(n_obs, n_pcs)`.
- **Summary Table** (`tables/qc_summary.csv` or `.json`): pre- and post-filter cell/gene counts and median mitochondrial percentage.

## 4. Gotchas & Troubleshooting

1. **Species Case Sensitivity for Mitochondrial Genes**:
   - Human Ensembl/HGNC symbols use uppercase `MT-` (`MT-CO1`, `MT-ND1`), whereas mouse MGI symbols use title-case `mt-` (`mt-Co1`). If `adata.obs["pct_counts_mt"].max() == 0.0`, inspect `adata.var_names[:20]`—you may have Ensembl IDs (`ENSG...`) or the wrong species prefix.
2. **Preserving `layers["counts"]` After Cell/Gene Filtering**:
   - Always assign `adata.layers["counts"] = adata.X.copy()` **after** `filter_cells` / `filter_genes` (or slice `adata` so both `.X` and `.layers["counts"]` are subsetted together) and **before** `normalize_total`. Downstream tools (`scvi-tools`, `pydeseq2`, `cell2location`) fail if `layers["counts"]` is missing or log-transformed.
3. **`seurat_v3` HVG Requires Raw Counts**:
   - `sc.pp.highly_variable_genes(..., flavor="seurat_v3")` models mean-variance on integer counts (`layer="counts"`). Passing log-normalized data to `seurat_v3` or omitting `scikit-misc` raises an error; if `scikit-misc` is unavailable, use `flavor="seurat"` on log1p-normalized `adata.X`.
4. **Do Not Subset `adata` to HVGs In-Place**:
   - Keep all filtered genes in `adata` and pass `mask_var="highly_variable"` to `sc.tl.pca`. Dropping non-HVG genes (`adata = adata[:, adata.var.highly_variable]`) strips canonical cell-type markers needed later in `sc-cell-annotation` and `sc-de`.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `phate`, `scanpy`, `scipy`, `seaborn`
