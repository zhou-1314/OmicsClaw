---
name: spatial-velocity
description: Load when estimating RNA velocity on a spatial AnnData with `layers["spliced"]` +
  `layers["unspliced"]` via scVelo (stochastic / deterministic / dynamical) or veloVI. Skip when
  input lacks the spliced/unspliced layers (quantify upstream first); non-spatial scRNA velocity
  (use sc-velocity).
trigger: RNA velocity, cellular dynamics, scVelo, VELOVI, latent time, spliced unspliced
tags:
- spatial
- velocity
- rna-velocity
- scvelo
- velovi
- dynamics
---

# Spatial RNA Velocity & Cellular Dynamics (`spatial-velocity`)

## 1. Method Decision Matrix & When to Use

Use this playbook to estimate directional cellular dynamics (`velocity`), transcriptional kinetic rates, and `latent_time` from `adata.layers["spliced"]` and `adata.layers["unspliced"]`, and project velocity vector fields directly onto 2D tissue space (`adata.obsm["spatial"]`):

| Method (`mode=` / `method=`) | Upstream Package | Splicing Kinetic Model | Estimates `latent_time`? | Best Used When |
|---|---|---|---|---|
| **Stochastic Velocity** (`mode="stochastic"`) | `scvelo.tl.velocity(mode="stochastic")` | Steady-state 2nd-order moments of spliced/unspliced covariance | No (use velocity pseudotime) | Fast default screening of directional transitions on spatial count datasets |
| **Dynamical Velocity** (`mode="dynamical"`) | `scvelo.tl.recover_dynamics` + `scvelo.tl.velocity(mode="dynamical")` | Full EM likelihood over transcription ($\alpha$), splicing ($\beta$), and degradation ($\gamma$) rates | Yes (`scv.tl.latent_time`) | Resolving transient induction/repression states and shared universal latent time |
| **veloVI** (`velovi`) | `velovi` (`VELOVI` in `scvi-tools`) | Deep variational autoencoder modeling gene-specific splicing states end-to-end | Yes (posterior latent time + uncertainty) | Noisy/sparse spatial UMI capture or evaluating velocity uncertainty per cell |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `scvelo` Dynamical / Stochastic Velocity Projected onto Tissue Space

```python
import scvelo as scv

# 1. Verify spliced and unspliced layers exist
assert "spliced" in adata.layers and "unspliced" in adata.layers, "Requires spliced & unspliced layers"

# 2. Filter genes, normalize, and compute PCA-knn first/second-order moments
scv.pp.filter_and_normalize(adata, min_shared_counts=20, n_top_genes=2000)
scv.pp.moments(adata, n_pcs=30, n_neighbors=30)

# 3. Fit dynamical splicing kinetics (or skip recover_dynamics and use mode="stochastic")
scv.tl.recover_dynamics(adata, n_jobs=1)
scv.tl.velocity(adata, mode="dynamical")
scv.tl.velocity_graph(adata, n_jobs=1)

# 4. Compute velocity length, coherence confidence, latent time, and project onto spatial coordinates
scv.tl.velocity_confidence(adata)
scv.tl.latent_time(adata)
scv.tl.velocity_embedding(adata, basis="spatial")
# Visualize on tissue coordinates with: scv.pl.velocity_embedding_stream(adata, basis="spatial", color="cell_type")
```

### Pattern B: Deep Generative Velocity (`velovi`)

```python
import scvelo as scv
from velovi import preprocess_data, VELOVI

scv.pp.filter_and_normalize(adata, min_shared_counts=20, n_top_genes=2000)
scv.pp.moments(adata, n_pcs=30, n_neighbors=30)
adata = preprocess_data(adata)

VELOVI.setup_anndata(adata, spliced_layer="Ms", unspliced_layer="Mu")
vae = VELOVI(adata)
vae.train(max_epochs=250)

adata.obsm["velocity_velovi"] = vae.get_velocity(n_samples=25, velo_statistic="mean")
adata.obs["latent_time_velovi"] = vae.get_latent_time(n_samples=25).mean(axis=1).to_numpy()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-velocity")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `run_scvelo_spatial(adata, mode="stochastic", min_shared_counts=20, n_top_genes=2000, n_pcs=30, n_neighbors=30)`
- `velocity_summary(adata, groupby="leiden")`

## 3. Output Format & Verification Contract

- **Velocity-Annotated Spatial AnnData** (`intermediate/velocity.h5ad`):
  - `adata.layers["velocity"]`: gene-by-cell RNA velocity matrix.
  - `adata.obs["velocity_length"]` and `adata.obs["velocity_confidence"]`: speed and local vector coherence in $[0, 1]$.
  - `adata.obs["latent_time"]`: universal internal clock in $[0, 1]$ (when `mode="dynamical"` or `velovi`).
  - `adata.obsm["velocity_spatial"]`: 2D velocity displacement vectors aligned with `adata.obsm["spatial"]`.
- **Velocity Summary Table** (`tables/velocity_summary.csv`):
  - Columns: `group` (`leiden` / `cell_type`), `velocity_length`, `velocity_confidence`, `latent_time`.
- **Verification**:
  - Confirm `adata.obsm["velocity_spatial"].shape == adata.obsm["spatial"].shape` and verify mean `velocity_confidence > 0.5` in active differentiation zones.

## 4. Gotchas & Troubleshooting

1. **Missing `spliced` / `unspliced` Layers in Standard SpaceRanger & Probe Spatial Data**:
   - Standard 10x `spaceranger count` and probe-hybridization spatial platforms (Xenium, CosMx, MERFISH, Visium CytAssist FFPE) do **not** quantify intronic unspliced transcripts. `spatial-velocity` applies only to poly-A capture spatial sequencing (fresh-frozen Visium, Slide-seq, Stereo-seq) processed through `velocyto`, `STARsolo --soloFeatures Velocyto`, or `kb-python --workflow lamanno`. For probe-based spatial datasets, use `spatial-trajectory` instead.
2. **Spatial Streamlines vs. Physical Cell Migration**:
   - `scv.tl.velocity_embedding(adata, basis="spatial")` projects transcriptional state transitions onto the directions of neighboring spots with similar future expression profiles. It visualizes **transcriptional maturation gradients across tissue anatomy**, not literal physical cell locomotion velocity.
3. **Visium Multi-Cell Spot Admixture Blurs Phase Portraits**:
   - When a $55\ \mu\text{m}$ Visium spot mixes multiple unrelated lineages, spliced/unspliced phase portraits become noisy superpositions. Restrict `spatial-velocity` to lineage-homogeneous domains or single-cell resolution spatial capture arrays.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `scvelo`, `scvi-tools`, `seaborn`, `torch`, `velovi`
