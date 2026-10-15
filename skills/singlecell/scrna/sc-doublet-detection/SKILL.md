---
name: sc-doublet-detection
description: Load when annotating putative doublets in single-cell RNA-seq using Scrublet, DoubletDetection,
  DoubletFinder, scDblFinder, or scds. Skip when ambient RNA is the contamination problem (use sc-ambient-removal);
  before counts exist (use sc-fastq-qc).
tags:
- singlecell
- scrna
- doublet
- scrublet
- doubletfinder
- scdblfinder
---

# Single-Cell Multiplet / Doublet Detection (`sc-doublet-detection`)

## 1. Method Decision Matrix & When to Use

Select a computational doublet caller based on dataset structure, sample multiplexing, and runtime environment:

| Method (`--method` / `method=`) | Upstream Package | Core Algorithm | Required Input | Best Used When |
|---|---|---|---|---|
| **Scrublet** (`method="scrublet"`) | `scanpy.pp.scrublet` / `scrublet` | Simulates artificial doublets by summing random pairs of observed cells; scores $k$NN fraction in PCA space | Raw integer UMI counts (`adata.layers["counts"]` or `adata.X`) + `batch_key` | **Default Python workflow**; fast, deterministic, supports per-batch execution via `batch_key` |
| **DoubletDetection** (`method="doubletdetection"`) | `doubletdetection.BoostClassifier` | Iterative boosted synthetic doublet classification (`n_iters=10`) | Raw integer UMI counts | When Scrublet's bimodal score histogram threshold is ambiguous |
| **scDblFinder** (`method="scdblfinder"`) | R `scDblFinder` (Bioconductor) | Cluster-guided artificial doublet generation + gradient boosted decision trees (`xgboost`) | Raw integer counts (`SingleCellExperiment`) + `samples=` vector | Top benchmark accuracy in complex multi-cluster tissues when R/Bioconductor is available |
| **scds** (`method="scds"`) | R `scds` (`cxds` / `bcds` / `hybrid`) | Gene-pair co-expression (`cxds`) + binary classifier (`bcds`) | Raw or binary count matrix | Ultra-fast co-expression scoring complementary to $k$NN synthetic doublet methods |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Batch-Aware Scrublet via Scanpy (`scanpy.pp.scrublet`)

```python
import scanpy as sc
import pandas as pd

# 1. Scrublet requires raw counts; scale expected_doublet_rate ~0.008 per 1,000 captured cells (~0.06 for 7,500 cells)
adata_raw = adata.copy()
if "counts" in adata_raw.layers:
    adata_raw.X = adata_raw.layers["counts"].copy()

sc.pp.scrublet(
    adata_raw,
    batch_key="sample_id" if "sample_id" in adata_raw.obs else None,
    expected_doublet_rate=0.06,
    random_state=0,
)

# 2. Copy doublet annotations back to working AnnData (annotate first, inspect before dropping)
adata.obs["doublet_score"] = adata_raw.obs["doublet_score"]
adata.obs["predicted_doublet"] = adata_raw.obs["predicted_doublet"].astype(bool)
adata.obs["doublet_classification"] = adata.obs["predicted_doublet"].map({True: "doublet", False: "singlet"})
```

### Pattern B: Boosted Synthetic Doublet Consensus (`doubletdetection`)

```python
import doubletdetection
import numpy as np

raw_mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
clf = doubletdetection.BoostClassifier(n_iters=10, clustering_algorithm="leiden", standard_scaling=False, random_state=0)
labels = clf.fit(raw_mat).predict(p_thresh=1e-7, voter_thresh=0.8)

adata.obs["doublet_score"] = clf.voting_average_
adata.obs["predicted_doublet"] = (labels == 1)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-doublet-detection")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `detect_doublets(adata, batch_key=None, expected_doublet_rate=0.06, threshold=None, random_state=0)`
- `doublet_summary(adata)`
- `group_doublet_rates(adata, groupby="sample_id")`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/processed.h5ad`):
  - `adata.obs["doublet_score"]`: continuous floating-point doublet likelihood score per cell (`float`, no `NaN`).
  - `adata.obs["predicted_doublet"]`: boolean call (`True` = doublet, `False` = singlet).
  - `adata.obs["doublet_classification"]`: `"doublet"` or `"singlet"` string label.
- **Summary Tables** (`tables/doublet_calls.csv`, `tables/summary.csv`, `tables/group_summary.csv`):
  - Per-cell calls (`cell_id`, `doublet_score`, `predicted_doublet`, `doublet_classification`) and per-group rates (`n_cells`, `n_doublets`, `doublet_rate`, `median_score`).
- **Verification Invariants**:
  - Cell count is conserved (`adata_out.n_obs == adata_in.n_obs`; this skill annotates doublets, whereas `sc-filter` removes them).
  - Overall predicted doublet rate is plausible ($0\% < \text{rate} < 25\%$).

## 4. Gotchas & Troubleshooting

1. **Always Run Doublet Detection Per-Sample (`batch_key`), Never Across Pooled Samples**:
   - Physical doublets can only form between two cells co-encapsulated inside the **same microfluidic channel / sample**. Running Scrublet on a merged multi-sample `AnnData` without `batch_key="sample_id"` synthesizes impossible cross-sample chimeric doublets and distorts the null distribution.
2. **Expected Doublet Rate Scales Linearly with Captured Cell Count**:
   - On 10x Chromium, multiplet rate increases by $\approx 0.8\%$ per $1,000$ cells recovered ($\sim 0.8\%$ at $1,000$ cells, $\sim 4.0\%$ at $5,000$ cells, $\sim 8.0\%$ at $10,000$ cells). Adjust `expected_doublet_rate` to match sample loading density.
3. **Homotypic Doublets & Transitional Progenitors**:
   - Synthetic doublet callers exclusively detect **heterotypic** doublets (two transcriptionally distinct cell types). Homotypic doublets (two T cells in one droplet) look transcriptionally identical to singlets, while genuine proliferating or differentiating bridge populations may receive elevated doublet scores—always inspect cluster markers before deleting an entire transitional cluster.
4. **Automatic Threshold Failure on Unimodal Score Distributions**:
   - When `sc.pp.scrublet` cannot find a clean bimodal minimum in the simulated doublet histogram, `predicted_doublet` may default to all `False` (or require a manual `threshold=0.25`). Always check `doublet_summary(adata)` after running.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `doubletdetection`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `scrublet`, `seaborn`
