---
name: sc-filter
description: Load when removing low-quality cells and lowly-detected genes from a single-cell AnnData
  using QC-derived thresholds or tissue presets. Skip when the full normalize→HVG→PCA→cluster pipeline
  (use sc-preprocessing); reads are still raw FASTQ (use sc-fastq-qc).
trigger: filter cells, cell filtering, gene filtering, remove low quality, qc filtering, tissue-specific thresholds
tags:
- singlecell
- scrna
- filter
- qc
- mitochondrial
---

# Single-Cell Quality Control Cell & Gene Filtering (`sc-filter`)

## 1. Method Decision Matrix & When to Use

Apply explicit threshold-based, tissue-preset, or adaptive MAD-based filtering to remove empty droplets, lysed/dying cells, putative doublets, and uninformative genes from an `AnnData` object after inspecting `sc-qc`.

| Filtering Strategy | Criteria Applied | Typical Parameters | Best Used When |
|---|---|---|---|
| **Manual / Explicit Cutoffs** | Fixed bounds on `n_genes_by_counts`, `total_counts`, `pct_counts_mt`, `min_cells` | `min_genes=200`, `max_mt_percent=15..20`, `min_cells=3` | Single-tissue datasets after inspecting `sc-qc` violin/scatter plots |
| **Tissue-Specific Presets** | Pre-calibrated (`min_genes`, `max_genes`, `max_mt_percent`) per organ | `pbmc` ($200\text{--}2500$, $<5\%$ MT); `brain`/snRNA ($200\text{--}6000$, $<5\%$ MT); `tumor` ($200\text{--}7500$, $<20\%$ MT) | Standardized pipeline execution across known tissue types |
| **Sample-Aware Adaptive MAD** | Median $\pm k \cdot \text{MAD}$ per batch/sample (`scipy.stats.median_abs_deviation`) | $> 3\text{--}5$ MAD below median $\log_{10}(\text{counts/genes})$, $> 3$ MAD above median MT% | Multi-sample cohorts where sequencing depth or baseline viability varies across batches |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Explicit QC Threshold + Doublet Filtering (`scanpy`)

```python
import scanpy as sc
import pandas as pd

n_cells_before, n_genes_before = adata.n_obs, adata.n_vars

# 1. Ensure QC metrics exist on the raw count matrix
if "n_genes_by_counts" not in adata.obs or "pct_counts_mt" not in adata.obs:
    adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], inplace=True)

# 2. Construct composite cell retention mask
cell_mask = (
    (adata.obs["n_genes_by_counts"] >= 200)
    & (adata.obs["n_genes_by_counts"] <= 5000)
    & (adata.obs["pct_counts_mt"] < 20.0)
)
if "predicted_doublet" in adata.obs:
    cell_mask &= ~adata.obs["predicted_doublet"].astype(bool)

adata = adata[cell_mask].copy()

# 3. Filter genes AFTER cell removal so genes expressed only in dropped cells are removed
sc.pp.filter_genes(adata, min_cells=3)
```

### Pattern B: Adaptive Median Absolute Deviation (MAD) Outlier Filtering (`scipy.stats`)

```python
import numpy as np
from scipy.stats import median_abs_deviation

def is_mad_outlier(series: pd.Series, nmads: float = 5.0, upper_only: bool = False) -> pd.Series:
    med = np.median(series)
    mad = median_abs_deviation(series, scale="normal")
    if upper_only:
        return series > (med + nmads * mad)
    return (series < med - nmads * mad) | (series > med + nmads * mad)

low_qual = (
    is_mad_outlier(np.log1p(adata.obs["total_counts"]), nmads=5.0)
    | is_mad_outlier(np.log1p(adata.obs["n_genes_by_counts"]), nmads=5.0)
    | is_mad_outlier(adata.obs["pct_counts_mt"], nmads=3.0, upper_only=True)
)
adata = adata[~low_qual].copy()
sc.pp.filter_genes(adata, min_cells=3)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-filter")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `filter_cells(adata, min_genes=200, max_genes=None, min_counts=None, max_counts=None, max_mt_percent=20.0, min_cells=3, remove_doublets=True)`
- `retention_summary(before, after)`
- `tissue_presets()`

## 3. Output Format & Verification Contract

- **Filtered AnnData** (`intermediate/processed.h5ad`):
  - Subsetted `adata` where all `layers` (`adata.layers["counts"]`) and `obs`/`var` annotations remain strictly aligned.
- **Retention Summary Tables** (`tables/retention_summary.csv`, `tables/filter_summary.csv`, `tables/filter_stats.csv`):
  - Columns: `feature` (`cells`, `genes`), `before`, `after`, `retained_pct`.
- **Verification Invariants**:
  - Verify `0 < adata.n_obs <= n_cells_before` and `0 < adata.n_vars <= n_genes_before`.
  - Verify `adata.obs["n_genes_by_counts"].min() >= min_genes` and `adata.obs["pct_counts_mt"].max() < max_mt_percent`.

## 4. Gotchas & Troubleshooting

1. **Always Filter Cells Before Filtering Genes (`min_cells`)**:
   - Running `sc.pp.filter_genes(adata, min_cells=3)` before dropping low-quality/empty droplets retains genes that were only detected inside discarded debris droplets, leaving all-zero gene columns in the cell-filtered matrix. Always filter cells first, then call `sc.pp.filter_genes`.
2. **Beware of Pre-Existing `obs` QC Columns After Normalization**:
   - If `adata` was already log-normalized and `n_genes_by_counts` / `total_counts` are missing from `adata.obs`, recomputing `sc.pp.calculate_qc_metrics` on log1p-normalized `adata.X` yields log-summed values ($\sim 10\text{--}100$) instead of raw UMI totals ($\sim 1,000\text{--}50,000$). Always compute QC metrics on `adata.layers["counts"]`.
3. **Overly Aggressive `max_genes` or `max_mt_percent` Strips Biological Cell Types**:
   - Metabolically active hepatocytes, cardiomyocytes, and tumor cells routinely exhibit $15\text{--}30\%$ mitochondrial counts, and large polyploid cells or macrophages have high `n_genes_by_counts`. Applying a PBMC preset (`max_mt_percent=5.0`, `max_genes=2500`) to solid tumors or liver tissue will wipe out the parenchymal cell types of interest.
4. **Update or Clear `adata.raw` After Gene Filtering**:
   - In `AnnData`, slicing `adata[:, gene_mask]` subsets `adata.X` and `adata.layers`, but **does not subset `adata.raw`** (which intentionally retains the full pre-filtered gene dimension). If you want `adata.raw` to match the filtered gene set, reassign `adata.raw = adata.copy()` before log-normalization.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
