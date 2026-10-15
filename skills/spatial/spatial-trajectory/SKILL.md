---
name: spatial-trajectory
description: Load when inferring pseudotime / lineage trajectories on a preprocessed spatial AnnData
  via DPT (default), CellRank, or Palantir. Skip when wanting velocity-driven dynamics from
  spliced/unspliced layers (use spatial-velocity); non-spatial scRNA pseudotime (use
  sc-pseudotime).
trigger: trajectory, pseudotime, diffusion pseudotime, DPT, CellRank, Palantir, cell fate, lineage
tags:
- spatial
- trajectory
- pseudotime
- dpt
- cellrank
- palantir
- lineage
---

# Spatial Trajectory & Pseudotime Inference (`spatial-trajectory`)

## 1. Method Decision Matrix & When to Use

Use this playbook to infer continuous differentiation trajectories, pseudotime gradients, and lineage branch probabilities across tissue space (e.g., intestinal crypt-to-villus axis, cortical layer neurogenesis, or tumor invasive front EMT). When spliced/unspliced counts are available for kinetic vector fields, pair with `spatial-velocity`.

| Method (`method=`) | Upstream Package | Mathematical Model | Requires Explicit Root (`iroot`)? | Best Used When |
|---|---|---|---|---|
| **Diffusion Pseudotime** (`dpt`) | `scanpy.tl.diffmap` + `scanpy.tl.dpt` | Random-walk transition matrix eigen-decomposition across multiscale diffusion steps | Yes (`adata.uns["iroot"]`) | Default robust ordering along a continuous developmental or spatial gradient |
| **CellRank** (`cellrank`) | `cellrank` (`ConnectivityKernel` / `CytoTRACEKernel` + `GPCCA`) | Markov chain coarse-graining into initial/terminal macrostates & absorption probabilities | Optional (`CytoTRACEKernel` estimates potency automatically) | Resolving multiple terminal fates and lineage commitment probabilities |
| **Palantir** (`palantir`) | `palantir` (`palantir.core.run_palantir`) | Shortest-path waypoint Markov chain over diffusion components | Yes (`early_cell` barcode) | Quantifying per-cell differentiation entropy (plasticity) and smooth branch probabilities |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Diffusion Pseudotime (`scanpy.tl.dpt`) Projected onto Spatial Coordinates

```python
import numpy as np
import scanpy as sc

# 1. Select biologically grounded root cell (e.g., highest stem/progenitor marker expression inside stem niche)
stem_mask = (adata.obs["cell_type"] == "Stem_Progenitor").to_numpy()
candidate_indices = np.flatnonzero(stem_mask) if np.any(stem_mask) else np.arange(adata.n_obs)
# Pick medoid of the progenitor cluster in PCA space as stable iroot
pca_sub = adata.obsm["X_pca"][candidate_indices]
medoid_offset = int(np.argmin(np.linalg.norm(pca_sub - pca_sub.mean(axis=0), axis=1)))
adata.uns["iroot"] = int(candidate_indices[medoid_offset])

# 2. Compute diffusion map and Diffusion Pseudotime (DPT)
if "connectivities" not in adata.obsp:
    sc.pp.neighbors(adata, n_neighbors=15, use_rep="X_pca", random_state=0)
sc.tl.diffmap(adata, n_comps=15, random_state=0)
sc.tl.dpt(adata, n_dcs=10)
```

### Pattern B: Terminal Macrostates & Fate Probabilities (`cellrank`)

```python
import cellrank as cr

# Combine transcriptomic connectivity kernel (or CytoTRACE / Velocity kernel)
ck = cr.kernels.ConnectivityKernel(adata).compute_transition_matrix()
g = cr.estimators.GPCCA(ck)
g.compute_schur(n_components=10)
g.compute_macrostates(n_states=3, cluster_key="cell_type")
g.predict_terminal_states()
g.compute_fate_probabilities()
# Fate probabilities are stored in adata.obsm["lineages_fwd"]
```

### Pattern C: Waypoint Branch Probabilities & Differentiation Entropy (`palantir`)

```python
import pandas as pd
import palantir

# Run diffusion maps and Palantir from explicit early_cell barcode
early_cell = str(adata.obs_names[adata.uns["iroot"]])
dm_res = palantir.utils.run_diffusion_maps(pd.DataFrame(adata.obsm["X_pca"], index=adata.obs_names), n_components=10)
ms_data = palantir.utils.determine_multiscale_space(dm_res)
pr_res = palantir.core.run_palantir(ms_data, early_cell=early_cell, num_waypoints=min(500, adata.n_obs - 1))

adata.obs["palantir_pseudotime"] = pr_res.pseudotime
adata.obs["palantir_entropy"] = pr_res.entropy
adata.obsm["palantir_branch_probs"] = pr_res.branch_probs
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-trajectory")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `run_dpt_spatial(adata, root_key=None, root_val=None, root_idx=0, n_dcs=10)`
- `pseudotime_summary(adata, groupby="leiden", pt_key="dpt_pseudotime")`

## 3. Output Format & Verification Contract

- **Trajectory-Annotated Spatial AnnData** (`intermediate/trajectory.h5ad`):
  - `adata.obs["dpt_pseudotime"]` (or `palantir_pseudotime`): continuous pseudotime in $[0, 1]$.
  - `adata.obsm["X_diffmap"]`: diffusion map eigenvectors.
  - `adata.obsm["lineages_fwd"]` or `adata.obsm["palantir_branch_probs"]`: per-cell lineage absorption probabilities summing to $1.0$.
- **Pseudotime Summary Table** (`tables/pseudotime_summary.csv`):
  - Columns: `group`, `n_spots`, `mean_pseudotime`, `median_pseudotime`.
- **Verification**:
  - Verify `adata.obs["dpt_pseudotime"].iloc[adata.uns["iroot"]] == 0.0` and confirm that disconnected components (`np.isinf(adata.obs["dpt_pseudotime"])`) are either zero or masked out.

## 4. Gotchas & Troubleshooting

1. **Unrelated Lineages in Whole-Tissue Spatial Slides (`inf` Pseudotime)**:
   - Unlike FACS-sorted scRNA-seq, a whole-tissue spatial slide contains unrelated lineages (e.g., endothelial cells, B cells, neurons, and epithelial cells simultaneously). Running `sc.tl.dpt` across all cell types produces disconnected graph components with `dpt_pseudotime = inf`. Subset `adata` to the developmental lineage of interest (e.g., epithelial + progenitor compartments) and rebuild `sc.pp.neighbors` before computing `diffmap` and `dpt`.
2. **Arbitrary `iroot = 0` Inverts Trajectory Direction**:
   - `sc.tl.dpt` measures diffusion distance from `adata.uns["iroot"]`. Setting `iroot = 0` without checking whether observation `0` is a progenitor vs. terminally differentiated cell frequently inverts pseudotime ($0 \to 1$ runs backward). Always anchor `iroot` using stem markers (`LGR5`, `SOX2`, `CD34`), `CytoTRACE` gene-count potency, or anatomical crypt/basal coordinates.
3. **Scanpy Diffusion Component 0 (`X_diffmap[:, 0]`)**:
   - In `scanpy >= 1.8`, `adata.obsm["X_diffmap"][:, 0]` is the trivial steady-state eigenvector (constant). Meaningful trajectory axes start at `X_diffmap[:, 1]` and `X_diffmap[:, 2]`.

## Dependencies

`anndata`, `cellrank`, `matplotlib`, `numpy`, `palantir`, `pandas`, `scanpy`, `scipy`, `scvelo`, `seaborn`, `statsmodels`
