---
name: proteomics-ms-qc
description: Load when computing protein-table QC — proteins × samples count, missing-value rate,
  intensity CV — from a MaxQuant / FragPipe / DIA-NN protein CSV. Skip when raw mzML / RAW
  spectra are the input (run a search engine first); peptide-level QC is needed (use
  proteomics-identification).
trigger: MS QC, mass spec QC, PTXQC, rawTools
tags:
- proteomics
- qc
- ms
- maxquant
- intensity
- missing-values
- cv
---

# Mass Spectrometry Protein Matrix Quality Control (`proteomics-ms-qc`)

## 1. Method Decision Matrix & When to Use

Select the QC metric suite according to the proteomics acquisition mode and matrix stage:

| QC Dimension | Metric & Formula | Target Benchmark (LFQ / DIA) | Why |
|---|---|---|---|
| **Data Completeness (Missingness)** | $\text{Missing Rate} = \frac{\#(\text{NaN or } 0)}{N_{\text{proteins}} \times N_{\text{samples}}}$ | DIA: $< 10\%$; DDA LFQ: $< 25 - 35\%$; TMT (within-plex): $< 5\%$ | Detects LC column degradation, electrospray dropouts, or failed Match-Between-Runs (MBR) |
| **Technical Precision (Linear Intensity CV)** | $\text{CV} = \frac{\sigma_{\text{linear}}}{\mu_{\text{linear}}} \times 100\%$ across technical/QC replicates | Median CV $< 15\%$ (DIA/TMT), $< 20\%$ (DDA LFQ) | Quantifies quantitative reproducibility on the **unlogged linear** intensity scale |
| **Sample Dynamic Range & Total Load** | Per-sample $\log_{10}(\text{Intensity})$ median & IQR | $< 0.3$ $\log_{10}$ shift across runs before normalization | Flags sample-loading pipetting errors or ion suppression before median/LFQ normalization |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Proteomics Matrix Completeness & Linear-Scale Coefficient of Variation (CV)

```python
import numpy as np
import pandas as pd

# 1. Load protein intensity table and isolate numeric sample columns (exclude metadata)
df = pd.read_csv("tables/proteins.csv", index_col=0)
meta_names = {"mw", "mol_weight", "length", "peptides", "unique_peptides", "razor_peptides", "score", "q_value", "contaminant", "reverse"}
sample_cols = [c for c in df.select_dtypes(include=[np.number]).columns if c.lower() not in meta_names]

# 2. Treat 0 and NaN as missing values
mat = df[sample_cols].replace(0, np.nan).astype(float)
missing_rate_pct = float(mat.isna().mean().mean() * 100.0)

# 3. Compute per-protein CV on linear intensities (ddof=1 across >=2 observed samples)
linear_mat = 2.0 ** mat if np.nanmax(mat.to_numpy()) < 50.0 else mat
row_mean = linear_mat.mean(axis=1)
row_std = linear_mat.std(axis=1, ddof=1)
cv_pct = (row_std / row_mean * 100.0).dropna()
median_cv_pct = float(cv_pct.median())
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-ms-qc")`)

```python
# Calling use_skill("proteomics-ms-qc") auto-injects kernel.py helpers into the python kernel:
qc_table = quality_control(df)
per_sample = sample_completeness(df)
```

## 3. Output Format & Verification Contract

- **One-Row Scalar QC Summary** (`tables/qc_metrics.csv`):
  - Contains `n_proteins`, `n_samples`, `missing_rate_pct`, `completeness_pct`, `median_cv_pct`, and `mean_cv_pct`.
- **Per-Sample Completeness Diagnostics** (`qc_table.attrs["run_info"]`):
  - Contains `sample_columns` and `per_sample_completeness_pct` mapping each sample run to its percentage of quantified proteins.
- **Verification Invariants**:
  - `missing_rate_pct + completeness_pct == 100.0` (within floating-point precision).
  - Both `0` and `NaN` entries are counted as missing observations.

## 4. Gotchas & Troubleshooting

1. **Never Compute Coefficient of Variation ($	ext{CV} = \sigma / \mu$) on $\log_2$-Transformed Values**:
   - $\log_2$ transformation turns multiplicative relative variance into additive variance. Computing $\sigma_{\log_2} / \mu_{\log_2}$ divides by an arbitrary log-offset and yields meaningless CV percentages (e.g., 3% instead of 25%). Always compute CV on **linear intensities** (or use the exact log-normal geometric CV formula $\sqrt{2^{\sigma_{\log_2}^2 \ln 2} - 1}$).
2. **Numeric Metadata Columns (`Mol. weight [kDa]`, `Sequence length`, `Peptides`) Polluting Sample Columns**:
   - Selecting all numeric columns (`df.select_dtypes(np.number)`) on a raw MaxQuant/FragPipe file treats `Peptides` (e.g. `12`) and `Sequence coverage [%]` as low-intensity sample runs, inflating the apparent CV to $>200\%$. Always exclude metadata column names before computing matrix QC.
3. **Zero-Filled DIA/MaxQuant Matrices Mask True Missingness**:
   - Export scripts often fill missing values with `0.0`. Always convert `0 -> np.nan` before calculating completeness or per-protein CVs.

## Dependencies

`numpy`, `pandas`, `matplotlib`
