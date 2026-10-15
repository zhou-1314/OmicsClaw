---
name: metabolomics-normalization
description: Load when normalising a feature × sample metabolomics CSV via median, quantile, total (sum),
  PQN (probabilistic quotient), or log methods — emits a normalised wide-form table. Skip when also imputing
  (use metabolomics-quantification); raw spectra (use metabolomics-xcms-preprocessing).
trigger: metabolomics normalization, scaling, NOREVA, TIC normalization
tags:
- metabolomics
- normalization
- pqn
- quantile
- median
- log
---

# Metabolomics Sample Normalization & Drift Correction (`metabolomics-normalization`)

## 1. Method Decision Matrix & When to Use

Select the sample normalization algorithm according to specimen type (biofluid vs. cell extract) and batch size:

| Biological Specimen & Design | Recommended Method (`method=`) | Mathematical Formulation | Why |
|---|---|---|---|
| **Urine, Feces, Saliva, or Variable Dilution Biofluids** | **Probabilistic Quotient Normalization (`method="pqn"`)** | Divide each sample by the **median quotient** $Q_s = \text{median}_i(I_{i,s} / I_{i,\text{ref}})$ against a reference spectrum | Resilient to a few hyper-abundant dietary/drug metabolites that corrupt Total Ion Current (TIC) |
| **Serum, Plasma, or Cultured Cells** (consistent total biomass) | **Median Normalization (`method="median"`)** | Scale each sample column by $\frac{\text{median}_{\text{global}}}{\text{median}_s}$ | Aligns sample medians without sensitivity to extreme high-intensity lipid/sugar outliers |
| **Targeted / Small-Panel Assays** | **Total Ion Current (`method="total"` / TIC)** or **Internal Standard (IS)** | Scale each sample by $\frac{\text{median}(\sum_i I_{i,s})}{\sum_i I_{i,s}}$ or deuterated IS peak area | Standard constant-sum scaling when no single metabolite dominates total ion current |
| **Multi-Batch LC-MS (>50 injections with pooled QC every 5–10 runs)** | **QC-RLSC / LOESS / SERRF** | Fit local spline/random-forest drift curve across injection order on pooled QCs | Removes non-linear MS sensitivity decay and ionization drift across multi-day analytical sequences |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Probabilistic Quotient Normalization (PQN; Dieterle et al., *Anal. Chem.* 2006)

```python
import numpy as np
import pandas as pd

# 1. Load feature x sample matrix (features as rows, samples as columns)
mat = pd.read_csv("data/feature_table.csv", index_col=0).astype(float)

# 2. Step 1 of PQN: Optional TIC pre-scaling, then compute median reference spectrum across samples
ref_spectrum = mat.median(axis=1).replace(0, np.nan)

# 3. Step 2 of PQN: Compute sample-wise median dilution quotient relative to reference spectrum
quotients = mat.div(ref_spectrum, axis=0).median(axis=0)
pqn_normalized = mat.div(quotients.replace(0, np.nan), axis=1)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-normalization")`)

```python
# Calling use_skill("metabolomics-normalization") auto-injects kernel.py helpers into the python kernel:
pqn_df = normalize(mat, method="pqn")
med_df = normalize(mat, method="median")
```

## 3. Output Format & Verification Contract

- **Normalized Feature Matrix** (`tables/normalized.csv`):
  - Retains the exact input feature index and sample column names, scaled according to `method` (`"median"`, `"pqn"`, `"total"`, `"quantile"`, or `"log"`).
- **Run Metadata** (`df.attrs["run_info"]`):
  - Stores `method`, `n_features`, and `n_samples`.
- **Verification Invariants**:
  - `normalize` performs **no silent missing-value imputation** (`NaN` values remain `NaN`).
  - Zero or all-`NaN` divisors yield `NaN` rather than $\pm\infty$.

## 4. Gotchas & Troubleshooting

1. **Why Total Ion Current (`total` / TIC) Fails in Urine and Drug-Perturbation Studies**:
   - In urine or xenobiotic dosing studies, a single excreted drug glucuronide or creatinine peak can account for 30–50% of total ion current. TIC normalization divides all endogenous metabolites by that huge sum, creating thousands of **false-positive down-regulated hits**. Always prefer **PQN** (`method="pqn"`) or **median** normalization (`method="median"`).
2. **Order of Operations: Normalization Must Precede $\log_2$ Transformation and Pareto Scaling**:
   - Dilution and injection-volume differences are **multiplicative** in raw intensity space ($I'_{i,s} = c_s I_{i,s}$). Apply PQN / median / TIC normalization on raw linear intensities *first*, and apply $\log_2(x + 1)$ variance stabilization *second*.
3. **Post-Normalization QC-RSD Filtering (`RSD < 20–30%`)**:
   - After normalization, compute the Relative Standard Deviation ($\text{RSD} = \sigma / \mu \times 100\%$) of each feature across pooled Quality Control (QC) injections and discard unstable features with $\text{QC-RSD} > 30\%$ before statistical testing.

## Dependencies

`numpy`, `pandas`, `matplotlib`
