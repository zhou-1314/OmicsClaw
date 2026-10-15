---
name: sc-multi-count
description: Load when merging multiple single-sample scRNA-seq count matrices (one per sample-from-sc-count)
  into a single downstream-ready AnnData with sample labels. Skip when input is one already-merged AnnData
  (use sc-standardize-input); FASTQ→counts on each sample (use sc-count).
trigger: merge count matrices, multi-sample count, aggregate samples, combine count outputs, cellranger aggr alternative
tags:
- singlecell
- scrna
- multi-sample
- merge
- aggregation
---

# Multi-Sample Single-Cell Count Matrix Aggregation (`sc-multi-count`)

## 1. Method Decision Matrix & When to Use

Use this playbook to concatenate multiple single-sample raw count `AnnData` objects into a unified multi-sample `AnnData` with collision-free cell barcodes and explicit sample provenance before `sc-qc` and `sc-batch-integration`.

| Concatenation Mode | Parameter (`join=`) | Feature Alignment Behavior | Best Used When |
|---|---|---|---|
| **Union of Features (Default)** | `anndata.concat(..., join="outer", fill_value=0)` | Retains all genes present in **any** sample; fills absent sample-gene entries with `0` | Merging samples quantified against slightly different gene annotations or pre-filtered per-sample matrices |
| **Intersection of Features** | `anndata.concat(..., join="inner")` | Retains only genes shared across **every** input sample | All samples were quantified with the exact same reference transcriptome index (`cellranger` / `STARsolo`) |

> **Scope Distinction**: `sc-multi-count` stacks raw count matrices and assigns `obs["sample_id"]` without subsampling reads (unlike `cellranger aggr` depth normalization) or removing batch effects (use `sc-batch-integration` downstream).

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Multi-Sample Concatenation with Collision-Free Barcodes (`anndata.concat`)

```python
import anndata as ad
import numpy as np
import pandas as pd
import scanpy as sc

sample_files = {
    "donor1_ctrl": "sample1_filtered_feature_bc_matrix.h5",
    "donor2_ctrl": "sample2_filtered_feature_bc_matrix.h5",
    "donor3_stim": "sample3_filtered_feature_bc_matrix.h5",
}

adatas = []
for sample_id, path in sample_files.items():
    a = sc.read_10x_h5(path) if path.endswith(".h5") else sc.read_h5ad(path)
    a.var_names_make_unique()
    a.obs["sample_id"] = sample_id
    # Prefix cell barcodes with sample_id to prevent 10x barcode collisions across channels!
    a.obs_names = [f"{sample_id}_{bc}" for bc in a.obs_names]
    adatas.append(a)

# Concatenate along observations (axis=0) with zero-fill for any sample-specific genes
merged = ad.concat(adatas, axis=0, join="outer", fill_value=0, merge="same")
merged.obs["sample_id"] = merged.obs["sample_id"].astype("category")
merged.layers["counts"] = merged.X.copy()
merged.raw = merged.copy()
```

### Pattern B: Per-Sample Depth & Complexity Audit Table

```python
mat = merged.layers["counts"]
per_cell = pd.DataFrame({
    "sample_id": merged.obs["sample_id"].values,
    "total_counts": np.asarray(mat.sum(axis=1)).ravel(),
    "n_genes": np.asarray((mat > 0).sum(axis=1)).ravel(),
})
per_sample_df = per_cell.groupby("sample_id", observed=True).agg(
    n_cells=("total_counts", "count"),
    median_counts=("total_counts", "median"),
    median_genes=("n_genes", "median"),
    total_umis=("total_counts", "sum"),
).reset_index()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-multi-count")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `merge_samples(adatas, sample_ids=None, sample_key="sample_id", join="outer")`
- `per_sample_summary(adata, sample_key="sample_id")`
- `barcode_metrics(adata, sample_key="sample_id")`

## 3. Output Format & Verification Contract

- **Merged Count AnnData** (`intermediate/processed.h5ad`):
  - `adata.obs["sample_id"]`: categorical sample identifier with $\ge 2$ distinct levels.
  - `adata.obs_names`: globally unique sample-prefixed cell barcodes (`adata.obs_names.is_unique == True`).
  - `adata.layers["counts"]` and `adata.X`: non-negative integer count matrix of shape $(\sum_i n_{\text{obs}, i}, n_{\text{vars}})$.
- **Summary Tables** (`tables/per_sample_summary.csv`, `tables/barcode_metrics.csv`):
  - `per_sample_summary.csv` columns: `sample_id`, `n_cells`, `median_counts`, `median_genes`, `total_umis`.
- **Verification Invariants**:
  - Assert `merged.n_obs == sum(a.n_obs for a in adatas)` and `merged.obs_names.is_unique`.

## 4. Gotchas & Troubleshooting

1. **10x Barcode Collisions Across Gel-Bead Lanes**:
   - Every 10x Chromium channel draws from the same barcode whitelist (`AAACCCAAGAAACACT-1`), so identical barcode strings appear in multiple samples. Always prefix `obs_names` with `sample_id` (or pass `index_unique="-"` to `anndata.concat`) so cells from different donors never collide.
2. **Never Concatenate Log-Normalized and Raw Count Matrices Together**:
   - All input `AnnData` objects must be in the **same raw integer count state** before `anndata.concat`. Mixing an already CP10K-normalized `.h5ad` with a raw count `.h5ad` corrupts library-size normalization, HVG selection, and pseudobulk aggregation.
3. **`anndata.concat` Drops `.var` Metadata Columns By Default (`merge=None`)**:
   - Calling `ad.concat(adatas)` without `merge="same"` or `merge="first"` discards shared `.var` columns such as `gene_ids` or `feature_types`. Always pass `merge="same"` (or `merge="first"`) to preserve concordant gene metadata.
4. **Run Doublet Detection Per-Sample (`batch_key="sample_id"`) After Merging**:
   - Once samples are merged into a single `AnnData`, any downstream `sc-doublet-detection`, HVG selection (`sc.pp.highly_variable_genes`), or `sc-batch-integration` step **must** receive `batch_key="sample_id"`.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
