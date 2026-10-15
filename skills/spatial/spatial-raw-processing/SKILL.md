---
name: spatial-raw-processing
description: Load when converting spatial transcriptomics raw FASTQ pairs through ST-Pipeline into a `raw_counts.h5ad`
  ready for spatial-preprocess. Skip when input is already a count-matrix AnnData (use spatial-preprocess);
  non-spatial bulk / scRNA FASTQ (use bulkrna-read-qc).
trigger: spatial raw processing, raw spatial fastq, spatial fastq, st_pipeline, st pipeline, barcode coordinates, ids file, visium raw fastq, slide-seq fastq, slideseq fastq, upstream spatial processing
tags:
- spatial
- raw-processing
- fastq
- st-pipeline
- visium
- slideseq
---

# Spatial Raw FASTQ & Barcode-Coordinate Processing (`spatial-raw-processing`)

## 1. Method Decision Matrix & When to Use

Use this playbook when assembling raw spatial transcriptomics sequencing outputs (FASTQ pairs via `st_pipeline`, 10x SpaceRanger outputs, or Slide-seq / Stereo-seq count + coordinate tables) into a standardized, coordinate-aligned `raw_counts.h5ad` ready for `spatial-preprocess`:

| Input Format / Platform | Upstream Tool / Reader | Coordinate Source | Output Matrix Convention |
|---|---|---|---|
| **Raw Spatial FASTQ Pairs** (Legacy ST, Visium, Slide-seq) | `st_pipeline` + STAR index + barcode `ids_file` | Barcode whitelist `(x, y)` mapping table (`stdata.tsv`) | Spot $\times$ gene integer counts (`CSR` sparse) + `adata.obsm["spatial"]` |
| **10x SpaceRanger Directory** (`filtered_feature_bc_matrix.h5` + `spatial/`) | `squidpy.read.visium` / `scanpy.read_visium` | `spatial/tissue_positions.csv` + `scalefactors_json.json` | Spot $\times$ gene CSR counts + H&E images in `adata.uns["spatial"]` |
| **Slide-seq / Stereo-seq / Generic TSV Matrices** | `pandas` / `scipy.io.mmread` + `anndata` | Bead/bin barcode coordinate file (`barcode`, `x`, `y`) | Intersected barcode CSR counts + `adata.obsm["spatial"]` `(n_obs, 2)` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Loading 10x Visium SpaceRanger Outputs (`squidpy` / `scanpy`)

```python
import squidpy as sq
import numpy as np

# Load filtered count matrix, tissue_positions, and H&E scalefactors
adata = sq.read.visium(path="spaceranger_out/", counts_file="filtered_feature_bc_matrix.h5")
adata.var_names_make_unique()

# Preserve raw integer counts in adata.layers["counts"] and verify spatial coordinates
adata.layers["counts"] = adata.X.copy()
adata.obsm["spatial"] = np.asarray(adata.obsm["spatial"], dtype=float)
```

### Pattern B: Assembling `st_pipeline` or Slide-seq Count + Coordinate Tables into `AnnData`

```python
import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse

# 1. Read spot-by-gene counts and barcode-to-(x, y) coordinate table
counts_df = pd.read_csv("stdata_counts.tsv", sep="\t", index_col=0)
coords_df = pd.read_csv("barcode_coords.tsv", sep="\t", index_col=0)  # columns: ["x", "y"]

# 2. Strictly intersect and order barcodes across both tables
common_bcs = counts_df.index.intersection(coords_df.index)
counts_sub = counts_df.loc[common_bcs]
coords_sub = coords_df.loc[common_bcs, ["x", "y"]].astype(float)

# 3. Construct sparse CSR AnnData with standard spatial metadata
X_csr = sparse.csr_matrix(counts_sub.to_numpy(dtype=np.float32))
adata = ad.AnnData(
    X=X_csr,
    obs=pd.DataFrame(index=common_bcs.astype(str)),
    var=pd.DataFrame(index=counts_sub.columns.astype(str)),
)
adata.var_names_make_unique()
adata.layers["counts"] = X_csr.copy()
adata.obsm["spatial"] = coords_sub.to_numpy(dtype=float)
adata.obs["x"] = coords_sub["x"].to_numpy()
adata.obs["y"] = coords_sub["y"].to_numpy()
adata.obs["total_counts"] = np.asarray(X_csr.sum(axis=1)).ravel()
adata.obs["n_genes_by_counts"] = np.asarray((X_csr > 0).sum(axis=1)).ravel()
adata.write_h5ad("intermediate/raw_counts.h5ad")
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-raw-processing")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `build_spatial_anndata(counts_df, coords_df)`
- `raw_qc_summary(adata)`

## 3. Output Format & Verification Contract

- **Raw Spatial Count AnnData** (`intermediate/raw_counts.h5ad`):
  - `adata.X` and `adata.layers["counts"]`: non-negative integer UMI counts stored as `scipy.sparse.csr_matrix`.
  - `adata.obsm["spatial"]`: 2D `float64` NumPy array of shape `(n_obs, 2)` with zero `NaN` or `inf` entries.
  - `adata.obs[["total_counts", "n_genes_by_counts"]]`: per-spot sequencing depth and gene detection metrics.
- **Barcode QC Summary Table** (`tables/barcode_qc.csv`):
  - Per-spot `barcode`, `x`, `y`, `total_counts`, `n_genes_by_counts`.
- **Verification**:
  - Assert `adata.obsm["spatial"].shape == (adata.n_obs, 2)`, `not np.isnan(adata.obsm["spatial"]).any()`, and `adata.X.min() >= 0`.

## 4. Gotchas & Troubleshooting

1. **Transposed Count Matrices (Gene $\times$ Spot vs. Spot $\times$ Gene)**:
   - Legacy `st_pipeline` outputs spot $\times$ gene tables (`x_y` rows), whereas 10x MTX / Drop-seq tools output gene $\times$ spot tables. Always verify whether barcodes are on `.index` or `.columns` before intersecting with `coords_df`.
2. **SpaceRanger v1 vs. v2+ `tissue_positions.csv` Header Change**:
   - SpaceRanger `<2.0` wrote a headerless `spatial/tissue_positions_list.csv`, whereas SpaceRanger `>=2.0` writes `spatial/tissue_positions.csv` with a header row (`barcode,in_tissue,array_row,array_col,pxl_row_in_fullres,pxl_col_in_fullres`). Older `sc.read_visium` versions can fail or ingest the header row as a string coordinate; use `squidpy.read.visium` or cast `adata.obsm["spatial"]` to `float`.
3. **Image Coordinate Convention `(x, y) = (pxl_col, pxl_row)`**:
   - In `adata.obsm["spatial"]`, column 0 is horizontal $x$ (`pxl_col_in_fullres`) and column 1 is vertical $y$ (`pxl_row_in_fullres`). Swapping `(row, col)` rotates the tissue $90^\circ$ relative to the H&E image.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `PyYAML`, `scipy`, `seaborn`
