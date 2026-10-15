---
name: sc-clustering
description: Load when building the neighbour graph, embedding (UMAP/t-SNE/diffmap/PHATE), and clustering
  (Leiden/Louvain) on a normalised single-cell AnnData. Skip when QC/normalisation/HVG/PCA have not run
  yet (use sc-preprocessing); marker ranking after clustering (use sc-markers).
tags:
- singlecell
- scrna
- clustering
- leiden
- louvain
- umap
- tsne
- phate
---

# Single-Cell Graph Construction, UMAP & Leiden Clustering (`sc-clustering`)

## 1. Method Decision Matrix & When to Use

Use this playbook to construct a $k$-nearest neighbor ($k$NN) graph from a latent representation (`X_pca`, `X_scVI`, `X_harmony`), compute a 2D UMAP embedding, and partition cells into communities via Leiden clustering.

| Upstream State | Latent Representation (`use_rep`) | Recommended Parameters | When to Choose |
|---|---|---|---|
| **Single-sample preprocessed** | `use_rep="X_pca"` | `n_neighbors=15`, `n_pcs=30`, `resolution=0.8..1.0` | Standard single-sample scRNA-seq after `sc-preprocessing` |
| **Harmony-integrated** | `use_rep="X_pca_harmony"` | `n_neighbors=15`, `resolution=0.8..1.0` | After `sc-batch-integration` with Harmony |
| **scVI / scANVI latent space** | `use_rep="X_scVI"` | `n_neighbors=15`, `n_pcs=None` (use full 10–30D latent) | After variational autoencoder batch integration |
| **BBKNN batch-balanced graph** | Graph already in `adata.obsp` | Skip `sc.pp.neighbors`; run `sc.tl.umap` + `sc.tl.leiden` | After `sc.external.pp.bbknn` |

## 2. How to Run (Idiomatic Upstream Patterns)

```python
import scanpy as sc
import pandas as pd

# 1. Choose latent representation automatically or explicitly
rep = "X_scVI" if "X_scVI" in adata.obsm else ("X_pca_harmony" if "X_pca_harmony" in adata.obsm else "X_pca")
n_pcs = 30 if rep == "X_pca" else None

# 2. Build kNN graph and UMAP embedding with fixed random seed
sc.pp.neighbors(adata, n_neighbors=15, n_pcs=n_pcs, use_rep=rep, random_state=0)
sc.tl.umap(adata, min_dist=0.5, spread=1.0, random_state=0)

# 3. Run Leiden community detection (igraph backend avoids leidenalg deprecation warnings)
sc.tl.leiden(
    adata,
    resolution=1.0,
    key_added="leiden",
    flavor="igraph",
    n_iterations=2,
    random_state=0,
)

# 4. Summarize cluster sizes
cluster_counts = (
    adata.obs["leiden"]
    .value_counts()
    .rename_axis("cluster")
    .reset_index(name="n_cells")
)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-clustering")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `cluster(adata, n_neighbors=15, n_pcs=30, resolution=1.0, key_added="leiden", random_state=0)`
- `cluster_summary(adata, key="leiden")`

## 3. Output Format & Verification Contract

- **Clustered AnnData** (`intermediate/clustered.h5ad`):
  - `adata.obs["leiden"]`: categorical cluster labels (`"0"`, `"1"`, ...).
  - `adata.obsm["X_umap"]`: 2D coordinates of shape `(n_obs, 2)` with zero `NaN` values.
  - `adata.obsp["connectivities"]` & `adata.obsp["distances"]`: sparse $k$NN graph matrices.
- **Cluster Summary** (`tables/clusters.csv` or `tables/clusters.json`): cell counts and percentages per cluster, verifying $\sum 	ext{n\_cells} = 	ext{adata.n\_obs}$.

## 4. Gotchas & Troubleshooting

1. **Overwriting Batch-Integrated Graphs**:
   - If upstream integration used `scVI` (`obsm["X_scVI"]`) or `Harmony` (`obsm["X_pca_harmony"]`), calling `sc.pp.neighbors(adata)` without `use_rep=` silently falls back to uncorrected `X_pca` and reintroduces batch effects! Always inspect `list(adata.obsm.keys())` first.
2. **Precondition Check (`raw_counts` vs. Normalized Input)**:
   - Never run `sc.pp.neighbors` on raw integer counts without PCA (`X_pca`). If `"X_pca"` is absent from `adata.obsm`, run normalization, HVG selection, and `sc.tl.pca` first (see `sc-preprocessing`).
3. **Scanpy $\ge 1.10$ Leiden Backend (`flavor="igraph"`)**:
   - Pass `flavor="igraph", n_iterations=2` to `sc.tl.leiden` for deterministic, fast execution without `leidenalg` deprecation warnings.
4. **Resolution Sensitivity**:
   - `resolution=0.3..0.5` yields coarse lineage clusters (T cells, B cells, Myeloid); `resolution=1.0..1.5` resolves fine subpopulations. When exploring, write multiple columns (`key_added=f"leiden_{res}"`) rather than overwriting `"leiden"`.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `louvain`, `matplotlib`, `numpy`, `pandas`, `phate`, `scanpy`, `scikit-learn`, `scipy`, `seaborn`
