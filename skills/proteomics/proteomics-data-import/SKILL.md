---
name: proteomics-data-import
description: Load when ingesting a MaxQuant `proteinGroups.txt`, FragPipe `combined_protein.tsv`,
  DIA-NN report, or generic CSV / TSV protein table — normalises columns to a standard schema.
  Skip when raw spectra are the input (run the search engine first); the file is already in
  OmicsClaw schema.
trigger: data import, convert proteomics, format conversion
tags:
- proteomics
- import
- maxquant
- fragpipe
- diann
- spectronaut
---

# Proteomics Search Engine Table Import & Filtering (`proteomics-data-import`)

## 1. Method Decision Matrix & When to Use

Select the import parser and artifact-filtering rules according to the upstream proteomics search engine:

| Search Engine & Acquisition | Primary Protein Table | Artifact Rows to Filter Out | Intensity Columns Extracted |
|---|---|---|---|
| **MaxQuant** (DDA LFQ / TMT) | `proteinGroups.txt` (TSV) | `Reverse == "+"`, `Potential contaminant == "+"`, `Only identified by site == "+"` | `LFQ intensity <sample>` (preferred) or `Intensity <sample>` |
| **FragPipe / Philosopher** (DDA / DIA) | `combined_protein.tsv` | Accessions starting with `rev_` / `contam_` (`cRAP`) | `<sample> MaxLFQ Intensity` or `<sample> Intensity` |
| **DIA-NN** (DIA library-free / spectral lib) | `report.pg_matrix.tsv` | `Contaminant` accessions (`cRAP-*`) | Sample run file columns (already MaxLFQ normalized at $1\%$ PG $q$-value) |
| **Spectronaut / Generic CSV** | Protein matrix CSV/TSV | Decoy (`DECOY_`) & keratin/trypsin contaminant rows | Numeric sample intensity columns (`PG.Quantity` / `Intensity_*`) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic MaxQuant `proteinGroups.txt` Import & Decoy/Contaminant Removal

```python
import numpy as np
import pandas as pd

# 1. Read MaxQuant tab-delimited proteinGroups.txt
df = pd.read_csv("proteinGroups.txt", sep="\t", low_memory=False)

# 2. Remove target-decoy reverse hits, cRAP contaminants, and site-only identifications
for flag_col in ["Reverse", "Potential contaminant", "Only identified by site"]:
    if flag_col in df.columns:
        df = df[df[flag_col].fillna("") != "+"]

# 3. Extract leading protein ID, gene symbol, and LFQ intensity matrix (replace 0 with NaN)
df["protein_id"] = df["Majority protein IDs"].astype(str).str.split(";").str[0]
lfq_cols = [c for c in df.columns if c.startswith("LFQ intensity ")]
mat = df.set_index("protein_id")[lfq_cols].replace(0, np.nan)
mat.columns = [c.replace("LFQ intensity ", "") for c in lfq_cols]
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-data-import")`)

```python
# Calling use_skill("proteomics-data-import") auto-injects kernel.py helpers into the python kernel:
raw_df = read_table("data/proteinGroups.txt")
clean_df = standardize(raw_df, format="maxquant")
summary = import_summary(clean_df)
```

## 3. Output Format & Verification Contract

- **Standardized Protein Table** (`tables/proteins.csv`):
  - Contains `protein_id` (primary accession), optional `gene_name`, and standardized numeric sample intensity columns (`Intensity_*` / `LFQ_*`), with decoy, contaminant, and site-only rows removed.
- **Run Metadata** (`df.attrs["run_info"]`):
  - Records `format`, `n_input_rows`, `n_retained_proteins`, `n_removed_flags`, and `sample_columns`.
- **Verification Invariants**:
  - Zero rows remain with `Reverse == "+"`, `Potential contaminant == "+"`, or `Only identified by site == "+"`.
  - `n_retained_proteins > 0` and all retained intensity columns are numeric (`float64`).

## 4. Gotchas & Troubleshooting

1. **Forgetting to Filter `Potential contaminant` and `Reverse` Rows**:
   - Unfiltered MaxQuant `proteinGroups.txt` includes trypsin, human keratins, bovine serum albumin from cell culture media, and `REV__` decoy hits. Leaving them in distorts total intensity normalization and produces false top hits in differential expression.
2. **MaxQuant Encodes Missing Values as Exact `0` Instead of `NaN`**:
   - MaxQuant writes `0` when a protein group is unquantified in a sample. Taking `np.log2(df + 1)` without first converting `0 -> np.nan` creates an artificial spike at `0.0` that violates Gaussian assumptions in `limma` and Welch $t$-tests.
3. **Semicolon-Delimited `Majority protein IDs` Break Downstream Pathway Joins**:
   - Homologous isoforms sharing peptides are grouped as `P04406;P00354`. Always split on `;` and retain the leading canonical UniProt accession (`str.split(";").str[0]`) and first `Gene names` token before mapping to GO/KEGG/Reactome.

## Dependencies

`numpy`, `pandas`, `matplotlib`
