---
name: sc-markers
description: Load when ranking cluster-level marker genes from a clustered single-cell AnnData via Scanpy
  Wilcoxon / t-test / logreg or COSG specificity. Skip when comparing condition-vs-control with replicates
  (use sc-de); assigning cell-type labels (use sc-cell-annotation).
tags:
- singlecell
- scrna
- markers
- cluster-markers
- annotation
- differential-expression
- cosg
---

# Single-Cell Cluster Marker Gene Ranking & Specificity Scoring (`sc-markers`)

## 1. Method Decision Matrix & When to Use

Select a cluster marker discovery algorithm based on cluster count, dataset size, and whether statistical $p$-values or pure expression specificity are needed:

| Method (`--method` / `method=`) | Upstream Package | Ranking Criterion | Outputs $p$-Values? | Best Used When |
|---|---|---|---|---|
| **Wilcoxon Rank-Sum** (`method="wilcoxon"`) | `scanpy.tl.rank_genes_groups` | Non-parametric Mann-Whitney U test Z-score + BH `pvals_adj` | Yes (`pvals`, `pvals_adj`) | **Default general-purpose marker test**; robust to outliers and skewed single-cell distributions |
| **COSG Specificity** (`method="cosg"`) | `cosg` (`cosg.cosg`) | Cosine similarity in cell-expression space with specificity penalty $\mu$ (`mu=1.0`) | No (`scores` only) | Identifying highly selective cell-type markers that are strictly silent in off-target clusters; scales to $>10^6$ cells |
| **Welch's $t$-test** (`method="t-test"`) | `scanpy.tl.rank_genes_groups` | Welch's variance-adjusted two-sample $t$-statistic | Yes (`pvals`, `pvals_adj`) | Fast exploratory marker scan on very large atlases |
| **Logistic Regression** (`method="logreg"`) | `scanpy.tl.rank_genes_groups` (`sklearn`) | Multivariate $L_2$-penalized classification coefficient | No (`scores` only) | Selecting a minimal, non-redundant gene panel across $\ge 3$ clusters |

> **Scope Distinction**: Use `sc-markers` for **one-vs-rest cluster characterization** (`leiden`, `cell_type`). For **multi-donor condition-vs-control differential expression**, use `sc-de` (pseudobulk `pydeseq2`).

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Scanpy Wilcoxon Ranking with Expression-Fraction Filtering

```python
import scanpy as sc
import pandas as pd

# 1. Rank genes per cluster on log1p-normalized expression (always set pts=True and use_raw=False)
sc.tl.rank_genes_groups(
    adata,
    groupby="leiden",
    method="wilcoxon",
    pts=True,          # Populates pct_nz_group and pct_nz_reference
    use_raw=False,
)

# 2. Filter for specific markers (expressed in >=25% of target cluster, <=50% of rest, log2FC >= 0.25)
sc.tl.filter_rank_genes_groups(
    adata,
    min_in_group_fraction=0.25,
    min_fold_change=0.25,
    max_out_group_fraction=0.50,
)

markers_all = sc.get.rank_genes_groups_df(adata, group=None)
markers_top10 = (
    markers_all.dropna(subset=["names"])
    .query("pvals_adj < 0.05 and logfoldchanges >= 0.25")
    .groupby("group", observed=True)
    .head(10)
)
```

### Pattern B: High-Specificity Marker Discovery with `COSG`

```python
import cosg
import scanpy as sc

cosg.cosg(
    adata,
    key_added="cosg",
    mu=1.0,                        # Specificity penalty (higher mu penalizes expression in other clusters)
    expressed_pct=0.1,
    remove_lowly_expressed=True,
    n_genes_user=50,
    groupby="leiden",
    use_raw=False,
)
cosg_markers = sc.get.rank_genes_groups_df(adata, key="cosg", group=None)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-markers")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `find_markers(adata, groupby="leiden", method="wilcoxon", min_in_group_fraction=0.25, min_fold_change=0.25, max_out_group_fraction=0.5)`
- `top_markers(markers_df, n_top=10)`
- `cluster_marker_summary(markers_df, fdr=0.05)`

## 3. Output Format & Verification Contract

- **Marker Tables** (`tables/markers_all.csv`, `tables/markers_top.csv`, `tables/cluster_summary.csv`):
  - `markers_all.csv` / `markers_top.csv` columns: `group`, `names`, `scores`, `logfoldchanges`, `pvals`, `pvals_adj`, `pct_nz_group`, `pct_nz_reference`.
  - `cluster_summary.csv` columns: `group`, `n_markers`, `top_gene`, `max_lfc`.
- **Verification Invariants**:
  - Confirm every cluster in `adata.obs[groupby].unique()` is represented in `markers_top.csv`.
  - Confirm top markers exhibit positive `scores` and `pct_nz_group > pct_nz_reference`.

## 4. Gotchas & Troubleshooting

1. **Never Run `rank_genes_groups` on Zero-Centered `sc.pp.scale` Matrices**:
   - `sc.tl.rank_genes_groups` computes `logfoldchanges` assuming `adata.X` holds non-negative $\log_e(1 + \text{CP10K})$ expression. Running on `sc.pp.scale`d matrices (which contain negative z-scores) produces `NaN` log-fold changes and corrupts `pts` (`pct_nz_group` counts any z-score $> 0$ as "expressed"). If `adata.X` was scaled, pass `layer="log1p"` or restore `adata = adata.raw.to_adata()`.
2. **`filter_rank_genes_groups` Replaces Rejected Genes with `NaN` Names**:
   - `sc.tl.filter_rank_genes_groups` does not shorten the arrays in `adata.uns["rank_genes_groups_filtered"]`; instead it replaces filtered-out gene names with `NaN`. Always call `.dropna(subset=["names"])` on the resulting DataFrame.
3. **`COSG` and `logreg` Produce Specificity Scores Without $p$-Values**:
   - Neither `cosg` nor `method="logreg"` computes frequentist $p$-values (`pvals` and `pvals_adj` are `NaN` or absent). Sort and threshold their outputs by `scores` rather than `pvals_adj < 0.05`.
4. **Two-Group `method="logreg"` Shape Quirk in Scanpy**:
   - In binary classification ($2$ groups), `sklearn.linear_model.LogisticRegression.coef_` has shape `(1, n_genes)` rather than `(2, n_genes)`, causing `sc.get.rank_genes_groups_df(adata, group=None)` to omit the second group. Use `method="wilcoxon"` or `cosg` when comparing exactly two groups.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scikit-learn`, `scipy`, `seaborn`
