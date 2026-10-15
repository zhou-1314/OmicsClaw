---
name: spatial-integrate
description: Load when removing batch effects from multi-batch spatial AnnData with PCA using Harmony, BBKNN, or Scanorama. Skip physical coordinate alignment (use spatial-register) and single-batch data (use spatial-domains).
trigger: multi-sample integration, batch correction, Harmony, BBKNN, Scanorama
tags:
- spatial
- integration
- batch-correction
---

# Multi-Sample Spatial Expression Integration & Batch Correction (`spatial-integrate`)

## 1. Method Decision Matrix & When to Use

Use this playbook to remove technical slide/batch effects from multi-sample spatial transcriptomics embeddings (`X_pca` or neighbor graph) before joint clustering (`spatial-domains`) or UMAP visualization. For aligning 2D physical tissue coordinates across serial sections, use `spatial-register` instead.

| Method (`method=`) | Upstream Package | Corrected Output | Preserves Continuous Latent Space | Best Used When |
|---|---|---|---|---|
| **Harmony** (`harmony`) | `harmonypy` (`hm.run_harmony`) | `adata.obsm["X_pca_harmony"]` + rebuilt `obsp["connectivities"]` | Yes (`(n_obs, n_pcs)` embedding) | Default fast, scalable multi-slide integration; compatible with `cellcharter` and `sc.pp.neighbors` |
| **BBKNN** (`bbknn`) | `bbknn` (`bbknn.bbknn`) | Batch-balanced `adata.obsp["connectivities"]` & `distances` | No (graph-only; no corrected PCA matrix) | Lightweight graph-level integration directly feeding `sc.tl.umap` and `sc.tl.leiden` |
| **Scanorama** (`scanorama`) | `scanorama` (`scanorama.integrate_scanpy`) | `adata.obsm["X_scanorama"]` | Yes (`(n_obs, 100)` embedding) | Stitching partially overlapping tissue types or heterogeneous FOVs across slides |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Harmony PCA Integration (`harmonypy` + `scanpy`)

```python
import numpy as np
import harmonypy as hm
import scanpy as sc

# 1. Select batch-aware HVGs and compute PCA if not already present
adata.obs["batch"] = adata.obs["batch"].astype(str).astype("category")
if "X_pca" not in adata.obsm:
    sc.pp.highly_variable_genes(adata, n_top_genes=min(2000, adata.n_vars), batch_key="batch")
    sc.tl.pca(adata, n_comps=30, random_state=0)

# 2. Run Harmony on X_pca
ho = hm.run_harmony(adata.obsm["X_pca"], adata.obs, "batch", random_state=0, verbose=False)
Z = np.asarray(ho.Z_corr)
adata.obsm["X_pca_harmony"] = Z.T if Z.shape[0] != adata.n_obs else Z

# 3. Rebuild neighbor graph, UMAP, and joint Leiden clusters from X_pca_harmony
sc.pp.neighbors(adata, use_rep="X_pca_harmony", n_neighbors=15, random_state=0)
sc.tl.umap(adata, random_state=0)
sc.tl.leiden(adata, resolution=0.8, key_added="leiden_integrated", flavor="igraph", n_iterations=2, random_state=0)
```

### Pattern B: Batch-Balanced KNN Graph (`bbknn`)

```python
import bbknn
import scanpy as sc

# BBKNN directly replaces adata.obsp["connectivities"] and adata.obsp["distances"]
bbknn.bbknn(adata, batch_key="batch", use_rep="X_pca", neighbors_within_batch=3)
sc.tl.umap(adata, random_state=0)
sc.tl.leiden(adata, resolution=0.8, key_added="leiden_integrated", flavor="igraph", n_iterations=2, random_state=0)
```

### Pattern C: Mutual Nearest Neighbor Integration (`scanorama`)

```python
import scanorama
import scanpy as sc

# Split AnnData into per-batch list (Scanorama requires observations ordered by batch)
batches = [adata[adata.obs["batch"] == b].copy() for b in adata.obs["batch"].unique()]
scanorama.integrate_scanpy(batches, dimred=50)
adata_int = sc.concat(batches, index_unique=None)
adata.obsm["X_scanorama"] = adata_int.loc[adata.obs_names].obsm["X_scanorama"]
sc.pp.neighbors(adata, use_rep="X_scanorama", n_neighbors=15, random_state=0)
sc.tl.umap(adata, random_state=0)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-integrate")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `integrate_harmony(adata, batch_key="batch", use_rep="X_pca", key_added="X_pca_harmony", n_neighbors=15, random_state=0)`
- `batch_mixing_summary(adata, batch_key="batch", cluster_key="leiden")`

## 3. Output Format & Verification Contract

- **Integrated Spatial AnnData** (`intermediate/integrated.h5ad`):
  - `adata.obsm["X_pca_harmony"]` (or `adata.obsm["X_scanorama"]`): batch-corrected embedding.
  - `adata.obsp["connectivities"]` and `adata.obsm["X_umap"]`: integrated graph and UMAP coordinates.
  - `adata.obsm["spatial"]`: original per-slide physical coordinates preserved intact.
- **Batch Mixing Summary Table** (`tables/batch_mixing.csv`):
  - Columns: `cluster`, `n_spots`, `n_batches_present`, `batch_entropy_norm`.
- **Verification**:
  - Verify `adata.n_obs` is unchanged and most shared anatomical clusters exhibit `n_batches_present > 1` with improved normalized Shannon batch entropy.

## 4. Gotchas & Troubleshooting

1. **`harmonypy` `Z_corr` Transpose Orientation Across Versions**:
   - Depending on `harmonypy` version (`<=0.0.9` vs `>=0.0.10`), `ho.Z_corr` has shape `(n_pcs, n_obs)` or `(n_obs, n_pcs)`. Always check `Z.T if Z.shape[0] != adata.n_obs else Z` before assigning `adata.obsm["X_pca_harmony"]`.
2. **Do Not Call `sc.pp.neighbors` After `bbknn.bbknn`**:
   - `bbknn.bbknn` writes the batch-balanced graph directly into `adata.obsp["connectivities"]`. Calling `sc.pp.neighbors(adata)` afterward overwrites BBKNN's graph with an uncorrected PCA graph!
3. **Scanorama Requires Contiguous Batch Ordering**:
   - Calling `scanorama.integrate_scanpy` on an `adata` with interleaved batch rows raises an error or misaligns cells. Split `adata` by `batch_key` in a deterministic order and re-index back to `adata.obs_names`.
4. **Never Run Differential Expression on Batch-Corrected Embeddings**:
   - Use `X_pca_harmony` / `X_scanorama` strictly for clustering, neighborhood aggregation (`CellCharter`), and visualization. Always run `spatial-de` and `spatial-condition` on `adata.layers["counts"]` or uncorrected log1p `adata.X`.

## Dependencies

`anndata`, `bbknn`, `harmonypy`, `matplotlib`, `numpy`, `pandas`, `scanorama`, `scanpy`, `scipy`, `seaborn`

Missing method packages raise ImportError with an install_skill_deps hint.
