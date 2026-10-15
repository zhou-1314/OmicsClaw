---
name: sc-de
description: Load when finding marker genes per cluster or comparing condition expression in single-cell
  RNA-seq. Skip when the data is bulk (use bulkrna-de); spatial (use spatial-de); cluster-only markers
  without conditions (use sc-markers).
trigger: differential expression, marker genes, de analysis, wilcoxon, pseudo-bulk
tags:
- singlecell
- differential-expression
- markers
- wilcoxon
- deseq2
---

# Single-Cell Differential Expression & Marker Discovery (`sc-de`)

## 1. Method Decision Matrix & When to Use

Select the statistical paradigm based on experimental design and biological replication before writing code:

| Experimental Scenario | Biological Replicates (`sample_key`) | Recommended Method | Upstream Library | Statistical Unit |
|---|---|---|---|---|
| **Multi-sample two-condition DE** (e.g., disease vs. control across patients/donors) | $\ge 3$ per condition | **Pseudobulk Negative Binomial GLM** | `pydeseq2` (`DeseqDataSet` + `DeseqStats`) | **Sample / Donor** (prevents pseudoreplication) |
| **Cluster marker discovery** (1-vs-rest or exploratory cell-type signatures) | $1$ or Exploratory | **Wilcoxon Rank-Sum** (`method="wilcoxon"`) | `scanpy.tl.rank_genes_groups` | **Cell** (fast, non-parametric rank test) |
| **Continuous covariate / nested random effects** | $\ge 2$ per condition with covariates | **Linear Mixed / GLM Hurdle** (`MAST` / `statsmodels`) | `statsmodels` or R `MAST` | **Cell + Donor random effect** |

> **Decision Rule**: Never report cell-level Wilcoxon $p$-values as condition-level differential expression across multi-donor cohorts; cell-level tests treat thousands of cells from the same donor as independent observations and inflate false positives by orders of magnitude.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Exploratory Cluster Marker Ranking (`scanpy`)

```python
import scanpy as sc
import pandas as pd

# Ensure log-normalized expression is in adata.X (or specify layer="log1p")
sc.tl.rank_genes_groups(
    adata,
    groupby="leiden",
    method="wilcoxon",
    pts=True,          # Record fraction of cells expressing each gene (pct_nz_group / pct_nz_reference)
    use_raw=False,
)
markers_df = sc.get.rank_genes_groups_df(adata, group=None)
# Standardized columns: group, names, scores, logfoldchanges, pvals, pvals_adj, pct_nz_group, pct_nz_reference
```

### Pattern B: Multi-Sample Condition DE via Pseudobulk (`pydeseq2`)

```python
import numpy as np
import pandas as pd
from pydeseq2.dds import DeseqDataSet
from pydeseq2.ds import DeseqStats

# 1. Aggregate raw integer counts from adata.layers["counts"] per (donor, cell_type)
raw_mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
pb_rows, pb_meta = {}, {}
for (sample, cond), idx in adata.obs.groupby(["sample_id", "condition"], observed=True).groups.items():
    if len(idx) < 10:  # Drop pseudobulk profiles with < 10 cells
        continue
    sub = raw_mat[adata.obs.index.get_indexer(idx)]
    pb_rows[str(sample)] = np.asarray(sub.sum(axis=0)).ravel().astype(int)
    pb_meta[str(sample)] = {"condition": str(cond), "n_cells": int(len(idx))}

counts_df = pd.DataFrame.from_dict(pb_rows, orient="index", columns=adata.var_names)
meta_df = pd.DataFrame.from_dict(pb_meta, orient="index")

# 2. Filter lowly expressed genes (>= 10 total reads across pseudobulk samples)
counts_df = counts_df.loc[:, counts_df.sum(axis=0) >= 10]

# 3. Fit Negative Binomial GLM and extract Wald test statistics
dds = DeseqDataSet(counts=counts_df, metadata=meta_df, design_factors="condition", quiet=True)
dds.deseq2()
stat_res = DeseqStats(dds, contrast=["condition", "stimulated", "control"], quiet=True)
stat_res.summary()
de_df = stat_res.results_df.reset_index().rename(columns={"index": "gene"})
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-de")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `rank_genes(adata, groupby="leiden", method="wilcoxon")`
- `pseudobulk_aggregate(adata, sample_key="sample_id", group_key="condition", layer="counts")`
- `de_summary(df)`

## 3. Output Format & Verification Contract

- **Primary DE Table** (`tables/de_results.csv` or `tables/markers_all.csv`):
  - For Scanpy marker ranking: `group`, `names` (or `gene`), `scores`, `logfoldchanges`, `pvals`, `pvals_adj`.
  - For Pseudobulk `pydeseq2`: `gene`, `baseMean`, `log2FoldChange`, `lfcSE`, `stat`, `pvalue`, `padj`.
- **Verification Checks**:
  - Confirm `pvals_adj` / `padj` are bounded in $[0, 1]$ with `NaN` handled explicitly before thresholding (`padj < 0.05` and `|log2FoldChange| >= 1.0`).
  - Confirm `n_tested > 0` and verify top upregulated genes match expected biology or cluster expression dotplots.

## 4. Gotchas & Troubleshooting

1. **Matrix Layer Mismatch**:
   - `sc.tl.rank_genes_groups` expects **log-normalized** values (`log1p(CP10K)`). Passing raw counts distorts fold-change calculation in Scanpy.
   - `pydeseq2` strictly requires **unnormalized non-negative integer counts** (`adata.layers["counts"]`). Never sum log-normalized values for pseudobulk.
2. **Scanpy `use_raw` Default Trap**:
   - In `scanpy`, `use_raw=None` defaults to `True` whenever `adata.raw` exists, which may silently run DE on an outdated gene subset or unnormalized `.raw.X`. Always pass `use_raw=False` and specify `layer=` explicitly.
3. **Sparse Matrix Summation**:
   - Summing a `scipy.sparse.csr_matrix` along `axis=0` returns a 2D `np.matrix` of shape `(1, n_genes)`. Always flatten with `np.asarray(sub.sum(axis=0)).ravel()`.
4. **Low-Cell Pseudobulk Groups**:
   - Pseudobulk replicates formed from $<10$ cells exhibit extreme dispersion and destabilize `pydeseq2` size factors (`median-of-ratios`). Filter out sample-celltype combinations with fewer than 10 cells prior to `DeseqDataSet`.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`adjustText`, `anndata`, `matplotlib`, `numpy`, `pandas`, `pydeseq2`, `scanpy`, `scipy`, `seaborn`
