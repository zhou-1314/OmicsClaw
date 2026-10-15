---
name: sc-pseudotime
description: Load when ordering cells along a developmental trajectory in a normalised scRNA AnnData via
  DPT, Palantir, VIA, CellRank, Slingshot (R), or Monocle3 (R). Skip when ranking marker genes per cluster
  (use sc-markers); RNA velocity vector fields (use sc-velocity).
trigger: pseudotime, trajectory, lineage, diffusion pseudotime, palantir, via, cellrank, monocle3, slingshot
tags:
- singlecell
- scrna
- pseudotime
- trajectory
- dpt
- palantir
- via
- cellrank
- slingshot
- monocle3
---

# Single-Cell Trajectory & Pseudotime Ordering (`sc-pseudotime`)

## 1. Method Decision Matrix & When to Use

Use this skill to order single cells along a continuous differentiation or state-transition manifold and identify trajectory-correlated driver genes via Diffusion Pseudotime (`DPT`), `Palantir`, `VIA`, `CellRank`, `Slingshot` (R), or `Monocle3` (R).

| Biological Scenario | Recommended Method | Upstream Library | Root Requirement & Key Outputs |
|---|---|---|---|
| **Single or branching lineage with known root** (fast, deterministic default) | **Diffusion Pseudotime (`dpt`)** | `scanpy.tl.diffmap` + `scanpy.tl.dpt` | Requires `adata.uns["iroot"]`; outputs `adata.obs["dpt_pseudotime"]` |
| **Probabilistic fate commitment & entropy** | **Palantir (`palantir`)** | `palantir.core.run_palantir` | Requires root cell ID; outputs pseudotime, branch probabilities, and differentiation entropy |
| **Complex/disconnected topologies & cyclic trajectories** | **VIA (`via`)** | `pyVIA.core.VIA` | Root cluster or cell; parcel-graph random walk pseudotime + terminal state prediction |
| **Directed Markov fate probabilities** (with CytoTRACE or Velocity kernel) | **CellRank (`cellrank`)** | `cellrank.estimators.GPCCA` | Infers macrostates, initial/terminal states, and per-cell absorption probabilities |
| **Principal curves on low-D embeddings (R)** | **Slingshot / Monocle3** | R `slingshot` / `monocle3` | Cluster-level MST + simultaneous principal curves (`slingshot_r`) or principal graph (`monocle3_r`) |

> **Decision Rule**: Pseudotime measures **manifold geodesic distance** from a user-defined root (`iroot`), **not** physical time or causal directionality. Always justify the choice of `root_cluster` / `root_cell` using canonical progenitor markers or `sc-cytotrace` before running `sc-pseudotime`.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Diffusion Map + PAGA + Diffusion Pseudotime (`scanpy.tl.dpt`)

```python
import numpy as np
import pandas as pd
import scanpy as sc
from scipy import sparse
from scipy.stats import spearmanr

# 1. Build kNN graph on X_pca (never build inference graph on 2D UMAP)
if "X_pca" not in adata.obsm:
    sc.tl.pca(adata, n_comps=50)
sc.pp.neighbors(adata, n_neighbors=15, n_pcs=min(50, adata.obsm["X_pca"].shape[1]), use_rep="X_pca")

# 2. Compute PAGA connectivity and Diffusion Map
sc.tl.paga(adata, groups="leiden")
sc.tl.diffmap(adata, n_comps=10)

# 3. Anchor root cell (iroot) at the medoid of the progenitor cluster in diffusion space
root_cluster = "0"  # Replace with biologically validated stem/progenitor cluster
root_indices = np.flatnonzero(adata.obs["leiden"].astype(str) == str(root_cluster))
dm = adata.obsm["X_diffmap"][root_indices]
adata.uns["iroot"] = int(root_indices[np.argmin(np.linalg.norm(dm - dm.mean(axis=0), axis=1))])

# 4. Run Diffusion Pseudotime and rank trajectory-correlated genes
sc.tl.dpt(adata, n_dcs=10)
adata.obs["pseudotime"] = adata.obs["dpt_pseudotime"]
```

### Pattern B: Probabilistic Fate Branching (`palantir`)

```python
import palantir

# Run diffusion maps and multiscale Palantir trajectory from an explicit start cell barcode
dm_res = palantir.utils.run_diffusion_maps(adata, n_components=10, knn=30, seed=20)
ms_data = palantir.utils.determine_multiscale_space(adata)
pr_res = palantir.core.run_palantir(
    adata,
    early_cell=start_cell_barcode,
    num_waypoints=1200,
    max_iterations=25,
    seed=20,
)
adata.obs["pseudotime"] = pr_res.pseudotime
adata.obsm["trajectory_fate_probabilities"] = pr_res.branch_probs
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-pseudotime")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `pseudotime(adata, method="dpt", cluster_key="leiden", root_cluster=None, root_cell=None, n_neighbors=15, n_dcs=10)`
- `trajectory_genes(adata, n_genes=50, method="pearson")`
- `pseudotime_table(adata, cluster_key="leiden")`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/pseudotime.h5ad` or `processed.h5ad`):
  - `adata.obs["pseudotime"]` (and `adata.obs["dpt_pseudotime"]` for DPT): finite float values in $[0.0, 1.0]$.
  - `adata.uns["iroot"]`: integer positional index of the selected root cell.
  - `adata.obsm["X_diffmap"]`: diffusion components used for trajectory inference.
  - Optional `adata.obsm["trajectory_fate_probabilities"]`: per-cell lineage absorption probabilities (`Palantir` / `CellRank`).
- **Tabular Outputs**:
  - `tables/pseudotime_cells.csv`: `cell`, `group`, `pseudotime`, and 2D embedding coordinates.
  - `tables/trajectory_genes.csv`: `gene`, `correlation`, `pval` ranked by absolute correlation with `pseudotime`.
- **Verification Invariants**:
  - Assert `np.isfinite(adata.obs["pseudotime"]).all()` (any `inf` or `NaN` indicates disconnected graph components).
  - Verify `adata.obs.groupby(cluster_key)["pseudotime"].median()` orders root $\to$ intermediate $\to$ terminal states as expected.

## 4. Gotchas & Troubleshooting

1. **Root Cell Selection (`adata.uns["iroot"]`) Trap**:
   - `sc.tl.dpt` requires `adata.uns["iroot"]` as an **integer positional index** in `0 .. adata.n_obs - 1`, not a cell barcode string! Selecting an outlier cell at the periphery of a cluster distorts DPT; select the cluster medoid in `X_diffmap` or the cell with peak stemness (`sc-cytotrace`).
2. **Disconnected $k$NN Graph Components Yield `inf` Pseudotime**:
   - Cells belonging to a disconnected island (e.g., erythrocytes or platelets in a myeloid trajectory) are unreachable from `iroot` and receive `dpt_pseudotime = inf`. Either subset to the connected lineage of interest or increase `n_neighbors`.
3. **Never Build the Inference Graph on 2D `X_umap`**:
   - UMAP non-linearly compresses and tears global distances. Always construct the $k$NN graph on `X_pca` (or `X_scVI` / `X_pca_harmony`) and reserve `X_umap` strictly for 2D plotting.
4. **Reversed Pseudotime Polarity**:
   - Diffusion eigenvectors have arbitrary sign; only `adata.uns["iroot"]` anchors $t = 0$. If pseudotime decreases along known differentiation markers, your `root_cluster` / `root_cell` was placed at the differentiated terminus instead of the progenitor state.

## Dependencies

`anndata`, `cellrank`, `matplotlib`, `numpy`, `palantir`, `pandas`, `pyVIA`, `scanpy`, `scipy`, `scvelo`, `seaborn`
