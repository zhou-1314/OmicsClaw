---
name: sc-qc
description: Load when computing per-cell QC metrics (n_genes, total counts, mt%, ribo%) on a single-cell
  AnnData before filtering. Skip when reads are still raw FASTQ (use sc-fastq-qc); you want to filter
  cells now (use sc-filter).
trigger: scRNA QC, single-cell QC, quality control, mitochondrial percentage, ribosomal percentage, QC violin, QC scatter, n genes per cell
tags:
- singlecell
- scrna
- qc
- mitochondrial
- ribosomal
---

# Single-Cell RNA-seq Quality Control Metrics (`sc-qc`)

## 1. Method Decision Matrix & When to Use

Use this playbook to compute and inspect per-cell and per-gene quality control (QC) distributions (`n_genes_by_counts`, `total_counts`, `pct_counts_mt`, `pct_counts_ribo`, `pct_counts_hb`) on raw single-cell `AnnData` before choosing filtering cutoffs.

| QC Metric | Biological Meaning | Typical Flagging Threshold | Adaptive MAD Alternative |
|---|---|---|---|
| `n_genes_by_counts` | Transcriptomic complexity per barcode | $< 200$ (empty droplet / debris) or $> 6,000$ (putative doublet) | $> 3	ext{--}5$ MAD below median on $\log_{10}$ scale |
| `total_counts` | Library size (total UMIs per cell) | $< 500$ UMIs | $> 3	ext{--}5$ MAD below median on $\log_{10}$ scale |
| `pct_counts_mt` | Mitochondrial transcript leakage | $> 15	ext{--}20\%$ (PBMC/solid tissue); $> 5\%$ for single-nucleus (`snRNA-seq`) | $> 3$ MAD above sample median |
| `pct_counts_hb` | Ambient erythrocyte contamination | $> 5\%$ in non-erythroid tissues | Inspect per-sample distribution |

## 2. How to Run (Idiomatic Upstream Patterns)

```python
import scanpy as sc
import numpy as np
import pandas as pd

# 1. Flag mitochondrial, ribosomal, and hemoglobin gene families (species-aware)
adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
adata.var["ribo"] = adata.var_names.str.startswith(("RPS", "RPL", "Rps", "Rpl"))
adata.var["hb"] = adata.var_names.str.contains("^HB[^(P)]|^Hb[^(p)]", regex=True)

# 2. Compute QC metrics in-place
sc.pp.calculate_qc_metrics(
    adata,
    qc_vars=["mt", "ribo", "hb"],
    percent_top=None,
    log1p=True,
    inplace=True,
)

# 3. Summarize per-cell distributions
qc_summary = {
    "n_cells": int(adata.n_obs),
    "n_genes": int(adata.n_vars),
    "median_genes": float(adata.obs["n_genes_by_counts"].median()),
    "median_umis": float(adata.obs["total_counts"].median()),
    "median_mt_pct": float(adata.obs["pct_counts_mt"].median()),
}
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-qc")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `calculate_qc(adata, species="human")`
- `qc_summary(adata)`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/qc_annotated.h5ad`):
  - `adata.obs` contains `n_genes_by_counts`, `total_counts`, `pct_counts_mt`, `pct_counts_ribo`, `pct_counts_hb`.
  - `adata.var` contains `n_cells_by_counts`, `total_counts`, `mean_counts`, `pct_dropout_by_counts`.
- **QC Summary Table** (`tables/qc_summary.json` or `tables/qc_metrics.csv`): cell count and quantiles (`median`, `IQR`, `95th percentile`) for each QC metric.

## 4. Gotchas & Troubleshooting

1. **Single-Nucleus RNA-seq (`snRNA-seq`) vs. Whole-Cell `scRNA-seq`**:
   - Intact nuclei should contain virtually zero cytoplasmic mitochondria; in `snRNA-seq`, `pct_counts_mt > 5%` indicates cytoplasmic stripping/ambient contamination, whereas metabolically active cardiomyocytes or tumor cells in whole-cell `scRNA-seq` may legitimately reach `20–30%`.
2. **Verify Mitochondrial Gene Matching**:
   - Always check `int(adata.var["mt"].sum()) > 0`. If `0` mitochondrial genes matched, check whether `adata.var_names` uses Ensembl IDs (`ENSG...`) instead of HGNC/MGI gene symbols.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
