---
name: spatial-deconv
description: Load when deconvolving spot-level cell-type proportions on a Visium-style AnnData using a
  labelled scRNA reference (FlashDeconv / Cell2location / RCTD / DestVI / Tangram). Skip when
  each spot is a single cell (Xenium / MERFISH) (use spatial-annotate); tissue-domain detection
  (use spatial-domains).
trigger: cell type deconvolution, spatial deconvolution, cell proportion, cell type proportion, cell2location, RCTD, DestVI, Stereoscope, Tangram, SPOTlight, CARD
tags:
- spatial
- deconvolution
- cell2location
- rctd
- destvi
- stereoscope
- tangram
- spotlight
- card
- flashdeconv
---

# Spatial Multi-Cell Spot Deconvolution (`spatial-deconv`)

## 1. Method Decision Matrix & When to Use

Use this playbook to estimate per-spot cell-type proportions or cell abundances on multi-cell-per-spot spatial platforms (10x Visium, Slide-seqV2, ST arrays) using an annotated single-cell RNA-seq (`scRNA-seq`) reference. For single-cell resolution spatial technologies (Xenium, MERFISH, CosMx), use `spatial-annotate` instead.

| Method (`method=`) | Upstream Package | Count Model & Input | Speed / Hardware | Best Used When |
|---|---|---|---|---|
| **Cell2location** (`cell2location`) | `cell2location` (`scvi-tools`) | Hierarchical Negative Binomial on raw integer counts | GPU recommended (2-stage VI) | Gold-standard sensitivity for fine-grained subtypes and absolute cell abundance estimation |
| **FlashDeconv** (`flashdeconv`) | `flashdeconv` | Sketch-regularized linear / NNLS on normalized or count matrices | Fast CPU ($<10\text{ s}$) | Rapid baseline deconvolution on large cohorts without GPU training overhead |
| **Tangram Clusters** (`tangram`) | `tangram` (`mode="clusters"`) | Cosine similarity + density prior on shared markers | Fast CPU / GPU | Projecting reference cluster profiles onto spatial spots with minimal hyperparameter tuning |
| **DestVI** (`destvi`) | `scvi.external.DestVI` (`CondSCVI` + `DestVI`) | Conditional VAE continuous state + discrete proportion | GPU / CPU | Capturing continuous intra-cell-type activation gradients inside spatial spots |
| **RCTD** (`rctd`) | R `spacexr` | Poisson-lognormal mixture (`doublet` or `full` mode) | Multi-core CPU (R) | Slide-seq beads (`doublet` mode) or Visium (`full` mode) with platform-effect normalization |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Two-Stage Bayesian Deconvolution (`cell2location`)

```python
import numpy as np
import pandas as pd
import cell2location as c2l
from cell2location.models import RegressionModel, Cell2location

# 1. Fit reference cell-type signatures on raw scRNA-seq counts
adata_ref = adata_sc.copy()
adata_ref.X = adata_ref.layers["counts"].copy() if "counts" in adata_ref.layers else adata_ref.X
RegressionModel.setup_anndata(adata_ref, labels_key="cell_type")
mod_ref = RegressionModel(adata_ref)
mod_ref.train(max_epochs=250, use_gpu=False)
adata_ref = mod_ref.export_posterior(adata_ref, sample_kwargs={"num_samples": 1000, "batch_size": 2500})
inf_aver = adata_ref.varm["means_per_cluster_mu_fg"][
    [f"means_per_cluster_mu_fg_{c}" for c in adata_ref.uns["mod"]["factor_names"]]
].copy()
inf_aver.columns = adata_ref.uns["mod"]["factor_names"]

# 2. Intersect genes and map cell types onto raw spatial spot counts
shared_genes = np.intersect1d(adata_sp.var_names, inf_aver.index)
adata_sp_sub = adata_sp[:, shared_genes].copy()
adata_sp_sub.X = adata_sp_sub.layers["counts"].copy() if "counts" in adata_sp_sub.layers else adata_sp_sub.X
inf_aver = inf_aver.loc[shared_genes]

Cell2location.setup_anndata(adata_sp_sub)
mod_sp = Cell2location(adata_sp_sub, cell_state_df=inf_aver, N_cells_per_location=8, detection_alpha=20)
mod_sp.train(max_epochs=5000, batch_size=None, train_size=1.0, use_gpu=False)
adata_sp_sub = mod_sp.export_posterior(adata_sp_sub)

# 3. Normalize 5% quantile posterior abundances to spot proportions summing to 1.0
abund = adata_sp_sub.obsm["q05_cell_abundance_w_sf"].copy()
abund.columns = adata_sp_sub.uns["mod"]["factor_names"]
prop_df = abund.div(abund.sum(axis=1).replace(0, 1.0), axis=0)
adata_sp.obsm["deconv_proportions"] = prop_df
```

### Pattern B: Fast Cluster-Level Deconvolution (`tangram`)

```python
import numpy as np
import tangram as tg

shared = list(np.intersect1d(adata_sc.var_names, adata_sp.var_names))
tg.pp_adatas(adata_sc, adata_sp, genes=shared)
ad_map = tg.map_cells_to_space(
    adata_sc,
    adata_sp,
    mode="clusters",
    cluster_label="cell_type",
    density_prior="rna_count_based",
    num_epochs=500,
    device="cpu",
)
tg.project_cell_annotations(ad_map, adata_sp, annotation="cell_type")
prop_df = adata_sp.obsm["tangram_ct_pred"].copy()
prop_df = prop_df.div(prop_df.sum(axis=1).replace(0, 1.0), axis=0)
adata_sp.obsm["deconv_proportions"] = prop_df
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-deconv")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `attach_proportions(adata, prop_df, key_added="deconv_proportions", dominant_key="dominant_cell_type")`
- `deconv_summary(adata, key="deconv_proportions")`

## 3. Output Format & Verification Contract

- **Deconvolved Spatial AnnData** (`intermediate/deconvolved.h5ad`):
  - `adata.obsm["deconv_proportions"]`: `pd.DataFrame` or array of shape `(n_spots, n_cell_types)` where each row is non-negative and sums to $1.0$.
  - `adata.obs["dominant_cell_type"]`: top-proportion cell type per spot.
- **Proportions Table** (`tables/proportions.csv`):
  - Spot-by-cell-type proportion matrix indexed by `adata.obs_names`.
- **Verification**:
  - Assert `np.allclose(prop_df.sum(axis=1), 1.0, atol=1e-3)` and verify zero `NaN` entries.

## 4. Gotchas & Troubleshooting

1. **Count-Based Models Require Unnormalized Integer Counts**:
   - `cell2location`, `DestVI`, `Stereoscope`, and `RCTD` model raw UMI counts via Negative Binomial or Poisson distributions. Passing log-normalized expression causes posterior collapse. Always restore raw counts from `adata.layers["counts"]` before calling `setup_anndata`.
2. **Remove Mitochondrial & High-Abundance Housekeeping Genes Before Deconvolution**:
   - Mitochondrial (`MT-`) and ribosomal (`RPS`/`RPL`) genes account for 15–30% of spot UMIs but carry weak lineage specificity. Exclude them from the shared gene set so cell-type marker genes drive the likelihood.
3. **Setting `N_cells_per_location` and `detection_alpha` in `cell2location`**:
   - Use `N_cells_per_location=8` (5–15 cells) for $55\ \mu\text{m}$ 10x Visium spots and `N_cells_per_location=1`–`2` for $10\ \mu\text{m}$ Slide-seq beads. Set `detection_alpha=20` when within-slide technical sensitivity varies strongly across tissue regions.
4. **Abundance vs. Relative Proportion**:
   - `cell2location` outputs estimated cell counts (`q05_cell_abundance_w_sf`) whose row sums vary by spot cellularity. Divide each row by its row sum when downstream analyses expect relative proportions summing to $1.0$.

## Dependencies

`anndata`, `cell2location`, `flashdeconv`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `scvi-tools`, `seaborn`, `tangram-sc`, `torch`

R backends need Rscript and spacexr (RCTD), SPOTlight or CARD.
