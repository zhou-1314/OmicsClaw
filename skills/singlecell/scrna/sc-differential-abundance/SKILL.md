---
name: sc-differential-abundance
description: Load when testing whether cell-type / cluster proportions or neighbourhood densities differ
  between conditions in a multi-sample scRNA AnnData via Milo, scCODA, simple proportion screen, or R
  Monte-Carlo permutation. Skip when ranking marker genes (use sc-markers); per-cell DE (use sc-de).
tags:
- singlecell
- scrna
- differential-abundance
- compositional
- milo
- sccoda
- proportion-test
---

# Single-Cell Differential Abundance & Compositional Analysis (`sc-differential-abundance`)

## 1. Method Decision Matrix & When to Use

Use this skill to test whether the relative abundance of discrete cell types or continuous $k$NN neighborhoods changes across conditions in a multi-sample cohort (`sample_key`).

| Experimental Design & Question | Recommended Method | Upstream Library | Statistical Unit & Assumptions |
|---|---|---|---|
| **Continuous state shifts / resolution-free DA** ($\ge 3$ replicates/condition) | **Milo** (`milo`) | `pertpy.tl.Milo` | Overlapping $k$NN neighborhoods (`nhood`); negative binomial GLM with spatial FDR |
| **Discrete cell-type compositional shift** ($\ge 3$ replicates/condition) | **scCODA** (`sccoda`) | `pertpy.tl.Sccoda` / `sccoda` | Sample $\times$ cell-type counts; hierarchical Dirichlet-multinomial with reference cell type |
| **Fast non-parametric sample proportion screen** ($\ge 4$ replicates/condition) | **Mann-Whitney U Proportion Screen** (`simple`) | `scipy.stats.mannwhitneyu` + `statsmodels` BH | Per-sample row-normalized cell-type proportions (`counts / sample_total`) |
| **Single-sample exploratory permutation** (no biological replicates) | **R Monte-Carlo Permutation** (`proportion_test_r`) | Base-R permutation + bootstrap CI | Permutes cell labels ignoring donor structure (exploratory only) |

> **Decision Rule**: Cell-type proportions sum to $1.0$ within each sample—expansion of one dominant population mechanically lowers the fractions of all others. Prefer `milo` for continuous sub-cluster shifts and `sccoda` (with an invariant reference cell type) for discrete cell-type composition.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Neighborhood-Level Differential Abundance via Milo (`pertpy`)

```python
import pertpy as pt
import scanpy as sc

# 1. Ensure a batch-corrected kNN graph exists (e.g., built from X_pca_harmony or X_scVI)
if "connectivities" not in adata.obsp:
    sc.pp.neighbors(adata, n_neighbors=30, use_rep="X_pca")

# 2. Construct overlapping kNN neighborhoods on the graph
milo = pt.tl.Milo()
mdata = milo.load(adata)
milo.make_nhoods(mdata["rna"], prop=0.1, seed=0)

# 3. Count cells per (neighborhood, biological sample) and fit GLM
mdata = milo.count_nhoods(mdata, sample_col="sample")
milo.da_nhoods(mdata, design="~condition")
nhood_df = mdata["milo"].var.copy()  # Columns: logFC, PValue, SpatialFDR, Nhood_size
```

### Pattern B: Replicate-Aware Sample Proportion Screen (`scipy` + `statsmodels`)

```python
import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu
from statsmodels.stats.multitest import multipletests

# 1. Tabulate sample-by-celltype counts and row-normalized proportions
counts = pd.crosstab(adata.obs["sample"], adata.obs["cell_type"])
props = counts.div(counts.sum(axis=1), axis=0)
sample_cond = adata.obs.groupby("sample", observed=True)["condition"].first().loc[props.index]
conds = sorted(sample_cond.unique())
g_a, g_b = sample_cond == conds[0], sample_cond == conds[1]

# 2. Compare replicate proportions per cell type with two-sided Mann-Whitney U + BH
rows = []
for ct in props.columns:
    va, vb = props.loc[g_a, ct].to_numpy(), props.loc[g_b, ct].to_numpy()
    _, pval = mannwhitneyu(vb, va, alternative="two-sided")
    l2fc = np.log2((vb.mean() + 1e-6) / (va.mean() + 1e-6))
    rows.append({"cell_type": ct, "mean_prop_a": va.mean(), "mean_prop_b": vb.mean(), "log2fc_group_b_over_a": l2fc, "pval": pval})

da_df = pd.DataFrame(rows)
da_df["padj"] = multipletests(da_df["pval"], method="fdr_bh")[1]
da_df["significant"] = da_df["padj"] < 0.05
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-differential-abundance")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `composition(adata, sample_key="sample", celltype_key="cell_type", condition_key="condition")`
- `condition_proportions(adata, sample_key="sample", celltype_key="cell_type", condition_key="condition")`
- `test_abundance(adata, method="simple", sample_key="sample", condition_key="condition", celltype_key="cell_type", fdr=0.05)`

## 3. Output Format & Verification Contract

- **Universal Composition Tables**:
  - `tables/sample_by_celltype_counts.csv`: integer cell counts of shape `(n_samples, n_cell_types)`.
  - `tables/sample_by_celltype_proportions.csv`: row-normalized sample proportions (each row sums to $1.0$).
  - `tables/condition_mean_proportions.csv`: condition-level mean proportions weighting each donor/sample equally.
- **Differential Abundance Results** (`tables/abundance.csv` / `tables/milo_nhood_results.csv` / `tables/sccoda_effects.csv`):
  - `simple`: `cell_type`, `mean_prop_a`, `mean_prop_b`, `log2fc_group_b_over_a`, `pval`, `padj`, `significant`.
  - `milo`: `logFC`, `PValue`, `SpatialFDR`, `Nhood_size`.
- **Verification Invariants**:
  - Assert every `sample` maps to exactly one `condition`.
  - Assert `np.allclose(proportions.sum(axis=1), 1.0)`.

## 4. Gotchas & Troubleshooting

1. **Under-Replication Trap ($n < 3$ Samples per Condition)**:
   - With only $n=2$ samples per condition, a two-sided Mann-Whitney $U$ test cannot achieve $p < 0.333$, so `padj < 0.05` is mathematically impossible. Require $\ge 3\text{--}4$ independent samples per condition.
2. **Sample-to-Condition Mapping Violation**:
   - Each value in `adata.obs[sample_key]` must belong to a single `condition_key`. If the same donor was sampled before and after treatment, use distinct sample IDs (`donor1_pre`, `donor1_post`) in `sample_key`.
3. **Reference Cell-Type Selection in `scCODA`**:
   - `scCODA` models log-fold changes relative to a reference cell type assumed constant across conditions. Verify that `reference_cell_type="automatic"` (or your explicit reference) exhibits stable proportions across samples.
4. **Cell-Level Permutation Pseudoreplication (`proportion_test_r`)**:
   - Permuting individual cell labels ignores sample-level correlation and inflates false positives when donor replicates exist. Use `proportion_test_r` only as an exploratory screen on unreplicated designs.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `pertpy`, `scanpy`, `sccoda`, `scipy`, `seaborn`, `statsmodels`
