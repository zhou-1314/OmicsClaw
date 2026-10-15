---
name: spatial-register
description: Load when aligning multiple spatial slices into a common coordinate frame with PASTE or STalign. Skip single-slice data; for expression-space batch correction use spatial-integrate.
trigger: spatial registration, slice alignment, PASTE, STalign
tags:
- spatial
- registration
- alignment
---

# Multi-Slice Spatial Coordinate Registration & Alignment (`spatial-register`)

## 1. Method Decision Matrix & When to Use

Use this playbook to physically align 2D spatial coordinates (`adata.obsm["spatial"]`) across serial or adjacent tissue sections into a unified 2D/3D coordinate frame. For batch-correcting gene expression embeddings without moving physical coordinates, use `spatial-integrate`.

| Method (`method=`) | Upstream Package | Transformation Class | Uses Gene Expression? | Best Used When |
|---|---|---|---|---|
| **PASTE Pairwise Alignment** (`paste_pairwise`) | `paste` (`pst.pairwise_align` + `pst.stack_slices_pairwise`) | Fused Gromov-Wasserstein Optimal Transport + Weighted Procrustes Rigid (Rotation + Translation) | Yes (KL / generalized KL divergence controlled by `alpha`) | Aligning sequential serial sections ($Z_1 \to Z_2 \to Z_3$) into a 3D stack |
| **PASTE Center Integration** (`paste_center`) | `paste` (`pst.center_align`) | FGW Barycenter + Low-Rank NMF Consensus Slice | Yes | Inferring a single denoised consensus template slice from biological/technical replicates |
| **STalign Diffeomorphic Warping** (`stalign`) | `STalign` (`STalign.LDDMM`) | Large Deformation Diffeomorphic Metric Mapping (smooth nonlinear vector field + affine) | Rasterized cell density / landmark intensity | Registering sections with nonlinear tissue stretching, folding, or cross-platform distortion (e.g., Xenium to Visium) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Pairwise Optimal Transport Registration (`paste`)

```python
import numpy as np
import paste as pst
import scanpy as sc

# 1. Split multi-slice AnnData by slice_id and subset to shared positive genes
slices = [adata[adata.obs["slice_id"] == s].copy() for s in adata.obs["slice_id"].unique()]
common_genes = slices[0].var_names
for sl in slices[1:]:
    common_genes = common_genes.intersection(sl.var_names)
slices = [sl[:, common_genes].copy() for sl in slices]

# 2. Compute pairwise Fused Gromov-Wasserstein transport plans (alpha=0.1 balances expression + geometry)
pis = [
    pst.pairwise_align(slices[i], slices[i + 1], alpha=0.1, dissimilarity="kl", use_rep=None)
    for i in range(len(slices) - 1)
]

# 3. Project all slices into a common rigid 2D coordinate frame via generalized Procrustes
aligned_slices = pst.stack_slices_pairwise(slices, pis)
for sl_orig, sl_aligned in zip(slices, aligned_slices):
    sl_orig.obsm["spatial_aligned"] = sl_aligned.obsm["spatial"].copy()

adata_reg = sc.concat(slices, index_unique=None)
```

### Pattern B: Nonlinear Diffeomorphic Registration (`STalign`)

```python
import numpy as np
from STalign import STalign

# Rasterize source (moving) and target (fixed) point clouds into smooth density images
xI, yI = slice_mov.obsm["spatial"][:, 0], slice_mov.obsm["spatial"][:, 1]
xJ, yJ = slice_ref.obsm["spatial"][:, 0], slice_ref.obsm["spatial"][:, 1]

XI, YI, I, _ = STalign.rasterize(xI, yI, dx=30.0)
XJ, YJ, J, _ = STalign.rasterize(xJ, yJ, dx=30.0)

# Fit LDDMM affine + velocity field transformation and apply to moving coordinates
out = STalign.LDDMM([YI, XI], I, [YJ, XJ], J, niter=200, device="cpu")
tpoints = STalign.transform_points_source_to_target(out["xv"], out["v"], out["A"], np.column_stack([yI, xI]))
slice_mov.obsm["spatial_aligned"] = np.column_stack([tpoints[:, 1], tpoints[:, 0]])
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-register")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `procrustes_register(coords_ref, coords_mov, pi=None)`
- `registration_error(coords_ref, coords_mov, pi=None)`

## 3. Output Format & Verification Contract

- **Registered Spatial AnnData** (`intermediate/registered.h5ad`):
  - `adata.obsm["spatial_aligned"]` (and/or updated `adata.obsm["spatial"]` with raw archived in `adata.obsm["spatial_raw"]`): aligned `(n_obs, 2)` coordinates in a shared coordinate frame.
- **Registration Summary Table** (`tables/registration_summary.csv`):
  - Columns: `slice_id`, `n_spots`, `centroid_shift`, `rmsd`.
- **Verification**:
  - Confirm that inter-slice centroid offset and transport-weighted RMSD decrease after alignment and that `adata.obsm["spatial_aligned"]` contains zero `NaN` values.

## 4. Gotchas & Troubleshooting

1. **PASTE KL Divergence Requires Strictly Non-Negative Expression**:
   - Passing zero-centered, $z$-scored `sc.pp.scale` matrices to `pst.pairwise_align(..., dissimilarity="kl")` produces `NaN` transport matrices because KL divergence is undefined on negative numbers. Pass non-negative log1p `adata.X` or raw counts, or use `dissimilarity="euclidean"` on PCA embeddings (`use_rep="X_pca"`).
2. **Choosing `alpha` in PASTE (`alpha=0.1` Default)**:
   - `alpha` weights spatial distance vs. transcriptional dissimilarity: `alpha=0.0` matches spots purely by gene expression, while `alpha=1.0` aligns purely by geometric point-cloud shape (Gromov-Wasserstein). Keep `alpha` in `0.05–0.2` so anatomical expression domains anchor symmetries (e.g., nearly circular tissue sections).
3. **`STalign` Coordinate Ordering `[y, x]` (Row-Column) vs. Scanpy `[x, y]`**:
   - `STalign.LDDMM` and `transform_points_source_to_target` operate in image matrix convention `[y, x]` (`[row, col]`), whereas `adata.obsm["spatial"]` stores `[x, y]`. Always swap columns when passing points to and from `STalign`.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `paste-bio`, `POT`, `scanpy`, `scikit-learn`, `scipy`, `seaborn`, `STalign`, `torch`

The CLI dependency list covers both methods; install only the chosen backend.
