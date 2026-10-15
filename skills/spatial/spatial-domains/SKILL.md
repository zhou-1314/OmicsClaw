---
name: spatial-domains
description: Load when detecting tissue domains / niches on a preprocessed spatial AnnData via
  spatial-weighted Leiden / Louvain or graph-neural backends (SpaGCN / STAGATE / GraphST). Skip
  when ranking spatially variable genes (use spatial-genes); spot-level cell-type annotation
  (use spatial-annotate).
trigger: spatial domain, tissue region, niche, spatial niche, niche identification, niche detection, SpaGCN, STAGATE, CellCharter, BANKSY
tags:
- spatial
- domains
- niches
- spagcn
- stagate
- graphst
- banksy
- cellcharter
- leiden
- louvain
---

# Spatial Tissue Domain & Niche Identification (`spatial-domains`)

## 1. Method Decision Matrix & When to Use

Use this playbook to segment tissue into spatially coherent anatomical domains or microenvironmental niches by jointly modeling gene expression and physical 2D/3D coordinates:

| Method (`method=`) | Upstream Package | Graph / Spatial Formulation | Target Number of Domains | Best Used When |
|---|---|---|---|---|
| **Spatially Weighted Leiden / Louvain** (`leiden` / `louvain`) | `scanpy` + `squidpy` | Convex combination $(1 - w) A_{\text{expr}} + w A_{\text{spatial}}$ | Controlled by `resolution` | Fast, deterministic baseline domain discovery on any spatial platform |
| **CellCharter** (`cellcharter`) | `cellcharter` (`cc.gr.aggregate_neighbors` + `cc.tl.Cluster`) | Multi-hop neighborhood latent feature concatenation + GMM | Auto-stability selection or fixed $K$ | Multi-sample spatial niche discovery across Xenium, CosMx, and Visium cohorts |
| **BANKSY** (`banksy`) | `pybanksy` | Own expression + local neighborhood mean + azimuthal Gabor gradient | Leiden resolution on augmented matrix | Distinguishing cell-typing ($\lambda \approx 0.2$) vs tissue-domain segmentation ($\lambda \approx 0.8$) |
| **STAGATE** (`stagate`) | `STAGATE_pyG` | Graph attention auto-encoder over spatial radius/KNN graph | `mclust` fixed $K$ (`n_domains`) or Leiden | Resolving laminar cortical layers and smooth anatomical boundaries |
| **SpaGCN** (`spagcn`) | `SpaGCN` | Spatial coordinates + H&E RGB histology graph convolution | Fixed $K$ (`n_domains`) via Louvain initialization | When H&E histology color/texture boundaries complement transcriptomics |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Spatially Weighted Graph Clustering (`scanpy` + `squidpy`)

```python
import scanpy as sc
import squidpy as sq

# 1. Build expression KNN graph on PCA and spatial neighbor graph on coordinates
if "connectivities" not in adata.obsp:
    sc.pp.neighbors(adata, n_neighbors=15, use_rep="X_pca", random_state=0)
sq.gr.spatial_neighbors(adata, coord_type="generic", delaunay=True)

# 2. Blend expression and spatial connectivities (spatial_weight = 0.3)
w = 0.3
blended_adj = (1.0 - w) * adata.obsp["connectivities"] + w * adata.obsp["spatial_connectivities"]

# 3. Cluster the blended graph with Leiden
sc.tl.leiden(
    adata,
    resolution=0.8,
    adjacency=blended_adj,
    key_added="spatial_domain",
    flavor="igraph",
    n_iterations=2,
    random_state=0,
)
```

### Pattern B: Multi-Ring Neighborhood Niche Discovery (`cellcharter`)

```python
import cellcharter as cc
import squidpy as sq

# Build Delaunay spatial graph (pass library_key="slide_id" for multi-slide datasets)
sq.gr.spatial_neighbors(adata, coord_type="generic", delaunay=True)
cc.gr.remove_long_links(adata)

# Aggregate latent embeddings (e.g., X_pca or X_scVI) across 3 spatial neighbor rings
cc.gr.aggregate_neighbors(adata, n_layers=3, use_rep="X_pca", out_key="X_cellcharter")

# Cluster aggregated microenvironment profiles with Gaussian Mixture Model
gmm = cc.tl.Cluster(n_clusters=7, random_state=42)
gmm.fit(adata, use_rep="X_cellcharter")
adata.obs["spatial_domain"] = gmm.predict(adata, use_rep="X_cellcharter")
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-domains")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `spatial_leiden(adata, spatial_weight=0.3, resolution=1.0, n_neighbors=15, coord_type="generic", key_added="spatial_domain", random_state=0)`
- `domain_summary(adata, key="spatial_domain")`

## 3. Output Format & Verification Contract

- **Domain-Annotated Spatial AnnData** (`intermediate/domains.h5ad`):
  - `adata.obs["spatial_domain"]`: categorical domain/niche label for each spot/cell.
  - `adata.obsp["spatial_connectivities"]` and `adata.obsp["spatial_distances"]`: spatial graph matrices.
- **Domain Counts Table** (`tables/domain_counts.csv`):
  - Columns: `domain`, `n_spots`, `proportion`.
- **Verification**:
  - Verify `adata.obs["spatial_domain"].nunique() >= 2` and inspect spatial coherence via `sq.gr.nhood_enrichment(adata, cluster_key="spatial_domain")` (diagonal self-enrichment $z$-scores should be strongly positive).

## 4. Gotchas & Troubleshooting

1. **Multi-Slide Coordinate Collision in `sq.gr.spatial_neighbors`**:
   - When `adata` contains multiple slides concatenated together, running `sq.gr.spatial_neighbors(adata)` without `library_key="slide_id"` connects spots from different slides that happen to share similar `(x, y)` pixel values. Always pass `library_key` for multi-slide objects.
2. **Long-Range Edge Artifacts in Delaunay Triangulation**:
   - At concave tissue borders or lumens, Delaunay triangulation (`delaunay=True`) creates long edges spanning empty space. Prune them with `cellcharter.gr.remove_long_links(adata)` or use `coord_type="grid", n_neighs=6` on hexagonal Visium arrays.
3. **Over-Smoothing with High `spatial_weight`**:
   - Setting `spatial_weight > 0.5` forces contiguous geometric blocks even when transcriptomic profiles differ sharply (e.g., erasing tertiary lymphoid structures inside tumor stroma). Keep `spatial_weight` in `0.2–0.35`.

## Dependencies

`anndata`, `cellcharter`, `GraphST`, `igraph`, `louvain`, `matplotlib`, `numpy`, `pandas`, `pybanksy`, `scanpy`, `scikit-learn`, `scipy`, `seaborn`, `SpaGCN`, `squidpy`, `STAGATE-pyG`, `torch`
