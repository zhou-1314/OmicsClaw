---
name: sc-velocity-prep
description: Load when generating spliced / unspliced layers from Cell Ranger BAM, FASTQ, STARsolo output,
  or velocyto loom — the prerequisite for sc-velocity. Skip when AnnData already has spliced+unspliced
  layers (use sc-velocity); any non-velocity preprocessing (use sc-preprocessing).
trigger: RNA velocity prep, prepare spliced unspliced layers, velocyto, starsolo velocyto, velocity-ready AnnData
tags:
- singlecell
- scrna
- velocity-prep
- velocyto
- starsolo
- spliced-unspliced
---

# Single-Cell RNA Velocity Layer Preparation via Velocyto / STARsolo (`sc-velocity-prep`)

## 1. Method Decision Matrix & When to Use

Use this skill to construct or import `adata.layers["spliced"]`, `adata.layers["unspliced"]`, and optional `adata.layers["ambiguous"]` from upstream `.loom` files, STARsolo `Velocyto/raw` matrix directories, Cell Ranger BAMs, or paired FASTQs—and optionally merge those splicing layers into an already-clustered base AnnData (`--base-h5ad`) prior to running `sc-velocity`.

| Upstream Input Artifact | Recommended Backend | Tool / Loader | Required Reference Assets |
|---|---|---|---|
| **Existing `.loom` file** (from `velocyto run10x` / `velocyto run`) | **Loom Import** | `scvelo.read_loom` / `anndata.read_loom` + `scv.utils.merge` | None (splicing matrices already quantified) |
| **Existing STARsolo `Solo.out/Velocyto/` directory** | **STARsolo Directory Loader** (`starsolo`) | Read `spliced.mtx`, `unspliced.mtx`, `ambiguous.mtx` + `barcodes.tsv` + `features.tsv` | None (loads precomputed matrices directly) |
| **Cell Ranger BAM (`possorted_genome_bam.bam`) + `barcodes.tsv`** | **Velocyto CLI** (`velocyto`) | `velocyto run -b barcodes.tsv -o out/ bam genes.gtf` | Reference gene annotation `--gtf` (`genes.gtf`) |
| **Raw paired-end FASTQ (`R1`/`R2`)** | **STARsolo Velocyto** (`starsolo`) | `STAR --soloType CB_UMI_Simple --soloFeatures Gene Velocyto` | STAR genome index (`--reference`), `--chemistry` (`10xv2`/`10xv3`), barcode `--whitelist` |

> **Decision Rule**: If `.loom` or `Solo.out/Velocyto/` matrices already exist on disk, load and merge them directly into your processed AnnData without re-running BAM/FASTQ alignment.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Loading a `.loom` or STARsolo Velocyto Matrix & Merging into Base `AnnData`

```python
import numpy as np
import pandas as pd
import scanpy as sc
from scipy import io, sparse

# Option 1: Load STARsolo Solo.out/Velocyto/raw (or filtered) directory
vel_dir = Path("Solo.out/Velocyto/filtered")
barcodes = pd.read_csv(vel_dir / "barcodes.tsv", header=None, sep="\t")[0].astype(str).to_numpy()
features = pd.read_csv(vel_dir / "features.tsv", header=None, sep="\t")
gene_names = features[1].astype(str).to_numpy() if features.shape[1] > 1 else features[0].astype(str).to_numpy()

spliced = io.mmread(vel_dir / "spliced.mtx").T.tocsr()
unspliced = io.mmread(vel_dir / "unspliced.mtx").T.tocsr()
ambiguous = io.mmread(vel_dir / "ambiguous.mtx").T.tocsr()

# Option 2: Merge spliced/unspliced layers onto an existing processed base_adata
# Clean barcode prefixes/suffixes (e.g. 'sample:AAACCCA...-1x' -> 'AAACCCA...') before intersecting
clean_bc = lambda idx: pd.Index(idx).astype(str).str.replace(r"^.*:", "", regex=True).str.replace(r"(-\d+|x)$", "", regex=True)
base_adata.obs_names = clean_bc(base_adata.obs_names)
vel_adata.obs_names = clean_bc(vel_adata.obs_names)
vel_adata.var_names_make_unique()

common_cells = base_adata.obs_names.intersection(vel_adata.obs_names)
common_genes = base_adata.var_names.intersection(vel_adata.var_names)
merged = base_adata[common_cells, common_genes].copy()
for layer in ("spliced", "unspliced", "ambiguous"):
    if layer in vel_adata.layers:
        merged.layers[layer] = vel_adata[common_cells, common_genes].layers[layer].copy()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-velocity-prep")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `merge_velocity_layers(base_adata, velocity_adata)`
- `velocity_layer_summary(adata)`
- `top_splicing_genes(adata, n_top=30)`

## 3. Output Format & Verification Contract

- **Velocity-Ready AnnData** (`processed.h5ad` / `velocity_input.h5ad`):
  - `adata.layers["spliced"]`: non-negative count matrix `(n_obs, n_vars)`.
  - `adata.layers["unspliced"]`: non-negative count matrix `(n_obs, n_vars)`.
  - Optional `adata.layers["ambiguous"]`: non-negative count matrix `(n_obs, n_vars)`.
- **Tabular Outputs**:
  - `tables/velocity_layer_summary.csv`: `layer`, `total_counts`, `fraction_of_total`, `mean_per_cell`, `nonzero_fraction`.
  - `tables/top_velocity_genes.csv`: `gene`, `spliced_counts`, `unspliced_counts`, `unspliced_fraction`.
- **Verification Invariants**:
  - Assert `"spliced" in adata.layers and "unspliced" in adata.layers`.
  - Assert `adata.layers["spliced"].sum() > 0` and `adata.layers["unspliced"].sum() > 0` (typical intronic `unspliced` fraction is $10\%\text{--}35\%$ of total UMI counts).

## 4. Gotchas & Troubleshooting

1. **Barcode Prefix/Suffix Mismatch Between `.loom` and `.h5ad`**:
   - `velocyto` writes barcodes in the format `<sample_name>:<16bp_barcode>x`, whereas 10x Cell Ranger `.h5ad` files use `<16bp_barcode>-1`. Always strip sample prefixes and `-1`/`x` suffixes (`scv.utils.clean_obs_names` or `merge_velocity_layers`) before intersecting `obs_names`.
2. **BAM-Backed `velocyto` Requires Matching GTF (`--gtf`)**:
   - Running `velocyto run` on a Cell Ranger BAM requires the exact genome assembly GTF (`genes.gtf`) used during alignment; mismatched chromosome names (`chr1` vs. `1`) produce zero unspliced counts.
3. **FASTQ-Backed `STARsolo` Requires Explicit `--chemistry` & `--whitelist`**:
   - `STARsolo` cannot infer 10x chemistry (`10xv2` vs. `10xv3`) from FASTQ reads alone. Always pass `--reference`, `--chemistry`, and the matching 10x barcode `--whitelist`.
4. **Preserving `.X` Normalization When Merging Into `--base-h5ad`**:
   - When merging `spliced`/`unspliced` layers from raw velocity output into an already log-normalized and clustered `base_adata`, attach the splicing matrices into `merged.layers["spliced"]` and `merged.layers["unspliced"]` without overwriting the normalized `merged.X` or `merged.obsm["X_umap"]`.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
