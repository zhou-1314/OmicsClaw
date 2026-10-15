---
name: bulkrna-batch-correction
description: Load when correcting batch effects in bulk expression using R sva ComBat or the legacy Python parametric approximation. Skip single-batch inputs; use sc-batch-integration for single-cell data or spatial-integrate for spatial slices.
trigger: batch correction, ComBat, batch effect, harmonize, multi-cohort, batch removal
tags:
- bulkrna
- batch-correction
- ComBat
- harmonization
- batch-effect
---

# Bulk RNA-seq Batch Correction (`bulkrna-batch-correction`)

## 1. Method Decision Matrix & When to Use

Select the batch harmonization strategy based on data type (continuous normalized expression vs. raw integer counts) and downstream analysis goals:

| Scenario / Input Matrix | Method | Upstream Package | Why |
|---|---|---|---|
| **Normalized continuous expression** ($\log_2(\text{TPM}+1)$ / $\log_2(\text{CPM}+1)$), parametric batch shifts | Empirical Bayes ComBat | `inmoose.pycombat.pycombat_norm` or R `sva::ComBat(par.prior=TRUE)` | Shrinks gene-wise location ($\gamma_{ig}$) and scale ($\delta_{ig}$) batch parameters across genes for small sample sizes |
| **Raw integer gene counts** destined for visualization or clustering | Negative Binomial ComBat-seq | `inmoose.pycombat.pycombat_seq` or R `sva::ComBat_seq` | Preserves integer count properties using a Negative Binomial regression model |
| **Downstream differential expression (`bulkrna-de`)** | Multi-factor GLM design (`~ batch + condition`) | `pydeseq2` (`design_factors=["batch", "condition"]`) or `limma` | Avoids artificially deflating residual degrees of freedom and false-positive inflation caused by pre-correcting counts |
| **Non-parametric or heavy-tailed batch distributions** | Non-parametric ComBat / linear covariate removal | R `sva::ComBat(par.prior=FALSE)` or `limma::removeBatchEffect` | Relaxes Gaussian prior assumptions when gene-level batch effects are multimodal |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `inmoose` (`pyComBat`) on Continuous Log-Normalized Expression

```python
import numpy as np
import pandas as pd
from inmoose.pycombat import pycombat_norm

# 1. Load log2-normalized expression matrix (genes x samples) and sample metadata
expr = pd.read_csv("data/log2_tpm.csv", index_col=0)
meta = pd.read_csv("data/metadata.csv", index_col=0).loc[expr.columns]

# 2. Filter zero-variance genes within any batch before running ComBat
valid_genes = expr.T.groupby(meta["batch"]).std().T.min(axis=1) > 1e-8
expr_filtered = expr.loc[valid_genes]

# 3. Run empirical Bayes batch correction while preserving biological condition covariates
design_cov = pd.get_dummies(meta[["condition"]], drop_first=True)
corrected = pycombat_norm(
    counts=expr_filtered,
    batch=meta["batch"].values,
    covar_mod=design_cov,
)
corrected.to_csv("tables/corrected_expression.csv")
```

### Pattern B: Lightweight Micro-Helper via `use_skill("bulkrna-batch-correction")`

```python
# Calling use_skill("bulkrna-batch-correction") auto-injects kernel.py helpers into the python kernel:
corrected_df = combat_empirical_bayes(
    expr,
    batch=meta["batch"],
    condition=meta.get("condition"),
)
metrics = batch_variance_metrics(expr, corrected_df, batch=meta["batch"])
```

## 3. Output Format & Verification Contract

- **Corrected Expression Matrix** (`tables/corrected_expression.csv` and `intermediate/corrected_expression.parquet`):
  - Indexed by `gene` with identical sample columns as the input matrix; all values must be finite (`np.isfinite(corrected_df.to_numpy()).all()`).
- **Batch Diagnostics Table** (`tables/batch_metrics.csv`):
  - Columns: `metric`, `before_correction`, `after_correction` reporting PC1–PC3 batch $R^2$ (ANOVA variance explained by batch) and batch silhouette score.
- **Verification Invariants**:
  - Sample and gene dimensions are conserved (excluding explicitly logged zero-variance genes).
  - Batch-explained variance ($R^2_{\text{batch}}$ on top PCs) decreases after correction while biological condition separation is retained.

## 4. Gotchas & Troubleshooting

1. **Complete Confounding Between `batch` and `condition`**:
   - If Batch 1 contains only Control samples and Batch 2 contains only Treated samples, batch and biological condition are collinear. Any batch correction (`ComBat` or `removeBatchEffect`) will either fail with a singular design matrix or erase the true biological signal. Always check `pd.crosstab(meta["batch"], meta["condition"])` first.
2. **Never Pass ComBat-Corrected Continuous Values into `DESeq2` / `pydeseq2`**:
   - Standard `ComBat` outputs continuous values (which can be negative) and alters the mean-variance relationship. For differential expression with `pydeseq2`, pass raw unmodified integer counts and include `"batch"` in `design_factors=["batch", "condition"]`.
3. **Zero Within-Batch Variance (`NaN` / Division by Zero)**:
   - Genes that have constant expression (e.g., all zeros) within even a single batch cause empirical Bayes scale estimation ($\hat{\delta}_{ig}$) to divide by zero. Pre-filter genes where within-batch variance is zero (`expr.T.groupby(batch).var().min(axis=0) > 0`).
4. **Single-Sample Batches**:
   - Parametric ComBat cannot estimate within-batch variance $\delta_{ig}$ when a batch has $n_b < 2$ samples unless `mean_only=True` is set.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `scipy`, `sva`, `Matrix`
