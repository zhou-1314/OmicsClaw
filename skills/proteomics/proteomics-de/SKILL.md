---
name: proteomics-de
description: Load when computing two-group differential protein abundance (log2FC + p-value +
  BH-adjusted FDR) via Welch t-test, equal-variance t-test, or Mann-Whitney on a wide protein ×
  sample CSV. Skip when you need multi-condition DE (run pairwise contrasts manually);
  label-based TMT linear-mixed models.
trigger: differential abundance, protein expression, MSstats, limma, volcano
tags:
- proteomics
- differential-expression
- ttest
- welch
- mann-whitney
- bh-fdr
---

# Differential Protein Abundance & Volcano Analysis (`proteomics-de`)

## 1. Method Decision Matrix & When to Use

Select the statistical test and missing-value strategy according to replicate count and intensity completeness:

| Experimental Design & Sample Size | Recommended Statistical Method | Upstream Package | Why |
|---|---|---|---|
| **Label-free (LFQ / DIA) or TMT**, $n \ge 3$ per group | **Empirical Bayes moderated $t$-test (`limma`)** or **Welch $t$-test** on $\log_2$ intensities | R `limma` / `DEqMS` or `scipy.stats.ttest_ind(..., equal_var=False)` + BH-FDR | Shrinks protein-wise variance toward a common prior (or PSM-count trend in `DEqMS`) and controls FDR |
| **Non-Gaussian / heavy-tailed abundance ratios**, $n \ge 5$ per group | **Wilcoxon rank-sum / Mann-Whitney U** (`method="wilcoxon"`) | `scipy.stats.ranksums` / `mannwhitneyu` + BH-FDR | Distribution-free test resilient to extreme outlier intensities |
| **Peptide-level linear mixed model** | **MSstats** (`dataProcess` + `groupComparison`) | Bioconductor `MSstats` | Models peptide-by-run effects and left-censored missing values directly without pre-aggregating to proteins |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: $\log_2$ Transformation, Left-Censored MNAR Imputation & Welch $t$-Test + BH-FDR

```python
import numpy as np
import pandas as pd
from scipy import stats

# 1. Load protein intensity matrix (proteins x samples), replace 0 with NaN, and log2-transform
df = pd.read_csv("tables/proteins.csv", index_col=0)
ctrl_cols = [c for c in df.columns if c.startswith("ctrl")]
treat_cols = [c for c in df.columns if c.startswith("treat")]
mat = np.log2(df[ctrl_cols + treat_cols].replace(0, np.nan))

# 2. Filter proteins observed in >= 50% of samples and apply Perseus-style down-shifted normal imputation
mat = mat.loc[mat.notna().mean(axis=1) >= 0.5].copy()
rng = np.random.default_rng(42)
for col in mat.columns:
    s = mat[col].dropna()
    na_mask = mat[col].isna()
    if na_mask.any() and len(s) > 1:
        # Perseus MNAR defaults: mean - 1.8 * SD, width = 0.3 * SD
        mat.loc[na_mask, col] = rng.normal(loc=s.mean() - 1.8 * s.std(ddof=1), scale=0.3 * s.std(ddof=1), size=na_mask.sum())

# 3. Welch two-sample t-test + Benjamini-Hochberg FDR
lfc = mat[treat_cols].mean(axis=1) - mat[ctrl_cols].mean(axis=1)
_, pvals = stats.ttest_ind(mat[treat_cols].values, mat[ctrl_cols].values, axis=1, equal_var=False)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-de")`)

```python
# Calling use_skill("proteomics-de") auto-injects kernel.py helpers into the python kernel:
res_df = differential_abundance(df, group1=ctrl_cols, group2=treat_cols, method="ttest")
sig_df = significant(res_df, alpha=0.05, log2fc_threshold=1.0)
```

## 3. Output Format & Verification Contract

- **Full Differential Abundance Table** (`tables/differential_abundance.csv`):
  - Contains `protein_id`, `mean_group1`, `mean_group2`, `log2fc` (`group2 - group1` on $\log_2$ scale), `stat`, `pvalue`, and `fdr` (Benjamini-Hochberg adjusted $p$-value).
- **Significant Regulated Proteins** (`tables/significant.csv`):
  - Subset satisfying `fdr < alpha` (default `0.05`) and `abs(log2fc) >= log2fc_threshold`.
- **Verification Invariants**:
  - `0 <= pvalue <= fdr <= 1.0` for all tested proteins with finite group observations.
  - `log2fc` reflects $\log_2(\text{group2}) - \log_2(\text{group1})$ (positive = up-regulated in `group2` / treatment).

## 4. Gotchas & Troubleshooting

1. **Missing Not At Random (MNAR) Left-Censored Values in Mass Spectrometry**:
   - Unlike RNA-seq where zeros reflect Poisson sampling, MS missing values are predominantly **MNAR** (peptides below the Orbitrap/timsTOF limit of detection) mixed with **MAR** (stochastic DDA precursor selection). Mean or median imputation artificially sets absent proteins in control to the high treatment average, destroying true on/off biomarkers. Use left-censored down-shifted normal imputation (`MinProb` / Perseus `-1.8 SD, 0.3 SD` width) for MNAR and $k$NN/QRILC for hybrid missingness.
2. **Computing Fold Changes on Raw Linear Scale vs. $\log_2$ Scale**:
   - Raw MS intensities span $10^4 - 10^{11}$ with multiplicative log-normal noise. Taking arithmetic means on linear intensities before $\log_2$ allows a single high-intensity outlier replicate to dominate the fold change. Always $\log_2$-transform intensities **before** computing group means and $t$-statistics (unless input is already $\log_2$-transformed, typically max $< 50$).
3. **Ratio Compression in Isobaric TMT/iTRAQ Workflows**:
   - Co-isolation of interfering precursor ions in MS2-based TMT compresses true fold changes toward `1.0` (`log2fc -> 0`). Use a relaxed `|log2fc| >= 0.5` ($1.4\times$) cutoff for TMT compared to `|log2fc| >= 1.0` ($2\times$) for LFQ/DIA.

## Dependencies

`numpy`, `pandas`, `matplotlib`, `scipy`
