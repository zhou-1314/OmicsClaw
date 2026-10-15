---
name: sc-perturb
description: Load when classifying perturbed vs non-perturbed cells in a Perturb-seq / CRISPR-screen
  AnnData via the pertpy Mixscape workflow. Skip when guide labels are not yet attached (use
  sc-perturb-prep); in-silico KO predictions on unperturbed data (use
  sc-in-silico-perturbation).
tags:
- singlecell
- scrna
- perturbation
- perturb-seq
- crispr
- mixscape
- pertpy
---

# Single-Cell CRISPR / Perturb-seq Perturbation Signature & Mixscape Analysis (`sc-perturb`)

## 1. Method Decision Matrix & When to Use

Use this skill to compute local perturbation signatures, separate genuinely perturbed (`KO` / `OE`) cells from escaped non-perturbed (`NP`) cells that received a guide RNA without exhibiting a transcriptomic phenotype, and quantify perturbation strength on an AnnData that already has guide/target labels (otherwise run `sc-perturb-prep` first).

| Analytical Goal | Recommended Method | Upstream Library | Key Assumptions |
|---|---|---|---|
| **Separate true KO/OE responders from escaped (`NP`) cells** | **Mixscape** (`mixscape`) | `pertpy.tl.Mixscape` (`perturbation_signature` + `mixscape`) | Normalized `adata.X`, `X_pca`, target column (`pert_key`), and non-targeting control label (`control="NT"`) |
| **Global distance between perturbation & control distributions** | **Energy Distance (E-test)** | `pertpy.tl.Distance` (`metric="edistance"`) | Latent space (`X_pca` / `X_scVI`) + non-targeting control group |
| **Attach barcode $\to$ sgRNA table to expression AnnData** | **Guide Mapping Prep** | `sc-perturb-prep` | Raw expression + barcode-to-guide TSV/CSV |

> **Decision Rule**: Pooled CRISPR screens routinely contain $20\%\text{--}60\%$ escaped cells (`NP`) due to in-frame indels or inefficient editing. Always run `Mixscape` to filter out `NP` cells before downstream target-level differential expression (`sc-de`).

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Perturbation Signature & Gaussian Mixture Classification (`pertpy.tl.Mixscape`)

```python
import pertpy as pt
import scanpy as sc

# 1. Ensure log-normalized expression in adata.X and PCA embedding in adata.obsm["X_pca"]
if "counts" not in adata.layers:
    adata.layers["counts"] = adata.X.copy()
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
if "X_pca" not in adata.obsm:
    sc.tl.pca(adata, n_comps=min(50, adata.n_obs - 1, adata.n_vars - 1))

# 2. Subtract local non-targeting (NT) control neighborhood signature
ms = pt.tl.Mixscape()
ms.perturbation_signature(
    adata,
    pert_key="perturbation",
    control="NT",
    split_by="replicate",   # Or None to use kNN nearest-neighbor controls across the dataset
    n_neighbors=20,
)

# 3. Classify cells into KO (or OE), NP (non-perturbed), and NT via Gaussian mixture model
ms.mixscape(
    adata,
    labels="perturbation",
    control="NT",
    new_class_name="mixscape_class",
    logfc_threshold=0.25,
    pval_cutoff=0.05,
    perturbation_type="KO",
)
# Outputs: adata.obs["mixscape_class"], adata.obs["mixscape_class_global"], adata.obs["mixscape_class_p_ko"]
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-perturb")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `mixscape(adata, pert_key="perturbation", control="NT", split_by=None, logfc_threshold=0.25, pval_cutoff=0.05, perturbation_type="KO")`
- `class_counts(adata)`
- `global_class_counts(adata)`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/mixscape.h5ad` or `processed.h5ad`):
  - `adata.obs["mixscape_class"]`: target-specific classification (e.g., `"STAT1 KO"`, `"STAT1 NP"`, `"NT"`).
  - `adata.obs["mixscape_class_global"]`: global call (`"KO"`, `"NP"`, `"NT"`).
  - `adata.obs["mixscape_class_p_ko"]` (or `mixscape_class_p_oe`): posterior probability $\in [0, 1]$ of true perturbation.
  - `adata.layers["X_pert"]`: local control-subtracted perturbation signature matrix.
- **Tabular Outputs**:
  - `tables/mixscape_cell_classes.csv`: per-cell perturbation, `mixscape_class`, `mixscape_class_global`, and posterior probability.
  - `tables/mixscape_class_counts.csv`: cell counts per target-specific class.
  - `tables/mixscape_global_class_counts.csv`: cell counts per `global_class` (`KO`, `NP`, `NT`).
- **Verification Invariants**:
  - Assert `control in adata.obs[pert_key].values` before running.
  - Verify `tables/mixscape_global_class_counts.csv` contains $>0$ `KO`/`OE` cells for positive-control targets (an all-`NP` result indicates weak effect size or mislabelled control group).

## 4. Gotchas & Troubleshooting

1. **Missing Control Label (`control="NT"`)**:
   - `Mixscape` requires non-targeting control cells whose label in `adata.obs[pert_key]` exactly matches `control`. Standardize control labels (`"NT"`, `"NTC"`, `"non-targeting"`) via `sc-perturb-prep` before running `sc-perturb`.
2. **`split_by` Behavior in `pertpy`**:
   - Passing `split_by="replicate"` computes perturbation signatures against control cells within each replicate; if the specified `split_by` column is absent or a replicate lacks control cells, pass `split_by=None` so `n_neighbors` nearest-neighbor controls are used globally.
3. **Zero `KO` Calls (`100% NP`) Is Not a Pipeline Crash**:
   - If a target gene has fewer than 5 DE genes passing `logfc_threshold` and `pval_cutoff` against `NT`, Mixscape assigns every cell of that guide to `NP`. Inspect `logfc_threshold` and verify guide on-target knockdown efficiency (`adata[:, target_gene].X`).
4. **Dependency Compatibility (`pertpy` + `statsmodels` + `filelock`)**:
   - `pertpy==1.0.3` imports `filelock` at module load time and expects `statsmodels<=0.14.6`. Ensure both are installed when running full `pertpy` workflows.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `pertpy`, `scanpy`, `scipy`, `scikit-learn`, `filelock`

The Python 3.11 CPU example is verified with `pertpy==1.0.3`,
`scikit-learn==1.7.2`, `anndata==0.11.4`, `statsmodels==0.14.6` and
`filelock==4.0.12`. statsmodels 0.15 removed a private import used by this pertpy
release. It also needs filelock
at import time but omits it from dependency metadata. Its dependencies include
JAX; use the separate extended environment. blitzgsea may need pip's isolated
source build, which the production wheels-only installer does not perform.
