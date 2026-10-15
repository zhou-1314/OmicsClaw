---
name: bulkrna-coexpression
description: Load when discovering bulk gene co-expression modules and hub genes with R WGCNA. Skip direct expression contrasts (use bulkrna-de), existing-gene-list PPI lookup (use bulkrna-ppi-network), or single-cell networks (use sc-grn).
trigger: coexpression, WGCNA, gene network, co-expression modules, hub genes, gene modules
tags:
- bulkrna
- coexpression
- WGCNA
- network
- modules
- hub-genes
---

# Bulk RNA-seq Weighted Gene Co-expression Network Analysis (`bulkrna-coexpression`)

## 1. Method Decision Matrix & When to Use

Choose the co-expression network construction strategy based on cohort size, outlier sensitivity, and regulatory directionality:

| Biological Goal & Cohort Setup | Network Type & Similarity | Upstream Package | Why |
|---|---|---|---|
| **Discovering signed functional modules** where up- and down-regulated pathways remain separated ($N \ge 15$ samples) | Signed network ($a_{ij} = \left(\frac{1 + \text{cor}(x_i, x_j)}{2}\right)^\beta$, default $\beta \approx 12$) | `PyWGCNA` or R `WGCNA::blockwiseModules(networkType="signed")` | Preserves correlation sign so negatively correlated genes are not merged into the same module |
| **Cohorts with occasional sample/expression outliers** | Biweight midcorrelation (`corType="bicor"`) | R `WGCNA` (`bicor`, `maxPOutliers=0.05`) | Robust median-based correlation that prevents single outlier samples from driving false co-expression |
| **Unsupervised module-trait association & hub gene ranking** | Module Eigengene (ME) correlation + $k_{\text{ME}}$ | `PyWGCNA` / R `WGCNA::signedKME` | Summarizes each module by its first principal component (ME) and scores intramodular hubs by $\text{cor}(x_g, \text{ME}_m)$ |
| **Small sample cohorts ($8 \le N < 15$) or rapid exploratory screening** | Soft-thresholded adjacency + hierarchical clustering | `scipy.cluster.hierarchy` / `bulkrna-coexpression` kernel | Lightweight scale-free topology selection and module eigengene hub scoring |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `PyWGCNA` / Scale-Free Soft-Thresholding Workflow

```python
import numpy as np
import pandas as pd
import PyWGCNA

# 1. Load variance-stabilized or log2(TPM + 1) expression (genes x samples -> transpose to samples x genes)
expr = pd.read_csv("data/vst_counts.csv", index_col=0)
# Keep top 5,000 most variable genes across samples to reduce noise and memory
mad = (expr.sub(expr.median(axis=1), axis=0)).abs().median(axis=1)
top_expr = expr.loc[mad.nlargest(5000).index].T  # samples x genes

# 2. Initialize and run PyWGCNA (soft-thresholding, TOM, dynamic tree cut, module eigengenes)
pywgcna = PyWGCNA.WGCNA(
    name="bulk_cohort",
    species="homo sapiens",
    geneExp=top_expr,
    outputPath="intermediate/",
    save=False,
)
pywgcna.preprocess()
pywgcna.findModules()

# 3. Extract module assignments and module eigengenes
modules_df = pywgcna.datExpr.var[["dynamicColors", "moduleColors"]]
me_df = pywgcna.MEs
```

### Pattern B: Lightweight Soft-Threshold & Hub-Gene Helper via `use_skill("bulkrna-coexpression")`

```python
# Calling use_skill("bulkrna-coexpression") auto-injects kernel.py helpers into the python kernel:
fit_df, best_power = select_soft_threshold(expr, powers=range(1, 16), r2_cutoff=0.85)
modules_df = detect_coexpression_modules(expr, power=best_power, min_module_size=15)
hubs_df = compute_kme_hubs(expr, modules_df, top_n=10)
```

## 3. Output Format & Verification Contract

- **Soft-Threshold Scale-Free Fit** (`tables/threshold_fit.csv`):
  - Columns: `power`, `r_squared` (scale-free topology $R^2$), `mean_connectivity`.
- **Module Assignments** (`tables/module_assignments.csv`):
  - Columns: `gene`, `module` (color or module label; `"grey"` denotes unassigned background genes).
- **Hub Genes Table** (`tables/hub_genes.csv`):
  - Columns: `gene`, `module`, `kME` (correlation with module eigengene $\text{ME}_m$, sorted descending by $|k_{\text{ME}}|$ per non-grey module).
- **Verification Invariants**:
  - Every input gene passed to module detection is accounted for in `tables/module_assignments.csv`.
  - All `kME` values lie strictly in $[-1, 1]$ and `r_squared` lies in $[0, 1]$.

## 4. Gotchas & Troubleshooting

1. **Minimum Sample Size ($N \ge 15$ Recommended)**:
   - Pairwise gene-gene correlations across $< 15$ bulk RNA-seq samples are noisy and prone to spurious scale-free fits. Never run WGCNA on $N < 8$ samples; for smaller cohorts, use differential expression (`bulkrna-de`) or curated PPI networks (`bulkrna-ppi-network`).
2. **Pre-Filter Low-Expression and Low-Variance Genes**:
   - Including 20,000+ genes with near-zero counts inflates the $G \times G$ Topological Overlap Matrix (TOM) to gigabytes of RAM and introduces zero-variance correlation `NaN`s. Always filter to expressed genes and retain the top $3,000\text{--}8,000$ genes by Median Absolute Deviation (MAD) or variance.
3. **Normalize and Variance-Stabilize Before Co-expression**:
   - Computing Pearson correlation on raw unnormalized counts measures library-size differences rather than biological co-regulation. Always transform counts using `DESeq2::vst`, `rlog`, or $\log_2(\text{TPM} + 1)$ first.
4. **Grey Module Interpretation**:
   - The `"grey"` module is a catch-all bin for genes that do not cluster into any coherent co-expression module. Always exclude `"grey"` before running downstream pathway enrichment (`bulkrna-enrichment`) or ranking `kME` hub genes.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `scipy`, `WGCNA`, `Matrix`
