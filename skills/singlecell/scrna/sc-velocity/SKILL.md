---
name: sc-velocity
description: Load when computing RNA velocity vectors on scRNA AnnData with spliced / unspliced layers
  via scVelo (stochastic / dynamical); dynamical mode exports latent time. Skip when input lacks
  spliced+unspliced layers (use sc-velocity-prep); trajectory pseudotime ordering (use
  sc-pseudotime).
trigger: rna velocity, velocity, scvelo, spliced unspliced, cellular dynamics, velovi, velocity pseudotime
tags:
- singlecell
- scrna
- velocity
- rna-velocity
- scvelo
- latent-time
- kinetics
---

# Single-Cell RNA Velocity & Latent Time Estimation (`sc-velocity`)

## 1. Method Decision Matrix & When to Use

Use this skill to estimate directed transcriptional dynamics ($\text{d}s/\text{d}t = \beta u - \gamma s$) from `adata.layers["spliced"]` and `adata.layers["unspliced"]` via `scVelo` (`stochastic`, `dynamical`, or `steady_state` mode). If splicing layers are not yet present, run `sc-velocity-prep` first.

| Analytical Goal | Recommended Mode | Upstream Function (`scvelo`) | Kinetic Assumptions & Key Outputs |
|---|---|---|---|
| **Fast default velocity vector field** | **Stochastic Mode (`stochastic`)** | `scv.tl.velocity(adata, mode="stochastic")` | Second-order moment equations ($\text{Cov}[u, s]$, $\text{Var}[s]$); fast, no EM fitting required |
| **Gene-shared latent time & transient states** | **Dynamical Mode (`dynamical`)** | `scv.tl.recover_dynamics` + `scv.tl.velocity(..., mode="dynamical")` + `scv.tl.latent_time` | Likelihood-based EM fit of $(\alpha, \beta, \gamma, t_s)$ per gene; required for `adata.obs["latent_time"]` |
| **Classic linear steady-state ratio** | **Deterministic / Steady-State (`steady_state`)** | `scv.tl.velocity(adata, mode="deterministic")` | Extreme-quantile linear regression $\gamma = u / s$; assumes constant transcription rate |

> **Decision Rule**: Use `mode="stochastic"` for rapid velocity stream visualization and `mode="dynamical"` whenever you need gene-shared `latent_time` or when cell states violate steady-state equilibrium.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Stochastic or Dynamical RNA Velocity Pipeline (`scvelo`)

```python
import scanpy as sc
import scvelo as scv

# 1. Verify spliced and unspliced count layers exist and contain signal
for req in ("spliced", "unspliced"):
    if req not in adata.layers:
        raise ValueError(f"Missing required layer '{req}'; run sc-velocity-prep first.")

# 2. Filter/normalize layers, compute seeded kNN graph, and first/second-order moments
scv.pp.filter_and_normalize(adata, min_shared_counts=20, n_top_genes=2000)
if "X_pca" not in adata.obsm:
    sc.tl.pca(adata, n_comps=30)
sc.pp.neighbors(adata, n_pcs=30, n_neighbors=30, random_state=0)
scv.pp.moments(adata, n_pcs=30, n_neighbors=30)

# 3. Fit velocity model ("stochastic" or "dynamical") and transition graph
mode = "dynamical"  # Or "stochastic"
if mode == "dynamical":
    scv.tl.recover_dynamics(adata, n_jobs=4)
scv.tl.velocity(adata, mode=mode)
scv.tl.velocity_graph(adata, n_jobs=4)
scv.tl.velocity_pseudotime(adata)
if mode == "dynamical":
    scv.tl.latent_time(adata)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-velocity")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `velocity(adata, mode="stochastic", n_jobs=4, random_state=0)`
- `velocity_diagnostics(adata)`
- `top_velocity_genes(adata, n_top=40)`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`processed.h5ad` / `adata_with_velocity.h5ad`):
  - `adata.layers["Ms"]`, `adata.layers["Mu"]`: $k$NN-smoothed spliced/unspliced moments.
  - `adata.layers["velocity"]`: per-cell, per-gene velocity matrix.
  - `adata.uns["velocity_graph"]`: cell-to-cell cosine transition matrix.
  - `adata.obs["velocity_pseudotime"]` and (for `mode="dynamical"` only) `adata.obs["latent_time"]`.
- **Tabular Outputs**:
  - `tables/velocity_summary.csv`: `metric`, `value` summary of mode, cells, genes, and latent time.
  - `tables/velocity_cells.csv`: `cell_id`, UMAP coordinates, velocity norm, `velocity_pseudotime`, and optional `latent_time`.
  - `tables/top_velocity_genes.csv`: `gene`, `mean_abs_velocity`, `mean_velocity` sorted descending.
- **Verification Invariants**:
  - Inspect `velocity_diagnostics(adata)` to confirm `all_zero_velocity == False`, `has_nan_velocity == False`, and `n_velocity_genes > 0`.

## 4. Gotchas & Troubleshooting

1. **Both `layers["spliced"]` and `layers["unspliced"]` Must Be Real Intronic/Exonic Counts**:
   - Never copy `adata.X` into `spliced`/`unspliced` or pass z-scored matrices; identical or scaled layers yield zero phase portraits and degenerate velocity fits. Generate true splicing layers with `sc-velocity-prep`.
2. **In-Place Gene Filtering Modifies `adata.n_vars`**:
   - `scv.pp.filter_and_normalize(adata, ...)` subsets `adata.X` and all aligned layers in place, removing genes with low spliced/unspliced counts. Always work on an explicit `adata.copy()` if you need to retry after a failed fit.
3. **NumPy $\ge 2.1$ Compatibility with `scvelo==0.3.4`**:
   - In `scvelo 0.3.4`, `mode="stochastic"` can raise a 1-element array-to-scalar assignment error on `numpy>=2.4` (tested stable with `numpy==2.0.2`). If encountered, flatten 1D array assignments or use `mode="steady_state"` / `mode="dynamical"`.
4. **Multiple Rate Kinetics (MURK) & Inverted Arrows**:
   - Erythroid maturation and rapid transcriptional bursts violate constant kinetic rate assumptions, producing inverted velocity arrows on UMAP. Check phase portraits (`Ms` vs. `Mu`) of top likelihood genes (`var["fit_likelihood"]`) before trusting stream directions.

## Dependencies

`anndata`, `joblib`, `matplotlib`, `numpy`, `pandas`, `psutil`, `scanpy`, `scikit-learn`, `scipy`, `scvelo`, `seaborn`
