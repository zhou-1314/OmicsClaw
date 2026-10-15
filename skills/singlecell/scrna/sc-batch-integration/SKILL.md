---
name: sc-batch-integration
description: Load when integrating multi-sample scRNA-seq with Harmony, scVI, scANVI, BBKNN, Scanorama,
  SIMBA, or supported R-backed methods to remove batch effects. Skip when the data is one sample (no batch
  effect to integrate); upstream merging only (use sc-multi-count).
trigger: batch integration, batch effect, harmony, scvi, bbknn, merge samples
tags:
- singlecell
- scrna
- batch-integration
- harmony
- scvi
- scanvi
- bbknn
- scanorama
- simba
---

# Single-Cell Multi-Batch Integration (`sc-batch-integration`)

## 1. Method Decision Matrix & When to Use

Select the integration method according to batch complexity, count availability, and sample size:

| Method (`--method` / `method=`) | Upstream Package | Input Required | Output Key | Best Used When |
|---|---|---|---|---|
| `method="harmony"` (`--method harmony`) | `scanpy.external.pp.harmony_integrate` / `harmonypy` | `adata.obsm["X_pca"]` + `batch_key` | `adata.obsm["X_pca_harmony"]` | Fast linear/soft-clustering correction across 2–50 batches |
| `method="scvi"` (`--method scvi`) | `scvi.model.SCVI` | Raw integer counts in `adata.layers["counts"]` | `adata.obsm["X_scVI"]` | Complex non-linear batch effects, large atlases, or multi-covariate designs |
| `method="scanorama"` (`--method scanorama`) | `scanpy.external.pp.scanorama_integrate` | `adata.obsm["X_pca"]` (batches in contiguous order) | `adata.obsm["X_scanorama"]` | Partially overlapping cell types across heterogeneous tissues |
| `method="bbknn"` (`--method bbknn`) | `scanpy.external.pp.bbknn` | `adata.obsm["X_pca"]` + `batch_key` | Batch-balanced `adata.obsp["connectivities"]` | Lightweight graph-level integration directly for UMAP & Leiden |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Harmony Integration on PCA (`method="harmony"`)

```python
import scanpy as sc
import scanpy.external as sce

# Ensure batch_key is categorical string and PCA is computed on batch-aware HVGs
adata.obs["batch"] = adata.obs["batch"].astype(str).astype("category")
if "X_pca" not in adata.obsm:
    sc.pp.highly_variable_genes(adata, n_top_genes=2000, batch_key="batch")
    sc.tl.pca(adata, n_comps=30, random_state=0)

sce.pp.harmony_integrate(adata, key="batch", basis="X_pca", adjusted_basis="X_pca_harmony")

# Build downstream neighbor graph on the Harmony-corrected embedding!
sc.pp.neighbors(adata, use_rep="X_pca_harmony", n_neighbors=15, random_state=0)
sc.tl.umap(adata, random_state=0)
sc.tl.leiden(adata, resolution=1.0, key_added="leiden", flavor="igraph", n_iterations=2, random_state=0)
```

### Pattern B: Deep Generative Integration with `scvi-tools` (`method="scvi"`)

```python
import scvi
import scanpy as sc

scvi.settings.seed = 0
adata_hvg = adata[:, adata.var["highly_variable"]].copy() if "highly_variable" in adata.var else adata.copy()

scvi.model.SCVI.setup_anndata(adata_hvg, layer="counts", batch_key="batch")
model = scvi.model.SCVI(adata_hvg, n_latent=30, n_layers=2, gene_likelihood="nb")
model.train(max_epochs=200, early_stopping=True)

adata.obsm["X_scVI"] = model.get_latent_representation()
sc.pp.neighbors(adata, use_rep="X_scVI", n_neighbors=15, random_state=0)
sc.tl.umap(adata, random_state=0)
```

## 3. Output Format & Verification Contract

- **Integrated AnnData** (`intermediate/integrated.h5ad`):
  - Contains the integrated latent representation (`adata.obsm["X_pca_harmony"]` or `adata.obsm["X_scVI"]`) with zero `NaN` values.
  - `sc.pp.neighbors` has been recomputed with `use_rep=` pointing to the integrated embedding (`adata.uns["neighbors"]["params"]["use_rep"]`).
- **Verification**:
  - Check batch mixing across Leiden clusters (`pd.crosstab(adata.obs["leiden"], adata.obs["batch"])`) to confirm batches are well mixed while biological cell types remain separated.

## 4. Gotchas & Troubleshooting

1. **Forgetting `use_rep="X_pca_harmony"` in `sc.pp.neighbors`**:
   - `harmony_integrate` writes `adata.obsm["X_pca_harmony"]` and leaves `X_pca` untouched. Calling `sc.pp.neighbors(adata)` without `use_rep="X_pca_harmony"` silently builds the graph on the unintegrated `X_pca`.
2. **Scanorama Contiguous Batch Ordering**:
   - `sce.pp.scanorama_integrate` requires `adata` observations to be sorted contiguously by `batch_key` (`adata = adata[adata.obs["batch"].argsort()].copy()`).
3. **`scVI` Requires Unnormalized Integer Counts**:
   - `scvi.model.SCVI.setup_anndata` models a Negative Binomial / ZINB distribution over raw integer counts (`layer="counts"`). Passing log-normalized data corrupts the variational posterior.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `bbknn`, `harmonypy`, `matplotlib`, `numpy`, `pandas`, `phate`, `scanorama`, `scanpy`, `scikit-learn`, `scipy`, `scvi-tools`, `seaborn`, `simba-bio`, `torch`
