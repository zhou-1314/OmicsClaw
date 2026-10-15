---
name: metabolomics-quantification
description: Load when imputing missing values (min / median / KNN) and normalising (TIC / median / log)
  a feature × sample metabolomics CSV. Skip when only normalisation is needed (use metabolomics-normalization);
  the input is raw spectra (use metabolomics-xcms-preprocessing).
trigger: metabolomics quantification, imputation, feature quantification, missing values
tags:
- metabolomics
- quantification
- imputation
- normalization
- knn
- tic
---

# Metabolomics Missing-Value Imputation & Feature Quantification (`metabolomics-quantification`)

## 1. Method Decision Matrix & When to Use

Select the missing-value imputation and scaling pipeline according to the missingness mechanism in LC-MS/GC-MS feature tables:

| Missingness Mechanism | Recommended Imputation (`impute=`) | Upstream Algorithm / Formula | Why |
|---|---|---|---|
| **Limit of Detection (LOD / MNAR)** — trace/absent xenobiotics or condition-specific metabolites | **Half-Minimum (`impute="min"`)** or **QRILC** | Replace `0` / `NaN` with $\frac{1}{2} \min_{i,s > 0}(I_{i,s})$ (or per-feature $1/5 \min$) | Preserves low-abundance ranking for left-censored peaks below the MS noise floor |
| **Random Technical Dropout (MAR)** — missed peak integration or gap-filling failure | **$k$-Nearest Neighbors (`impute="knn"`)** or **Random Forest (`MissForest`)** | `sklearn.impute.KNNImputer(n_neighbors=5)` across correlated features | Borrows intensity structure from co-regulated/correlated metabolic features |
| **Quick Baseline Exploration** | **Sample Median (`impute="median"`)** | Replace `0` / `NaN` with each sample's positive median | Simple non-parametric baseline when missingness is $< 5\%$ |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: 80%-Rule Filtering + KNN / Half-Min Imputation + TIC / PQN Normalization

```python
import numpy as np
import pandas as pd
from sklearn.impute import KNNImputer

# 1. Treat both 0 and NaN as missing values in LC-MS feature tables
df = pd.read_csv("data/peak_table.csv")
sample_cols = [c for c in df.columns if c.startswith(("sample_", "intensity_", "ctrl_", "treat_"))]
mat = df[sample_cols].replace(0, np.nan).astype(float)

# 2. Apply the metabolomics "80% rule": retain features present in >= 80% of samples (or >= 80% of at least one group)
keep_mask = mat.notna().mean(axis=1) >= 0.80
mat = mat.loc[keep_mask]

# 3. Impute remaining MAR gaps with kNN (k=5) or MNAR half-minimum, then TIC-normalize
imputed = pd.DataFrame(KNNImputer(n_neighbors=5).fit_transform(mat.T).T, index=mat.index, columns=mat.columns)
col_sums = imputed.sum(axis=0)
tic_norm = imputed.div(col_sums, axis=1) * col_sums.median()
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-quantification")`)

```python
# Calling use_skill("metabolomics-quantification") auto-injects kernel.py helpers into the python kernel:
q_df = quantify(df, impute="min", normalize="tic")
summary = quantification_summary(q_df)
```

## 3. Output Format & Verification Contract

- **Quantified Feature Table** (`tables/quantified_features.csv`):
  - Preserves non-sample metadata columns (`feature_id`, `mz`, `rt`, `name`) alongside imputed and normalized numeric sample intensity columns.
- **Run Diagnostics** (`df.attrs["run_info"]`):
  - Records `impute`, `normalize`, `n_features`, `n_samples`, and `n_missing_imputed` (count of `0` and `NaN` cells replaced).
- **Verification Invariants**:
  - Every sample column must contain at least one strictly positive ($> 0$) observed intensity prior to imputation; completely empty/zero sample columns raise `ValueError`.
  - After `quantify`, zero `NaN` values remain in the sample intensity columns.

## 4. Gotchas & Troubleshooting

1. **Always Run XCMS/MZmine Chromatographic Gap-Filling (`fillChromPeaks`) Before Statistical Imputation**:
   - Up to 60% of missing values in raw peak tables occur simply because the peak-picker SNR threshold was slightly missed in one sample even though signal exists in the raw `mzML` chromatogram. Always integrate raw EIC area across the aligned RT window (`fillChromPeaks` in XCMS) before falling back to `min` or `knn` imputation.
2. **Apply the "80% Rule" Per Biological Group Before Imputing**:
   - Imputing a feature that is missing in 90% of samples fabricates artificial variance and ties in non-parametric tests. Discard features missing in $>20\%$ of QC runs and present in $<80\%$ of samples across every study group before running `knn` or `min` imputation.
3. **Zero (`0.0`) Means Missing in Peak Pickers**:
   - Both `XCMS` (`into` when unintegrated) and `MZmine` export `0` for undetected peaks. Never leave `0` as a literal zero intensity prior to $\log_2$ transformation or KNN imputation.

## Dependencies

`numpy`, `pandas`, `scikit-learn`, `matplotlib`
