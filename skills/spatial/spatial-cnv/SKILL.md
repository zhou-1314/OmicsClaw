---
name: spatial-cnv
description: Load when inferring copy-number variation on a preprocessed spatial AnnData with gene
  coordinates in `var` via infercnvpy (sliding-window) or Numbat (R). Skip when
  `var["chromosome"]` / `var["start"]` / `var["end"]` metadata is missing; no normal-reference
  subset can be defined.
trigger: copy number variation, CNV, inferCNV, infercnvpy, Numbat, aneuploidy, chromosomal aberration, tumor clone
tags:
- spatial
- cnv
- copy-number
- infercnvpy
- numbat
- tumor
---

# Spatial Copy-Number Variation Inference (`spatial-cnv`)

## 1. Method Decision Matrix & When to Use

Use this playbook to infer chromosomal copy-number variations (CNVs), aneuploidy scores, and malignant clonal architecture across tissue space:

| Method (`method=`) | Upstream Library | Input Requirements | Normal Reference | Best Used When |
|---|---|---|---|---|
| **Sliding-Window Expression CNV** (`infercnvpy`) | `infercnvpy` (`cnv.tl.infercnv`) | Log-normalized `adata.X` + `adata.var[["chromosome", "start", "end"]]` | Non-malignant cell types (`reference_key` / `reference_cat`) or global mean | Fast spatial tumor-vs-normal aneuploidy mapping and CNV subclone clustering in Python |
| **Haplotype-Aware Allelic CNV** (`numbat`) | R `numbat` (`run_numbat`) | Raw count matrix + phased SNP pileup allele dataframe (`df_allele`) + diploid reference profiles | Required diploid expression reference (`lambdas_ref`) | Resolving copy-neutral LOH, subclonal phylogeny, and low-purity tumor spots with B-allele frequency |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Sliding-Window Expression CNV & Subclone Clustering (`infercnvpy`)

```python
import infercnvpy as cnv
import scanpy as sc

# Verify genomic coordinates exist in adata.var[["chromosome", "start", "end"]]
# If missing, annotate from a GTF file: cnv.io.genomic_position_from_gtf(gtf_path, adata=adata)
required_cols = {"chromosome", "start", "end"}
assert required_cols.issubset(adata.var.columns), "adata.var must contain chromosome, start, end"

# Run inferCNV using non-malignant microenvironment cells as diploid baseline
cnv.tl.infercnv(
    adata,
    reference_key="cell_type",
    reference_cat=["Endothelial", "Fibroblast", "T_cell", "B_cell"],
    window_size=100,
    step=10,
    exclude_chromosomes=("chrX", "chrY", "chrM", "X", "Y", "MT"),
)

# Cluster spots by CNV profile and compute per-spot CNV aberration score
cnv.tl.pca(adata)
cnv.pp.neighbors(adata)
cnv.tl.leiden(adata, key_added="cnv_leiden")
cnv.tl.cnv_score(adata)
```

### Pattern B: Stratifying Malignant vs. Diploid Microenvironment Domains

```python
import numpy as np
import pandas as pd

# Summarize CNV aberration score across CNV clusters and spatial coordinates
summary_df = (
    adata.obs.groupby("cnv_leiden", observed=True)["cnv_score"]
    .agg(n_spots="count", mean_cnv="mean", median_cnv="median")
    .reset_index()
)
# Classify high-aneuploidy subclones vs diploid stroma
thresh = adata.obs["cnv_score"].quantile(0.75)
adata.obs["malignancy_status"] = np.where(adata.obs["cnv_score"] >= thresh, "Malignant_CNV_High", "Diploid_Baseline")
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-cnv")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `run_infercnv(adata, reference_key=None, reference_cat=None, window_size=100, step=10)`
- `cnv_summary(adata, groupby="cnv_leiden", score_key="cnv_score")`

## 3. Output Format & Verification Contract

- **CNV-Annotated Spatial AnnData** (`intermediate/cnv_processed.h5ad`):
  - `adata.obsm["X_cnv"]`: smoothed chromosomal window copy-number matrix `(n_obs, n_windows)`.
  - `adata.uns["cnv"]`: chromosome boundary positions (`chr_pos`) and window metadata.
  - `adata.obs["cnv_leiden"]` and `adata.obs["cnv_score"]`: CNV cluster assignment and continuous aberration amplitude.
- **CNV Summary Table** (`tables/cnv_summary.csv`):
  - Columns: `group` (`cnv_leiden` or `cell_type`), `n_spots`, `mean_cnv_score`, `median_cnv_score`.
- **Verification**:
  - Confirm that designated normal reference categories (`reference_cat`) exhibit `mean_cnv_score` near the lower baseline compared with tumor core domains.

## 4. Gotchas & Troubleshooting

1. **Missing Genomic Coordinates (`adata.var[["chromosome", "start", "end"]]`)**:
   - `infercnvpy.tl.infercnv` immediately raises a `ValueError` or skips all genes if `chromosome`, `start`, and `end` are missing or all `NaN`. Populate them via `cnv.io.genomic_position_from_gtf()` or Ensembl Biomart lookup before running.
2. **Chromosome Naming Conventions & Sex/Mitochondrial Chromosomes**:
   - Datasets mix `"chr1"` and `"1"` prefixes. Always exclude `"chrX"`, `"chrY"`, `"chrM"` (and `"X"`, `"Y"`, `"MT"`) so sex-linked dosage or high mitochondrial content in necrotic tumor regions is not miscalled as focal amplification.
3. **Impure or Missing Normal Reference (`reference_cat`)**:
   - When `reference_key=None`, `infercnvpy` subtracts the global average across all spots. If $>80\%$ of the slide is homogeneous tumor, subtracting the global mean cancels out shared clonal gains/losses. Always specify stromal/immune `reference_cat` whenever normal cells are present.
4. **Targeted Spatial Panels Have Too Few Genes per Chromosome**:
   - Sliding-window expression CNV (`window_size=100` expressed genes) requires transcriptome-scale coverage (Visium, Stereo-seq, Slide-seq, $>3,000$ detected genes). Do not run `infercnvpy` on 300-gene targeted Xenium panels.

## Dependencies

`anndata`, `infercnvpy`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`, `Matrix`, `numbat`
