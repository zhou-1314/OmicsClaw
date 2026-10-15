---
name: metabolomics-de
description: Load when running two-group metabolomics DE (t-test + log2FC + BH-FDR + PCA) on a feature
  × sample CSV using group-prefix matching (default `ctrl` / `treat`). Skip when needing tunable
  test backends (use metabolomics-statistics); raw spectra are the input (use
  metabolomics-xcms-preprocessing).
trigger: metabolomics differential, PLS-DA, volcano plot, biomarker, OPLS-DA
tags:
- metabolomics
- de
- ttest
- pca
- bh-fdr
- biomarker
---

# Differential Metabolite Abundance, PCA & VIP Analysis (`metabolomics-de`)

## 1. Method Decision Matrix & When to Use

Select the univariate or multivariate differential framework according to study design and collinearity among metabolic features:

| Analytical Goal | Recommended Method | Upstream Package | Decision Thresholds |
|---|---|---|---|
| **Univariate two-group false-discovery control** (`ctrl` vs. `treat`) | **Welch's $t$-test** on $\log_2$ intensities + **Benjamini-Hochberg FDR** | `scipy.stats.ttest_ind(..., equal_var=False)` + BH-FDR | `fdr < 0.05` and `$|\log_2\text{FC}| \ge 0.58 - 1.0$` ($1.5\times - 2.0\times$) |
| **Multivariate collinear feature ranking** (e.g., lipidomics / untargeted LC-MS) | **PLS-DA / OPLS-DA Variable Importance in Projection (VIP)** + permutation test | `scikit-learn` (`PLSRegression`) / R `ropls` | $\text{VIP} > 1.0$ **paired with** cross-validated $Q^2 > 0.5$ and permutation $p < 0.05$ |
| **Unsupervised batch & QC drift check** | **PCA on Pareto- or UV-scaled $\log_2$ matrix** | `sklearn.decomposition.PCA` | Pooled QC samples must cluster tightly near the origin $(0,0)$ relative to biological groups |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Univariate Welch $t$-Test + BH-FDR Paired with Multivariate PLS-DA VIP & PCA

```python
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.cross_decomposition import PLSRegression
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

# 1. Load feature-by-sample table and split ctrl vs. treat columns
df = pd.read_csv("data/metabolite_matrix.csv", index_col=0)
ctrl_cols = [c for c in df.columns if c.startswith("ctrl")]
treat_cols = [c for c in df.columns if c.startswith("treat")]
log_mat = np.log2(df[ctrl_cols + treat_cols].clip(lower=0) + 1.0)

# 2. Univariate Welch t-test + log2 fold change
lfc = log_mat[treat_cols].mean(axis=1) - log_mat[ctrl_cols].mean(axis=1)
_, pvals = stats.ttest_ind(log_mat[treat_cols].values, log_mat[ctrl_cols].values, axis=1, equal_var=False)

# 3. Multivariate PCA scores on Z-score standardized (samples x features) matrix
X_scaled = StandardScaler().fit_transform(log_mat.T)
pca = PCA(n_components=2, random_state=0).fit(X_scaled)
scores = pca.transform(X_scaled)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-de")`)

```python
# Calling use_skill("metabolomics-de") auto-injects kernel.py helpers into the python kernel:
de_df = differential_expression(df, group_a_prefix="ctrl", group_b_prefix="treat")
sig_df = significant_features(de_df, alpha=0.05, lfc_threshold=0.58)
```

## 3. Output Format & Verification Contract

- **Full Differential Features Table** (`tables/differential_features.csv`):
  - Contains `feature_id`, `mean_a`, `mean_b`, `log2fc` ($\log_2(\text{treat}) - \log_2(\text{ctrl})$), `t_stat`, `pvalue`, and `fdr` (Benjamini-Hochberg FDR).
- **Significant Features Subset** (`tables/significant_features.csv`):
  - Subset satisfying `fdr < 0.05`.
- **Verification Invariants**:
  - Control (`group_a_prefix`) and treatment (`group_b_prefix`) column sets are non-empty and strictly disjoint.
  - All returned `pvalue` and `fdr` entries lie in $[0.0, 1.0]$.

## 4. Gotchas & Troubleshooting

1. **PLS-DA / OPLS-DA Overfitting When $p \gg n$**:
   - In untargeted metabolomics ($p = 5,000$ features, $n = 12$ samples), supervised PLS-DA will **always** produce a visually separated 2D score plot even on pure random Gaussian noise. Never trust a PLS-DA plot or `VIP > 1.0` unless validated by $k$-fold cross-validation ($R^2Y, Q^2Y$) and a 200-label permutation test, and require dual significance (`VIP > 1.0` AND univariate `fdr < 0.05`).
2. **Autoscaling (Unit Variance) vs. Pareto Scaling ($rac{x - ar{x}}{\sqrt{s}}$) in PCA/PLS-DA**:
   - Unit-variance (`StandardScaler`) scaling gives noisy baseline trace peaks the exact same weight as abundant core metabolites, whereas unscaled PCA is dominated by the 5 highest-intensity lipids/sugars. **Pareto scaling** (dividing by $\sqrt{\sigma}$ on $\log_2$-transformed intensities) balances biological signal against baseline noise.
3. **Direction of `log2fc` Sign (`group_b / group_a`)**:
   - `differential_expression` defines `log2fc` as `group_b` (`treat`) minus `group_a` (`ctrl`) on the $\log_2$ scale, so positive `log2fc` indicates accumulation in `treat`.

## Dependencies

`numpy`, `pandas`, `scipy`, `scikit-learn`, `matplotlib`
