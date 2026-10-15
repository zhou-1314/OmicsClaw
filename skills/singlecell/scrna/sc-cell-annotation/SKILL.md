---
name: sc-cell-annotation
description: Load when assigning cell-type labels to a clustered scRNA AnnData via marker dictionaries,
  CellTypist, PopV, KNNPredict, SingleR, scmap, SCSA, or a manual cluster-to-label map. Skip when ranking
  marker genes per cluster (use sc-markers); condition-vs-control DE (use sc-de).
tags:
- singlecell
- scrna
- cell-annotation
- celltypist
- popv
- singler
- scmap
- scsa
- knnpredict
- markers
---

# Single-Cell Cluster & Cell-Type Annotation (`sc-cell-annotation`)

## 1. Method Decision Matrix & When to Use

Choose an annotation strategy based on tissue type, reference availability, and marker knowledge:

| Method (`--method` / `method=`) | Upstream Package | Best Used When | Required Input |
|---|---|---|---|
| `method="celltypist"` (`--method celltypist`) | `celltypist` | Human/mouse immune, gut, lung, or organ atlas models exist | Log1p-normalized `adata` normalized to `target_sum=1e4` |
| `method="markers"` (`--method markers`) | `scanpy.tl.score_genes` / `rank_genes_groups_overlap` | Curated canonical marker dictionary is provided or tissue lacks a pre-trained model | Clustered `adata` + `dict[cell_type, list[gene]]` |
| `method="scanvi"` (`--method scanvi`) | `scvi.model.SCANVI` | Annotated reference `AnnData` (`reference.h5ad`) is available for semi-supervised label transfer | Raw counts in `layers["counts"]` for query and reference |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Automated Annotation with `celltypist` (`method="celltypist"`)

```python
import celltypist
import scanpy as sc

# CellTypist requires log1p-normalized expression scaled to 10,000 counts per cell (CP10K)
adata_ct = adata.copy()
if "counts" in adata_ct.layers:
    adata_ct.X = adata_ct.layers["counts"].copy()
    sc.pp.normalize_total(adata_ct, target_sum=1e4)
    sc.pp.log1p(adata_ct)

# Run CellTypist with majority voting over existing Leiden clusters
predictions = celltypist.annotate(
    adata_ct,
    model="Immune_All_Low.pkl",
    majority_voting=True,
    over_clustering="leiden" if "leiden" in adata_ct.obs else None,
)
adata.obs["cell_type"] = predictions.predicted_labels["majority_voting"].astype(str)
adata.obs["cell_type_conf"] = predictions.probability_matrix.max(axis=1).values
```

### Pattern B: Canonical Marker Scoring & Cluster Assignment (`method="markers"`)

```python
import scanpy as sc
import pandas as pd

markers = {
    "T_cells": ["CD3D", "CD3E", "IL7R"],
    "B_cells": ["MS4A1", "CD79A", "CD79B"],
    "Monocytes": ["CD14", "LYZ", "FCGR3A"],
    "NK_cells": ["GNLY", "NKG7", "KLRD1"],
}

# Score each marker signature and map clusters by mean signature score
score_cols = []
for ct, genes in markers.items():
    present = [g for g in genes if g in adata.var_names]
    if present:
        col = f"score_{ct}"
        sc.tl.score_genes(adata, present, score_name=col, use_raw=False)
        score_cols.append(col)

cluster_means = adata.obs.groupby("leiden", observed=True)[score_cols].mean()
cluster_to_ct = cluster_means.idxmax(axis=1).str.replace("^score_", "", regex=True).to_dict()
adata.obs["cell_type"] = adata.obs["leiden"].map(cluster_to_ct).astype("category")
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-cell-annotation")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `annotate(adata, method="markers", markers=..., cluster_key="leiden", key_added="cell_type")`
- `annotation_summary(adata, key="cell_type")`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/annotated.h5ad`):
  - `adata.obs["cell_type"]`: categorical or string cell-type label for every cell (no `NaN` values).
  - Optional confidence score in `adata.obs["cell_type_conf"]`.
- **Annotation Summary Table** (`tables/cell_type_counts.csv`):
  - Columns: `cell_type`, `n_cells`, `fraction` (and cluster-to-cell-type mapping table).

## 4. Gotchas & Troubleshooting

1. **CellTypist CP10K Normalization Requirement**:
   - `celltypist.annotate` validates that `adata.X` is `log1p`-normalized to **10,000 counts per cell** (`np.expm1(adata.X[0]).sum() ≈ 1e4`). If `adata` was normalized to median library size, scran pooling, or scaled with `sc.pp.scale`, reconstruct a temporary CP10K log1p copy from `adata.layers["counts"]`.
2. **Gene Symbol Namespace Mismatch (Ensembl IDs vs. HGNC)**:
   - Both `celltypist` models and canonical marker dictionaries use gene symbols (`CD3D`, `MS4A1`). If `adata.var_names` contains Ensembl IDs (`ENSG00000167286`), map `adata.var["gene_symbols"]` to `adata.var_names` (`adata.var_names_make_unique()`) before scoring.
3. **Cell-Level Noise vs. Cluster Majority Voting**:
   - Per-cell classifier predictions often fragment homogeneous clusters due to dropout. Always enable `majority_voting=True` (or aggregate marker scores at the `leiden` cluster level) so transcriptionally coherent clusters receive consistent labels.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `celltypist`, `matplotlib`, `numpy`, `pandas`, `popv`, `scanpy`, `scipy`, `seaborn`
