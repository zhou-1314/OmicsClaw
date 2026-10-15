---
name: bulkrna-survival
description: Load when comparing bulk expression strata against clinical time-to-event data with Kaplan-Meier and log-rank tests. R survival also fits Cox HR; Python reports a descriptive events/person-time ratio. Skip missing clinical outcomes.
trigger: survival, Kaplan-Meier, Cox, prognosis, hazard ratio, overall survival, clinical outcome
tags:
- bulkrna
- survival
- Kaplan-Meier
- Cox
- hazard-ratio
- clinical
---

# Bulk RNA-seq Clinical Survival Analysis (`bulkrna-survival`)

## 1. Method Decision Matrix & When to Use

Select the survival modeling framework based on whether you are comparing discrete expression strata or modeling continuous multivariable risk:

| Clinical Question | Statistical Method | Upstream Package | Why |
|---|---|---|---|
| **Compare survival curves between High vs. Low expression strata** | Kaplan-Meier estimator + two-sample Log-Rank test | `lifelines` (`KaplanMeierFitter`, `logrank_test`) or R `survival::survdiff` | Non-parametric visual survival curves $S(t)$ and exact score test without proportional hazards assumptions |
| **Estimate continuous gene hazard ratio ($\text{HR} = e^\beta$) adjusted for age/stage** | Cox Proportional Hazards (CoxPH) regression | `lifelines.CoxPHFitter` or R `survival::coxph` | Semi-parametric partial likelihood yielding $\text{HR}$, $95\%$ CI, and Wald $p$-value per unit ($z$-score) gene expression |
| **High-dimensional multi-gene prognostic signature** ($G \gg N$ genes) | $L_1$/$L_2$ penalized CoxPH (Lasso / ElasticNet Cox) | `lifelines.CoxPHFitter(penalizer=0.1, l1_ratio=0.5)` or `sksurv.linear_model.CoxnetSurvivalAnalysis` | Prevents singular Hessian matrices and overfitting when selecting a multi-gene risk score |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `lifelines` Kaplan-Meier + Log-Rank + Penalized CoxPH

```python
import numpy as np
import pandas as pd
from lifelines import CoxPHFitter, KaplanMeierFitter
from lifelines.statistics import logrank_test

# 1. Load log2-normalized expression (genes x samples) and clinical outcome table
expr = pd.read_csv("data/log2_tpm.csv", index_col=0)
clin = pd.read_csv("data/clinical.csv").set_index("sample")
shared = expr.columns.intersection(clin.index)
expr, clin = expr[shared], clin.loc[shared]

# 2. Median stratification + Log-Rank test + univariate standardized CoxPH per candidate gene
results = []
for gene in ["MKI67", "TP53", "ESR1"]:
    x = expr.loc[gene]
    high_mask = x >= x.median()
    lr = logrank_test(
        clin.loc[high_mask, "time"], clin.loc[~high_mask, "time"],
        event_observed_A=clin.loc[high_mask, "event"], event_observed_B=clin.loc[~high_mask, "event"],
    )
    df_cox = clin[["time", "event"]].copy()
    df_cox["gene_z"] = (x - x.mean()) / max(x.std(ddof=1), 1e-8)
    cph = CoxPHFitter(penalizer=0.01)
    cph.fit(df_cox, duration_col="time", event_col="event")
    row = cph.summary.loc["gene_z"]
    results.append({
        "gene": gene,
        "hr": float(row["exp(coef)"]),
        "hr_lower_95": float(row["exp(coef) lower 95%"]),
        "hr_upper_95": float(row["exp(coef) upper 95%"]),
        "cox_pvalue": float(row["p"]),
        "logrank_pvalue": float(lr.p_value),
    })
surv_df = pd.DataFrame(results)
```

### Pattern B: Lightweight KM & Log-Rank Micro-Helper via `use_skill("bulkrna-survival")`

```python
# Calling use_skill("bulkrna-survival") auto-injects kernel.py helpers into the python kernel:
surv_df = stratified_logrank_screen(expr, clinical=clin, genes=["MKI67", "TP53", "ESR1"])
km_df = kaplan_meier_curve(clin["time"], clin["event"])
```

## 3. Output Format & Verification Contract

- **Per-Gene Survival Summary** (`tables/survival_results.csv`):
  - Columns: `gene`, `cutoff`, `n_high`, `n_low`, `logrank_stat`, `pvalue`, `padj`, `hazard_ratio`.
- **Kaplan-Meier Curve Coordinates** (`intermediate/km_curves.csv` or `figures/km_<gene>.png`):
  - Columns: `gene`, `group` (`High` / `Low`), `time`, `survival` ($S(t) \in [0, 1]$).
- **Verification Invariants**:
  - `time >= 0` and `event` is strictly binary (`0` = censored, `1` = event).
  - Both `High` and `Low` expression strata contain $\ge 2$ samples (`n_high >= 2` and `n_low >= 2`).

## 4. Gotchas & Troubleshooting

1. **Collinearity & Complete Separation in `CoxPHFitter` (`LinAlgError: Matrix is singular`)**:
   - Low-variance genes, collinear multi-gene signatures, or small event counts ($< 10$ deaths) cause the partial-likelihood Hessian to become singular. Always standardize gene expression to unit variance ($z$-score) and set a small ridge penalty (`CoxPHFitter(penalizer=0.01)`) when screening bulk cohorts.
2. **Multiple Testing Inflation from "Optimal Cutpoint" Scanning (`cutoff_method="optimal"`)**:
   - Scanning all percentiles ($10\%\text{--}90\%$) to pick the split that minimizes the log-rank $p$-value severely inflates Type I error ($> 40\%$ false-positive rate at nominal $\alpha = 0.05$). Always default to the pre-specified **median** split (`cutoff_method="median"`) or continuous CoxPH regression; treat optimal-cutpoint $p$-values as exploratory unless adjusted via Lausen-Schumacher permutation.
3. **Censoring Coding Mismatches (`1/2` vs. `0/1`)**:
   - Legacy clinical tables from R or TCGA sometimes encode status as `1 = censored, 2 = dead` or strings (`"Alive"` / `"Dead"`). Passing `1/2` directly to `lifelines` treats every patient as an uncensored event! Explicitly validate `set(clin["event"].unique()) <= {0, 1}` before fitting.
4. **Proportional Hazards Assumption Violations**:
   - If the `High` and `Low` Kaplan-Meier curves cross each other mid-follow-up, the hazard ratio is non-proportional over time (`cph.check_assumptions(df_cox)`). Report restricted mean survival time (RMST) differences alongside log-rank results.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `scipy`, `survival`, `Matrix`
