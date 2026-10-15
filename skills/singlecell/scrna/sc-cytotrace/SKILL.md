---
name: sc-cytotrace
description: Load when computing per-cell differentiation potency / stemness scores from gene-expression
  complexity on a scRNA AnnData via the CytoTRACE-simple method. Skip when ordering cells along a trajectory
  (use sc-pseudotime); marker-based cell-type labelling (use sc-cell-annotation).
tags:
- singlecell
- scrna
- cytotrace
- potency
- stemness
- differentiation
---

# Single-Cell Differentiation Potency & Stemness Scoring (`sc-cytotrace`)

## 1. Method Decision Matrix & When to Use

Use this skill to estimate per-cell developmental potency (stemness vs. terminal differentiation) from transcriptional diversity and $k$NN-smoothed gene-count correlation without requiring an a priori root cell.

| Analytical Scenario | Recommended Method | Upstream Library / Implementation | Input Matrix Assumptions |
|---|---|---|---|
| **Unsupervised stemness / root-state ranking** | **CytoTRACE-simple** (detected-gene rank + $k$NN smoothing) | `scanpy` + `scipy.stats.rankdata` (`kernel.py`) | Unscaled `layers["counts"]` or unscaled log1p `adata.X` (strictly non-negative, uncentered) |
| **Potency-driven Markov transition matrix** | **CellRank CytoTRACEKernel** | `cellrank.kernels.CytoTRACEKernel` | `scvelo.pp.moments` (`layers["Ms"]`) + $k$NN connectivities |
| **Root-anchored trajectory pseudotime** | **Diffusion Pseudotime / Palantir** | `scanpy.tl.dpt` / `palantir` (use `sc-pseudotime`) | Normalized `adata.X` + explicit `iroot` / root cell |

> **Decision Rule**: Use `sc-cytotrace` to identify candidate progenitor clusters (`cytotrace_score` near $1.0$) before choosing `iroot` in `sc-pseudotime`. Never compute gene-detection counts on mean-centered or z-scored `adata.X`.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: CytoTRACE-Simple Potency Proxy (`scanpy` + `scipy`)

```python
import numpy as np
import pandas as pd
import scanpy as sc
from scipy import sparse
from scipy.stats import rankdata

# 1. Extract unscaled expression matrix (prefer raw integer counts or unscaled log1p)
mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
if sparse.issparse(mat):
    gene_counts = np.asarray((mat > 0).sum(axis=1)).ravel().astype(float)
else:
    gene_counts = np.asarray((mat > 0).sum(axis=1), dtype=float).ravel()

# 2. Ensure kNN graph exists for neighborhood smoothing
if "connectivities" not in adata.obsp:
    if "X_pca" not in adata.obsm:
        sc.tl.pca(adata, n_comps=min(30, adata.n_obs - 1, adata.n_vars - 1))
    sc.pp.neighbors(adata, n_neighbors=30)

# 3. Rank-normalize transcriptional complexity and smooth over kNN connectivities
ranks = (rankdata(gene_counts, method="average") - 1.0) / max(adata.n_obs - 1, 1)
conn = adata.obsp["connectivities"].tocsr()
row_sums = np.maximum(np.asarray(conn.sum(axis=1)).ravel(), 1e-12)
smoothed = (np.asarray(conn.dot(ranks)).ravel() / row_sums) + 0.5 * ranks
scores = (rankdata(smoothed, method="average") - 1.0) / max(adata.n_obs - 1, 1)

# 4. Annotate per-cell scores and six relative potency quantile bins
bins = ["Differentiated", "Unipotent", "Oligopotent", "Multipotent", "Pluripotent", "Totipotent"]
adata.obs["cytotrace_gene_count"] = gene_counts.astype(int)
adata.obs["cytotrace_score"] = scores
adata.obs["cytotrace_potency"] = pd.Categorical(
    pd.cut(scores, bins=np.linspace(0.0, 1.0, 7), labels=bins, include_lowest=True),
    categories=bins,
    ordered=True,
)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-cytotrace")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `cytotrace(adata, n_neighbors=30, layer=None)`
- `potency_table(adata)`
- `potency_composition(adata)`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/cytotrace.h5ad` or `processed.h5ad`):
  - `adata.obs["cytotrace_score"]`: float in $[0.0, 1.0]$ ($1.0 =$ highest relative potency/stemness, $0.0 =$ most differentiated).
  - `adata.obs["cytotrace_gene_count"]`: integer count of detected genes ($>0$) per cell.
  - `adata.obs["cytotrace_potency"]`: ordered categorical across 6 quantile bins (`Differentiated` $\to$ `Totipotent`).
- **Tabular Outputs** (`tables/cytotrace_scores.csv`):
  - Indexed by cell ID with columns `cytotrace_score`, `cytotrace_potency`, `cytotrace_gene_count`.
- **Verification Invariants**:
  - Assert `adata.obs["cytotrace_score"].between(0.0, 1.0).all()` with zero `NaN` entries.
  - Confirm positive Spearman rank correlation ($> 0.5$) between `cytotrace_score` and `cytotrace_gene_count`.

## 4. Gotchas & Troubleshooting

1. **Centered / Z-Scored `adata.X` Trap**:
   - After `sc.pp.scale(adata)`, roughly half of all matrix entries are $>0$ due to zero-centering, destroying the biological link between `(X > 0).sum(axis=1)` and detected gene count. Always pass `layer="counts"` or an unscaled log-normalized matrix.
2. **Relative Quantile Bins vs. Biological Totipotency**:
   - The second rank transform distributes `cytotrace_score` uniformly across $[0, 1]$, making the six `cytotrace_potency` categories (`Differentiated` through `Totipotent`) roughly equal-sized relative bins. `"Totipotent"` denotes the top quantile in your dataset, not embryonic totipotency.
3. **Existing `connectivities` Graph Reuse**:
   - When `adata.obsp["connectivities"]` is already present, `n_neighbors` is ignored and the existing graph is reused. Verify that the graph was constructed on a batch-corrected embedding (`X_pca_harmony` or `X_scVI`) in multi-sample cohorts.
4. **Cell-Cycle & Doublet Confounding**:
   - Undetected doublets and proliferating G2/M cells express significantly more genes per cell. Run `sc-doublet-detection` and inspect cell-cycle phase scores (`S_score`, `G2M_score`) before interpreting high `cytotrace_score` as stemness.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`
