---
name: sc-count
description: Load when turning scRNA FASTQ (or existing CellRanger/STARsolo/SimpleAF/kb-python output)
  into a downstream-ready AnnData. Skip when reads are already counted into AnnData (use sc-standardize-input);
  raw quality assessment only (use sc-fastq-qc).
trigger: Cell Ranger count, STARsolo count, fastq to adata, raw single-cell counting, generate count matrix
tags:
- singlecell
- scrna
- counting
- cellranger
- starsolo
- simpleaf
- kb-python
---

# Single-Cell RNA-seq Read Counting & Matrix Import (`sc-count`)

## 1. Method Decision Matrix & When to Use

Select a read-to-UMI counting backend or import path depending on library chemistry, compute budget, and whether splicing (velocyto) matrices are needed:

| Backend (`--method` / `method=`) | Upstream Tool | Alignment Mode | Typical Runtime & RAM | Best Used When |
|---|---|---|---|---|
| **Cell Ranger** (`method="cellranger"`) | `cellranger count` | Splice-aware STAR alignment (`10x` official pipeline) | ~2–4 hr, 32–64 GB RAM | Official 10x Chromium clinical/production pipelines requiring web summary HTML & BAM |
| **STARsolo** (`method="starsolo"`) | `STAR --soloType CB_UMI_Simple` | Full splice-aware genome alignment | ~15–30 min, 32 GB RAM | Fast drop-in replacement for Cell Ranger; natively emits `Gene`, `GeneFull`, and `Velocyto` (spliced/unspliced) matrices |
| **SimpleAF / Alevin-fry** (`method="simpleaf"`) | `simpleaf quant` (`piscem` + `alevin-fry`) | Lightweight mapping (`splici` index) | ~5–10 min, 8–16 GB RAM | Rapid USA-mode (Unspliced/Spliced/Ambiguous) quantification on workstation hardware |
| **kb-python** (`method="kb_python"`) | `kb count` (`kallisto` + `bustools`) | Pseudoalignment (`--workflow standard` or `lamanno`) | ~5–10 min, 4–16 GB RAM | High-throughput multi-sample FASTQ processing or low-memory environments |
| **Pre-counted Import** | `scanpy.read_10x_h5` / `read_10x_mtx` | Direct matrix ingestion | < 10 sec | `filtered_feature_bc_matrix` already exists on disk and needs canonical `AnnData` packaging |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Fast CLI Quantification (`STARsolo` or `kb-python`)

```bash
# Option 1: STARsolo (10x v3 chemistry: 16bp barcode + 12bp UMI on Read 1, cDNA on Read 2)
STAR --genomeDir /path/to/star_index \
     --readFilesIn sample_R2.fastq.gz sample_R1.fastq.gz \
     --readFilesCommand zcat \
     --soloType CB_UMI_Simple \
     --soloCBwhitelist /path/to/3M-february-2018.txt \
     --soloCBstart 1 --soloCBlen 16 --soloUMIstart 17 --soloUMIlen 12 \
     --soloFeatures Gene GeneFull \
     --runThreadN 8 \
     --outFileNamePrefix starsolo_out/

# Option 2: kb-python (kallisto | bustools) to h5ad
kb count -i index.idx -g t2g.txt -x 10xv3 -o kb_out/ --h5ad -t 8 sample_R1.fastq.gz sample_R2.fastq.gz
```

### Pattern B: Ingesting & Canonicalizing Count Outputs in Python (`scanpy`)

```python
import numpy as np
import scanpy as sc

# 1. Read 10x H5 or MTX directory ( filtered_feature_bc_matrix )
adata = sc.read_10x_h5("filtered_feature_bc_matrix.h5", gex_only=True)
adata.var_names_make_unique()
adata.obs_names_make_unique()

# 2. Enforce canonical raw count contract before any downstream normalization
adata.layers["counts"] = adata.X.copy()
adata.raw = adata.copy()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-count")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `load_count_matrix(path, gex_only=True)`
- `canonicalize_counts(adata, sample_id=None)`
- `count_summary(adata)`

## 3. Output Format & Verification Contract

- **Canonical Count AnnData** (`intermediate/processed.h5ad`):
  - `adata.X` and `adata.layers["counts"]`: sparse CSR matrix (`scipy.sparse.csr_matrix`) containing non-negative integer UMI counts.
  - `adata.var_names` and `adata.obs_names`: unique gene symbols and cell barcodes (`adata.var_names.is_unique == True`).
- **Count Summary Tables** (`tables/count_summary.csv`, `tables/barcode_metrics.csv`):
  - Columns: `n_cells`, `n_genes`, `median_umis`, `median_genes` and per-barcode `total_counts`, `n_genes_by_counts`.
- **Verification Invariants**:
  - Verify `adata.n_obs > 0`, `adata.n_vars > 0`, `"counts" in adata.layers`, and all non-zero entries in `adata.layers["counts"].data` are non-negative integers.

## 4. Gotchas & Troubleshooting

1. **STARsolo Read Order (`--readFilesIn cDNA_R2 Barcode_R1`)**:
   - Unlike `cellranger` and `kb count`, `STAR --soloType CB_UMI_Simple` expects the **biological cDNA read (`R2`) first** and the **barcode+UMI read (`R1`) second** in `--readFilesIn`. Swapping them results in $0\%$ mapped reads or invalid barcode matching.
2. **Chemistry-Specific Barcode & UMI Geometry (`10xv2` vs. `10xv3`)**:
   - `10xv2` uses a 16 bp cell barcode + **10 bp UMI** (`737K-august-2016.txt` whitelist), whereas `10xv3`/`10xv3.1` uses a 16 bp cell barcode + **12 bp UMI** (`3M-february-2018.txt` whitelist). Passing `10xv2` UMI length on `10xv3` libraries truncates UMIs and inflates PCR duplicate collisions.
3. **Duplicate Gene Symbols in 10x `features.tsv.gz`**:
   - Multiple Ensembl gene IDs often map to the same HGNC/MGI gene symbol (e.g., `TBCE`, `LINC01238`). Always call `adata.var_names_make_unique()` immediately after `sc.read_10x_mtx` / `sc.read_10x_h5` to prevent downstream indexing errors.
4. **Intronic Reads in Single-Nucleus RNA-seq (`snRNA-seq`)**:
   - Nuclear transcripts are enriched for unspliced pre-mRNA. Always enable `--include-introns true` in Cell Ranger (default since v7) or use `GeneFull` in `STARsolo` when counting `snRNA-seq` libraries.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
