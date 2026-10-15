---
name: spatial-condition
description: Load when comparing conditions on spatial AnnData using biological-sample pseudobulk PyDESeq2 or Wilcoxon, with sample, condition and cluster labels. Skip one-condition per-cluster DE (use spatial-de) and experiments without independent replicates.
trigger: condition comparison, pseudobulk, DESeq2, treatment vs control
tags:
- spatial
- condition
- pseudobulk
- differential-expression
---

# Multi-Sample Spatial Condition Comparison (`spatial-condition`)

## 1. Method Decision Matrix & When to Use

Use this playbook to test differential expression **between biological conditions** (e.g., Treated vs. Untreated, Disease vs. Healthy) across multi-slide / multi-patient spatial transcriptomics cohorts. For comparing spatial clusters on a single slide, use `spatial-de`.

| Experimental Design | Replicates per Condition | Recommended Method | Upstream Library | Statistical Unit |
|---|---|---|---|---|
| **Multi-Patient / Multi-Slide Cohort** (overall or within each spatial domain) | $\ge 3$ biological samples (`sample_key`) | **Pseudobulk NB-GLM** (`method="pydeseq2"`) | `pydeseq2` (`DeseqDataSet` + `DeseqStats`) | Biological sample $(N = \text{slides/patients})$ |
| **Multi-Slide with Batch Covariates** (e.g., paired patient or sequencing batch) | $\ge 3$ biological samples | **Multi-Factor Pseudobulk** (`design_factors=["batch", "condition"]`) | `pydeseq2` | Biological sample adjusted for batch |
| **Exploratory / Low-Replicate Screen** | $< 3$ biological samples | **Within-Domain Wilcoxon** (`method="wilcoxon"`) | `scanpy.tl.rank_genes_groups` | Spot/cell (descriptive ranking only; $p$-values inflated) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Domain-Stratified Sample Pseudobulk DE (`pydeseq2`)

```python
import numpy as np
import pandas as pd
from pydeseq2.dds import DeseqDataSet
from pydeseq2.ds import DeseqStats

# 1. Aggregate raw integer counts in adata.layers["counts"] per (sample_id, spatial_domain)
raw_mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
all_results = []

for domain, dom_obs in adata.obs.groupby("spatial_domain", observed=True):
    rows, meta = {}, {}
    for (sample_id, cond), idx in dom_obs.groupby(["sample_id", "condition"], observed=True).groups.items():
        if len(idx) < 10:  # Require >=10 spots per (sample, domain)
            continue
        sub = raw_mat[adata.obs.index.get_indexer(idx)]
        rows[str(sample_id)] = np.asarray(sub.sum(axis=0)).ravel().astype(int)
        meta[str(sample_id)] = {"condition": str(cond), "n_spots": int(len(idx))}

    meta_df = pd.DataFrame.from_dict(meta, orient="index")
    if meta_df["condition"].nunique() < 2 or meta_df["condition"].value_counts().min() < 2:
        continue

    counts_df = pd.DataFrame.from_dict(rows, orient="index", columns=adata.var_names)
    counts_df = counts_df.loc[:, counts_df.sum(axis=0) >= 10]

    dds = DeseqDataSet(counts=counts_df, metadata=meta_df, design_factors="condition", quiet=True)
    dds.deseq2()
    stats_res = DeseqStats(dds, contrast=["condition", "treated", "control"], quiet=True)
    stats_res.summary()
    res = stats_res.results_df.reset_index().rename(columns={"index": "gene"})
    res["spatial_domain"] = str(domain)
    all_results.append(res)

cond_de_df = pd.concat(all_results, ignore_index=True)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-condition")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `pseudobulk_aggregate(adata, sample_key="sample_id", condition_key="condition", cluster_key=None, layer="counts", min_cells=10)`
- `condition_de_summary(df, fdr=0.05, lfc_thresh=0.5)`

## 3. Output Format & Verification Contract

- **Condition DE Results Table** (`tables/condition_de_results.csv`):
  - Columns: `spatial_domain` (or `cluster`), `gene`, `baseMean`, `log2FoldChange`, `lfcSE`, `stat`, `pvalue`, `padj`.
- **Pseudobulk Count Matrix** (`tables/pseudobulk_counts.csv`):
  - Sample-by-gene summed integer count table used for NB-GLM fitting.
- **Verification**:
  - Verify that the degrees of freedom correspond to the number of biological replicates (`n_samples`), not total spots (`adata.n_obs`), and that `counts_df` contains strictly non-negative integers.

## 4. Gotchas & Troubleshooting

1. **Pseudoreplication from Spot-Level Condition Testing**:
   - Running `sc.tl.rank_genes_groups(adata, groupby="condition")` directly on $20,000$ Visium spots from 2 slides treats spots as independent biological replicates, yielding thousands of false-positive $p < 10^{-50}$ driven by slide batch effects and spatial autocorrelation. Always aggregate to `(sample_key, cluster_key)` pseudobulks first.
2. **PyDESeq2 Requires Unnormalized Integer Counts**:
   - Passing log1p-normalized floats from `adata.X` into `DeseqDataSet` raises a `ValueError` or corrupts dispersion estimation. Always sum `adata.layers["counts"]` (or `adata.raw.X` if raw counts were stored there).
3. **Sparse `(sample, domain)` Combinations (`min_cells < 10`)**:
   - A spatial niche that appears in only 2 spots on a slide produces an ultra-noisy pseudobulk vector that distorts size factors (`median-of-ratios`). Filter out `(sample, domain)` pairs with fewer than 10 spots before fitting `DeseqDataSet`.
4. **Hyphens or Spaces in Condition Labels**:
   - `pydeseq2` formula parsing checks factor levels in `design_factors`. Avoid underscores/hyphens that clash with contrast strings; sanitize `meta_df["condition"]` to clean tokens (e.g., `"treated"` vs `"control"`).

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `pydeseq2`, `scanpy`, `scipy`, `seaborn`, `statsmodels`
