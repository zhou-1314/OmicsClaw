---
name: metabolomics-statistics
description: Load when running univariate testing (t-test / Wilcoxon / ANOVA / Kruskal-Wallis) on a
  feature × sample metabolomics CSV with BH-FDR adjustment. Skip when working with raw spectra
  (use metabolomics-xcms-preprocessing); two-group DE with default `ctrl` / `treat` prefixes
  (use metabolomics-de).
trigger: metabolomics statistics, multivariate, PCA, clustering
tags:
- metabolomics
- statistics
- ttest
- wilcoxon
- anova
- kruskal
- bh-fdr
---

# Univariate Parametric & Non-Parametric Metabolomics Statistics (`metabolomics-statistics`)

## 1. Method Decision Matrix & When to Use

Select the univariate statistical hypothesis test according to group structure, sample size, and normality of $\log_2$ intensities:

| Experimental Design & Distribution | Method (`method=`) | Upstream `scipy.stats` Function | When to Use |
|---|---|---|---|
| **Two groups, approximately log-normal**, $n \ge 3$ per group | `method="ttest"` | `scipy.stats.ttest_ind(..., equal_var=False)` (Welch's $t$-test) | Default parametric test allowing unequal group variances |
| **Two groups, skewed / heavy-tailed / outliers**, $n \ge 5$ per group | `method="wilcoxon"` | `scipy.stats.ranksums` (Wilcoxon rank-sum) | Non-parametric rank test immune to extreme intensity spikes |
| **Equal-variance / multi-group parametric F-test** | `method="anova"` | `scipy.stats.f_oneway` (One-way ANOVA) | Parametric omnibus F-test across group means |
| **Non-parametric omnibus rank test** | `method="kruskal"` | `scipy.stats.kruskal` (Kruskal-Wallis H-test) | Non-parametric alternative to one-way ANOVA |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Explicit Group Selection (`group1_prefix` / `group2_prefix`), Hypothesis Testing & BH-FDR

```python
import numpy as np
import pandas as pd
from scipy import stats

# 1. Load feature-by-sample matrix (feature_id as index)
df = pd.read_csv("tables/normalized.csv", index_col=0)
g1_cols = [c for c in df.columns if c.startswith("WT_")]
g2_cols = [c for c in df.columns if c.startswith("KO_")]

# 2. Compute group2 / group1 log2 fold change and Wilcoxon rank-sum / Welch t-test p-values
g1, g2 = df[g1_cols].to_numpy(dtype=float), df[g2_cols].to_numpy(dtype=float)
log2fc = np.log2(np.clip(np.nanmean(g2, axis=1), 1e-12, None) / np.clip(np.nanmean(g1, axis=1), 1e-12, None))
stat_vals, pvals = stats.ttest_ind(g2, g1, axis=1, equal_var=False)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-statistics")`)

```python
# Calling use_skill("metabolomics-statistics") auto-injects kernel.py helpers into the python kernel:
res_df = test_groups(df, method="ttest", alpha=0.05, group1_prefix="WT_", group2_prefix="KO_")
sig_df = significant_table(res_df, alpha=0.05)
```

## 3. Output Format & Verification Contract

- **Full Statistical Results Table** (`tables/statistics.csv`):
  - Indexed by feature ID, with columns `mean_group1`, `mean_group2`, `log2fc` ($\log_2(\text{group2} / \text{group1})$), `statistic`, `pvalue`, `fdr` (Benjamini-Hochberg adjusted across all features), and boolean `significant` (`fdr < alpha`).
- **Significant Subset Table** (`tables/significant.csv`):
  - Rows satisfying `fdr < alpha`.
- **Verification Invariants**:
  - `group1` and `group2` column lists must be non-empty, disjoint, and explicitly paired (`group1_cols` + `group2_cols` or `group1_prefix` + `group2_prefix`).
  - `fdr` is monotonically non-decreasing with respect to sorted `pvalue` and bounded in $[0.0, 1.0]$.

## 4. Gotchas & Troubleshooting

1. **Minimum Sample Size ($n \ge 5$) Required for Non-Parametric Rank Tests (`wilcoxon` / `kruskal`) to Survive BH-FDR**:
   - With $n = 3$ biological replicates per group, the minimum mathematically possible two-sided Mann-Whitney / Wilcoxon $p$-value is $\frac{2}{\binom{6}{3}} = 0.10$ (`ranksums` normal approximation yields $p \approx 0.0495$). After Benjamini-Hochberg correction across 2,000 features, **zero features can ever achieve `fdr < 0.05`**. For $n = 3 - 4$, use parametric `ttest` (or `limma` empirical Bayes) on $\log_2$-transformed intensities.
2. **Independent (`ranksums`) vs. Paired (`scipy.stats.wilcoxon`) Samples**:
   - `method="wilcoxon"` in two-group untargeted pipelines invokes the **unpaired two-sample Wilcoxon rank-sum test** (`scipy.stats.ranksums`), not the paired signed-rank test. For longitudinal pre/post-treatment designs on the same subjects, compute subject-wise paired differences first.
3. **Never Rely on Implicit Midpoint Column Splitting in Production**:
   - Always supply explicit `group1_prefix` / `group2_prefix` or `group1_cols` / `group2_cols` so pooled QC columns or sample metadata columns are never accidentally mixed into experimental groups.

## Dependencies

`numpy`, `pandas`, `scipy`, `matplotlib`
