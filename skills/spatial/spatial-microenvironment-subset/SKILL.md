---
name: spatial-microenvironment-subset
description: Load when extracting a niche / microenvironment subset around a center cell type by radius
  from a labelled spatial AnnData, producing a smaller AnnData of centers + neighbours. Skip
  when running global tissue-domain detection (use spatial-domains); cross-condition comparison
  (use spatial-condition).
trigger: microenvironment, neighborhood subset, spatial radius, neighboring cells, nearby cells, tumor microenvironment, extract cells within 50 microns
tags:
- spatial
- microenvironment
- niche
- subsetting
- neighbourhood
- visium
- xenium
---

# Spatial Microenvironment & Radius-Based Niche Subsetting (`spatial-microenvironment-subset`)

## 1. Method Decision Matrix & When to Use

Use this playbook to extract a local **microenvironment subset** (`AnnData`) consisting of designated **center cells/spots** (e.g., tumor cells, TLS B-cells, amyloid plaques) together with all **neighboring cells/spots** within a specified physical distance radius or topological ring hop:

| Subset Strategy | Upstream Library | Distance Metric | Platform Fit | Best Used When |
|---|---|---|---|---|
| **Physical Euclidean Radius** (`cKDTree` / `squidpy` radius) | `scipy.spatial.cKDTree` / `squidpy.gr.spatial_neighbors(radius=...)` | Continuous 2D/3D Euclidean distance ($\le r\ \mu\text{m}$ or pixels) | Xenium, CosMx, MERFISH, Slide-seq, Stereo-seq | Extracting all immune/stromal cells within $30\text{–}100\ \mu\text{m}$ paracrine signaling range of center cells |
| **Topological $k$-Ring Hop** (`n_rings`) | `squidpy.gr.spatial_neighbors(coord_type="grid", n_rings=k)` | Discrete hexagonal or square grid hops ($1, 2, \dots, k$ rings) | 10x Visium ($100\ \mu\text{m}$ center-to-center pitch) | Isolating the immediate 1-ring ($6$ spots) or 2-ring ($18$ spots) invasive margin around a tumor domain |
| **Direct Contact Neighbors** (`delaunay`) | `squidpy.gr.spatial_neighbors(delaunay=True)` | First-order Voronoi/Delaunay adjacency | Single-cell segmented spatial data | Studying juxtacrine (direct membrane contact) neighbors around target cells |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Multi-Slide Safe Euclidean Radius Subsetting (`scipy.spatial.cKDTree`)

```python
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

center_types = ["Epithelial_Tumor", "Malignant"]
radius = 50.0  # Ensure units match adata.obsm["spatial"] (microns vs pixels)
slide_col = "slide_id" if "slide_id" in adata.obs.columns else None

keep_mask = np.zeros(adata.n_obs, dtype=bool)
min_dists = np.full(adata.n_obs, np.inf, dtype=float)
is_center_all = adata.obs["cell_type"].astype(str).isin(center_types).to_numpy()

# Query KD-tree independently per slide so coordinates never cross slides
groups = adata.obs.groupby(slide_col, observed=True).indices if slide_col else {"all": np.arange(adata.n_obs)}
for _, idx in groups.items():
    coords = np.asarray(adata.obsm["spatial"][idx], dtype=float)
    c_mask = is_center_all[idx]
    if not np.any(c_mask):
        continue
    tree = cKDTree(coords[c_mask])
    dists, _ = tree.query(coords, k=1)
    min_dists[idx] = dists
    keep_mask[idx] = dists <= radius

adata_niche = adata[keep_mask].copy()
adata_niche.obs["dist_to_center"] = min_dists[keep_mask]
adata_niche.obs["niche_role"] = pd.Categorical(
    np.where(is_center_all[keep_mask], "center", "neighbor"),
    categories=["center", "neighbor"],
)
```

### Pattern B: Visium $k$-Ring Topological Margin Extraction (`squidpy`)

```python
import numpy as np
import squidpy as sq

# Build 2-ring hexagonal Visium neighbor graph
sq.gr.spatial_neighbors(adata, coord_type="grid", n_neighs=6, n_rings=2)
adj = adata.obsp["spatial_connectivities"]

is_center = (adata.obs["spatial_domain"].astype(str) == "Tumor_Core").to_numpy()
has_center_nb = np.asarray(adj[:, is_center].sum(axis=1)).ravel() > 0
keep = is_center | has_center_nb

adata_margin = adata[keep].copy()
adata_margin.obs["niche_role"] = np.where(is_center[keep], "center", "neighbor")
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-microenvironment-subset")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `subset_microenvironment(adata, cell_type_key="cell_type", center_types=[...], radius=50.0, spatial_key="spatial")`
- `niche_composition(adata_sub, cell_type_key="cell_type", role_key="niche_role")`

## 3. Output Format & Verification Contract

- **Microenvironment Subset AnnData** (`intermediate/microenvironment_subset.h5ad`):
  - `adata_sub.obs["niche_role"]`: categorical role with levels `"center"` and `"neighbor"`.
  - `adata_sub.obs["dist_to_center"]`: Euclidean distance ($0.0$ for `"center"` cells, $\le \text{radius}$ for `"neighbor"` cells).
  - Preserves all original `layers`, `obsm["spatial"]`, and `var` metadata for downstream `spatial-de` or `spatial-communication`.
- **Niche Composition Table** (`tables/niche_composition.csv`):
  - Columns: `cell_type`, `center`, `neighbor`, `total`, `proportion`.
- **Verification**:
  - Verify `(adata_sub.obs["niche_role"] == "center").sum() > 0` and `adata_sub.obs["dist_to_center"].max() <= radius`.

## 4. Gotchas & Troubleshooting

1. **Pixel vs. Micron ($\mu\text{m}$) Coordinate Units in `adata.obsm["spatial"]`**:
   - On Xenium and MERSCOPE, `adata.obsm["spatial"]` is typically in microns ($\mu\text{m}$), so `radius=50` means $50\ \mu\text{m}$. On 10x Visium loaded via `sc.read_visium`, `adata.obsm["spatial"]` is in **full-resolution H&E image pixels**, where $100\ \mu\text{m}$ may equal $300\text{–}1,500$ pixels! Inspect `np.median(cKDTree(coords).query(coords, k=2)[0][:, 1])` (nearest-neighbor pitch) before choosing `radius`.
2. **Multi-Slide Coordinate Bleed**:
   - Building a single `cKDTree` across a multi-slide `AnnData` links center cells on Slide 1 with neighbor cells at the same `(x, y)` coordinates on Slide 2. Always iterate over `slide_id` / `library_id` groups.
3. **Center Priority Over Neighbor**:
   - Two adjacent tumor cells are within `radius` of each other. Always assign `"center"` priority (`dist_to_center == 0`) so center cells are never misclassified as `"neighbor"` cells.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`
