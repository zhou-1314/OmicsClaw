---
name: spatial-annotate
description: Load when assigning per-spot cell-type labels on a spatial AnnData via marker-gene scoring
  or scRNA-reference mapping (Tangram / scANVI / CellAssign). Skip when computing spot-level
  cell-type proportions (use spatial-deconv); tissue-domain detection (use spatial-domains).
trigger: cell type annotation, annotate cell types, Tangram, scANVI, CellAssign, marker genes, label transfer, spatial annotation
tags:
- spatial
- annotation
- cell-type
- marker-based
- tangram
- scanvi
- cellassign
---

# Spatial Cell-Type & Spot Annotation (`spatial-annotate`)

## 1. Method Decision Matrix & When to Use

Use this playbook to assign discrete cell-type identities to single-cell spatial technologies (Xenium, CosMx, MERFISH, Stereo-seq cell-binned) or dominant lineage labels to spatial spots. For continuous multi-cell proportion estimation on $55\ \mu\text{m}$ Visium spots, use `spatial-deconv`; for unsupervised microenvironment/niche clustering, use `spatial-domains`.

| Method (`method=`) | Upstream Library | Reference Required | Input Matrix Assumption | Best Used When |
|---|---|---|---|---|
| **Marker Scoring** (`marker_based`) | `scanpy.tl.score_genes` | Curated marker gene dict | Log-normalized `adata.X` | Fast, reference-free annotation using canonical lineage markers or targeted panel genes |
| **Tangram Cell Mapping** (`tangram`) | `tangram` (`tg.map_cells_to_space` + `tg.project_cell_annotations`) | Annotated `scRNA-seq` `AnnData` | Shared gene intersection (`adata.X` or raw counts) | Projecting fine-grained scRNA-seq cell types onto high-plex imaging or spot data |
| **scANVI Transfer** (`scanvi`) | `scvi-tools` (`SCVI` + `SCANVI`) | Annotated `scRNA-seq` `AnnData` | Raw integer counts (`adata.layers["counts"]`) | Joint batch-corrected latent space transfer across strong platform effects |
| **CellAssign** (`cellassign`) | `scvi.external.CellAssign` | Binary marker $\times$ cell-type matrix | Raw integer counts + size factors | Probabilistic marker-driven assignment accounting for library size |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Marker-Gene Panel Scoring (`scanpy`)

```python
import numpy as np
import pandas as pd
import scanpy as sc

markers = {
    "T_cell": ["CD3D", "CD3E", "CD8A", "IL7R"],
    "B_cell": ["MS4A1", "CD79A", "CD19"],
    "Myeloid": ["LYZ", "CD68", "C1QA", "APOE"],
    "Epithelial_Tumor": ["EPCAM", "KRT8", "KRT18", "KRT19"],
    "Fibroblast": ["COL1A1", "COL1A2", "DCN", "LUM"],
}

score_cols = []
for cell_type, genes in markers.items():
    present = [g for g in genes if g in adata.var_names]
    if present:
        col = f"score_{cell_type}"
        sc.tl.score_genes(adata, gene_list=present, score_name=col, use_raw=False)
        score_cols.append((cell_type, col))

score_mat = np.column_stack([adata.obs[col].to_numpy() for _, col in score_cols])
best_idx = np.argmax(score_mat, axis=1)
adata.obs["cell_type"] = pd.Categorical([score_cols[i][0] for i in best_idx])
adata.obs["cell_type_score"] = score_mat[np.arange(adata.n_obs), best_idx]
```

### Pattern B: scRNA-seq Reference Label Transfer (`tangram`)

```python
import numpy as np
import pandas as pd
import tangram as tg

# Prepare shared training genes between scRNA reference (adata_sc) and spatial query (adata_sp)
markers_shared = list(np.intersect1d(adata_sc.var_names, adata_sp.var_names))
tg.pp_adatas(adata_sc, adata_sp, genes=markers_shared)

# Map reference cells onto spatial locations at cluster level
ad_map = tg.map_cells_to_space(
    adata_sc,
    adata_sp,
    mode="clusters",
    cluster_label="cell_type",
    density_prior="rna_count_based",
    num_epochs=500,
    device="cpu",
)
tg.project_cell_annotations(ad_map, adata_sp, annotation="cell_type")

# Assign dominant cell type fromadata_sp.obsm["tangram_ct_pred"]
pred_df = adata_sp.obsm["tangram_ct_pred"]
adata_sp.obs["cell_type"] = pd.Categorical(pred_df.idxmax(axis=1))
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-annotate")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `score_marker_labels(adata, markers, key_added="cell_type", min_score=0.0)`
- `annotation_summary(adata, key="cell_type")`

## 3. Output Format & Verification Contract

- **Annotated Spatial AnnData** (`intermediate/annotated.h5ad`):
  - `adata.obs["cell_type"]`: categorical cell-type assignment for every spot/cell.
  - `adata.obs["cell_type_score"]` (or `adata.obsm["tangram_ct_pred"]`): per-cell assignment confidence or probability matrix.
  - `adata.obsm["spatial"]`: preserved 2D tissue coordinates `(n_obs, 2)`.
- **Annotation Summary Table** (`tables/annotation_summary.csv`):
  - Columns: `cell_type`, `n_spots`, `proportion`.
- **Verification**:
  - Confirm `adata.obs["cell_type"].isna().sum() == 0` and verify anatomical plausibility by plotting `sc.pl.spatial(adata, color="cell_type")` against H&E or DAPI morphology.

## 4. Gotchas & Troubleshooting

1. **Targeted Spatial Panels (Xenium / CosMx / MERFISH) Missing Canonical Markers**:
   - Imaging-based spatial platforms profile 300–1,000 genes and often omit classical scRNA-seq markers. Always intersect your marker dictionary with `adata.var_names` before calling `sc.tl.score_genes`, and require at least 2–3 present genes per cell type.
2. **Visium Multi-Cell Spot Admixture**:
   - Assigning a single argmax `cell_type` to a $55\ \mu\text{m}$ Visium spot masks infiltrating immune cells inside epithelial or stromal regions. When rare populations matter on spot-based arrays, use `spatial-deconv` to retain continuous proportions in `adata.obsm`.
3. **Scanpy `use_raw=True` Default in `sc.tl.score_genes`**:
   - If `adata.raw` exists from an upstream workflow, `sc.tl.score_genes` defaults to scoring on `adata.raw` rather than `adata.X`. Always pass `use_raw=False` explicitly so gene scoring uses the normalized `adata.X` matrix.
4. **Gene Symbol Case & Ensembl ID Mismatch in Reference Mapping**:
   - `tangram.pp_adatas` lowercases gene names internally by default, whereas `scANVI` requires exact `var_names` matching. Standardize both reference and spatial query to HGNC/MGI gene symbols (`adata.var_names_make_unique()`) prior to alignment.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `scvi-tools`, `seaborn`, `tangram-sc`, `torch`
