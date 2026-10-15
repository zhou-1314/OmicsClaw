---
name: sc-ambient-removal
description: Load when removing ambient RNA contamination from droplet-based scRNA-seq using a simple
  subtraction path, CellBender, or SoupX. Skip when the contamination is multiplet barcodes (use sc-doublet-detection);
  before counts exist (use sc-count).
trigger: ambient RNA, ambient removal, cellbender, contamination, background RNA
tags:
- singlecell
- scrna
- ambient
- cellbender
- soupx
- contamination
---

# Single-Cell Ambient RNA Decontamination (`sc-ambient-removal`)

## 1. Method Decision Matrix & When to Use

Select an ambient RNA decontamination strategy based on whether the raw unfiltered droplet matrix (`raw_feature_bc_matrix`) and GPU acceleration are available:

| Method (`--method` / `method=`) | Upstream Package / CLI | Required Inputs | Statistical Model | Best Used When |
|---|---|---|---|---|
| **CellBender** (`method="cellbender"`) | `cellbender remove-background` | Unfiltered `raw_feature_bc_matrix.h5` (includes empty droplets) | Deep generative variational autoencoder over ambient + cell counts | High ambient background (e.g., snRNA-seq, solid tumor dissociation, high-throughput 10x) with GPU available |
| **SoupX** (`method="soupx"`) | R `SoupX` (`autoEstCont` + `adjustCounts`) | Both `raw_feature_bc_matrix` (empty droplets) and `filtered_feature_bc_matrix` + rough clusters | Empirical background profile from empty droplets + marker exclusion contamination fraction $\rho$ | CPU-only workflows where both raw and filtered 10x matrices are available |
| **Simple Profile Subtraction** (`method="simple"`) | `numpy` / `scipy.sparse` (`kernel.py`) | Filtered `AnnData` (+ optional empty-droplet `AnnData`) and fixed `contamination` ($\rho \in [0, 1)$) | Deterministic subtraction $\max(0, X_{cg} - \rho \cdot N_c \cdot p_g)$ | Quick exploratory decontamination or when `raw_feature_bc_matrix` was not retained |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Deep Generative Background Removal with `CellBender` (CLI + `scanpy`)

```bash
# Run CellBender on the UNFILTERED raw_feature_bc_matrix.h5 (requires empty droplet plateau)
cellbender remove-background \
    --input raw_feature_bc_matrix.h5 \
    --output cellbender_out.h5 \
    --expected-cells 5000 \
    --total-droplets-included 25000 \
    --fpr 0.01 \
    --epochs 150 \
    --cuda
```

```python
import scanpy as sc

# Load CellBender-corrected counts and align with original raw counts
adata_clean = sc.read_10x_h5("cellbender_out_filtered.h5")
adata_Clean_names = adata_clean.obs_names
adata = adata[adata.obs_names.isin(adata_Clean_names)].copy()
adata.layers["counts"] = adata.X.copy()  # Preserve pre-decontamination integer counts
adata.X = adata_clean[adata.obs_names, adata.var_names].X.copy()
```

### Pattern B: Ambient Profile Subtraction in Python (`numpy` / `scipy.sparse`)

```python
import numpy as np
import scipy.sparse as sp

# 1. Preserve original unadjusted integer counts in layers["counts"]
raw_mat = adata.layers["counts"].copy() if "counts" in adata.layers else adata.X.copy()
adata.layers["counts"] = raw_mat.copy()

# 2. Estimate ambient gene distribution p_g (from empty droplets if available, else mean profile)
gene_totals = np.asarray(raw_mat.sum(axis=0)).ravel().astype(np.float64)
ambient_profile = gene_totals / max(gene_totals.sum(), 1.0)

# 3. Subtract expected ambient contribution per cell and clip at zero
contamination = 0.05
dense = raw_mat.toarray().astype(np.float32) if sp.issparse(raw_mat) else np.asarray(raw_mat, dtype=np.float32)
cell_depths = dense.sum(axis=1, keepdims=True)
corrected = np.clip(dense - contamination * (cell_depths * ambient_profile[None, :]), 0.0, None)
adata.X = sp.csr_matrix(corrected)
adata.uns["ambient_correction"] = {"method": "simple", "contamination": contamination}
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-ambient-removal")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `remove_ambient(adata, contamination=0.05, empty_droplets=None, layer="counts")`
- `estimate_ambient_profile(adata, empty_droplets=None)`
- `ambient_summary(adata, raw_layer="counts")`

## 3. Output Format & Verification Contract

- **Decontaminated AnnData** (`intermediate/processed.h5ad`):
  - `adata.X`: non-negative ambient-corrected expression matrix (`adata.X.min() >= 0`).
  - `adata.layers["counts"]`: original pre-correction raw integer counts (same `(n_obs, n_vars)` shape).
  - `adata.uns["ambient_correction"]`: dictionary recording `method` and `contamination` fraction.
- **Diagnostic Tables** (`tables/correction_summary.csv`, `tables/counts_comparison.csv`, `tables/ambient_profile.csv`):
  - Per-cell count comparison (`cell_id`, `counts_before`, `counts_after`), global reduction summary (`mean_counts_before`, `mean_counts_after`, `reduction_pct`), and top ambient genes (`gene`, `fraction`, such as *HBB*, *HBA1*, *IGKC*, *ALB*, *MALAT1*).
- **Verification Invariants**:
  - Assert `(adata.X >= 0).all()` (or `adata.X.data.min() >= 0` for sparse matrices) and verify that `adata.X.sum() <= adata.layers["counts"].sum()`.

## 4. Gotchas & Troubleshooting

1. **CellBender & SoupX Require Empty Droplets (`raw_feature_bc_matrix`)**:
   - Both `CellBender` and `SoupX` estimate the cell-free soup composition from low-UMI empty droplets ($10\text{--}100$ UMIs). Passing an already cell-filtered matrix (`filtered_feature_bc_matrix`) deprives `CellBender` of the empty-droplet plateau and causes convergence failure or over-subtraction.
2. **Contamination Parameter Scale (`[0, 1)` Fraction vs. Percentage)**:
   - `contamination` is a proportion in $[0, 1)$ (e.g., `0.05` for $5\%$ ambient RNA), never a percentage (`5.0`). Values $\ge 1.0$ or $< 0.0$ raise a `ValueError`.
3. **Preserve Pre-Correction Integer Counts for Count-Based Models**:
   - Simple subtraction and default `SoupX` produce continuous non-integer values in `adata.X`. Always preserve original integer counts in `adata.layers["counts"]` (or request integer rounding in `SoupX::adjustCounts(..., roundToInt=TRUE)`) before running `scVI` or `pydeseq2`.
4. **Ambient RNA vs. Multiplet Barcodes**:
   - Ambient removal subtracts ubiquitous background transcripts (e.g., hemoglobin *HBB* or hepatocyte *ALB* leaking into all cells) but does **not** remove two-cell co-encapsulation events. Run `sc-doublet-detection` separately to flag multiplet barcodes.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `cellbender`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `torch`
