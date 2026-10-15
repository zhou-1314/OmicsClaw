---
name: sc-perturb-prep
description: Load when attaching cell-barcode → sgRNA assignments onto a Perturb-seq AnnData, producing
  standardised perturbation / sgRNA / target-gene columns. Skip when the AnnData already has
  perturbation labels (use sc-perturb); guide-calling from FASTQ (run demuxlet / cellranger
  upstream).
tags:
- singlecell
- scrna
- perturb-prep
- perturb-seq
- crispr
- sgrna-assignment
---

# Perturb-seq Barcode-to-sgRNA Assignment & AnnData Preparation (`sc-perturb-prep`)

## 1. Method Decision Matrix & When to Use

Use this skill to standardize cell-barcode $\to$ sgRNA mapping tables (from 10x Cell Ranger `protospacer_calls_per_cell.csv`, FBA, or custom guide callers), resolve multi-guide doublets and non-targeting (`NT`) controls, strip non-expression CRISPR guide capture features from `.var`, and produce a clean AnnData ready for `sc-perturb`.

| Input Guide Format | Multi-Guide Policy | Recommended Workflow | Downstream Skill |
|---|---|---|---|
| **10x Cell Ranger `protospacer_calls_per_cell.csv`** (`cell_barcode`, `feature_call`, `num_features`) | Drop multi-guide cells (`num_features > 1` or `|`-delimited calls) | Parse single-guide rows, infer target gene from `<GENE>_<sgID>`, standardize `NT` controls | `sc-perturb` (`Mixscape` / `edistance`) |
| **Two/Three-column TSV/CSV** (`barcode`, `sgRNA`, optional `target_gene`) | `drop_multi_guide=True` (default) or retain as `"multi_guide"` | `standardize_mapping` $\to$ `collapse_assignments` $\to$ `attach_assignments` | `sc-perturb` / `sc-de` |
| **Multimodal 10x H5 with `CRISPR Guide Capture` in `var["feature_types"]`** | Threshold guide UMI counts or attach external calls | Subset `adata[:, adata.var["feature_types"] == "Gene Expression"]` before normalization | `sc-preprocessing` $\to$ `sc-perturb` |

> **Decision Rule**: Always separate `CRISPR Guide Capture` features from `Gene Expression` features before library-size normalization (`sc.pp.normalize_total`), otherwise high-copy sgRNA UMIs distort cellular mRNA size factors.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Standardizing & Attaching Barcode-to-Guide Tables (`pandas` + `anndata`)

```python
import re
import pandas as pd

# 1. Load barcode -> sgRNA table and drop empty / duplicate rows
raw_map = pd.read_csv("protospacer_calls.tsv", sep="\t")
mapping = raw_map.rename(columns={"cell_barcode": "barcode", "feature_call": "sgRNA"})[["barcode", "sgRNA"]].dropna()
mapping = mapping.drop_duplicates(subset=["barcode", "sgRNA"])

# 2. Collapse per barcode, drop multi-guide cells, and infer target gene vs. whole-token NT controls
ctrl_tokens = {"NT", "NTC", "NON-TARGET", "NON_TARGET", "NEGATIVE_CONTROL", "NEG_CTRL"}
grouped = mapping.groupby("barcode")["sgRNA"].apply(list)
single = grouped[grouped.map(len) == 1].map(lambda x: x[0])

def parse_target(sgrna: str) -> tuple[str, str]:
    tokens = [t.upper() for t in re.split(r"[^A-Za-z0-9]+", sgrna) if t]
    if any(t in ctrl_tokens for t in tokens):
        return "NT", "control"
    return sgrna.split("_")[0], "assigned"

parsed = single.map(parse_target)
assign_df = pd.DataFrame({
    "sgRNA": single,
    "target_gene": parsed.map(lambda x: x[0]),
    "perturbation": parsed.map(lambda x: x[0]),
    "assignment_status": parsed.map(lambda x: x[1]),
}, index=single.index)

# 3. Subset expression features and intersect assigned barcodes
if "feature_types" in adata.var.columns:
    adata = adata[:, adata.var["feature_types"] == "Gene Expression"].copy()
common = adata.obs_names.intersection(assign_df.index)
adata = adata[common].copy()
for col in ["sgRNA", "target_gene", "perturbation", "assignment_status"]:
    adata.obs[col] = assign_df.loc[adata.obs_names, col].to_numpy()
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-perturb-prep")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `standardize_mapping(table, barcode_column=None, sgrna_column=None, target_column=None)`
- `collapse_assignments(mapping, delimiter="_", gene_position=0, control_label="NT", drop_multi_guide=True)`
- `attach_assignments(adata, assignments, pert_key="perturbation", sgrna_key="sgRNA", target_key="target_gene")`

## 3. Output Format & Verification Contract

- **Prepared Perturb-seq AnnData** (`intermediate/perturb_prep.h5ad` or `processed.h5ad`):
  - Contains only `Gene Expression` features in `adata.var`.
  - Standardized `adata.obs` columns: `perturbation`, `sgRNA`, `target_gene`, `assignment_status` (`"assigned"`, `"control"`, or `"multi_guide"`).
- **Tabular Outputs**:
  - `tables/perturbation_assignments.csv`: per-barcode `barcode`, `sgRNA`, `target_gene`, `perturbation`, `assignment_status`.
  - `tables/assignment_status_counts.csv`: cell counts per `assignment_status`.
  - `tables/perturbation_counts.csv`: cell counts per `perturbation` sorted descending.
  - `tables/dropped_multi_guide_cells.csv`: barcodes excluded when `drop_multi_guide=True`.
- **Verification Invariants**:
  - Assert `len(adata) > 0` and `"NT" in set(adata.obs["perturbation"])` (or verify control label presence).
  - Assert no `CRISPR Guide Capture` or `Antibody Capture` rows remain in `adata.var`.

## 4. Gotchas & Troubleshooting

1. **Substring Trap When Identifying Non-Targeting (`NT`) Guides**:
   - Never match `"NT"` as a naive substring (`"NT" in sgrna.upper()`), which falsely classifies human genes such as `WNT3`, `NTRK1`, `NT5E`, and `ARNT` as non-targeting controls! Always split guide names on punctuation (`_`, `-`, `.`) and match whole tokens (`{"NT", "NTC", "NON_TARGET", ...}`).
2. **Barcode Suffix Mismatch (`-1` vs. Raw 16bp Barcode)**:
   - 10x Cell Ranger `obs_names` usually end with `-1` (`AAACCCAAGAAACCAT-1`), whereas some custom guide callers strip the gem-group suffix. Inspect `adata.obs_names[:5]` and `mapping["barcode"][:5]` before intersecting; zero overlapping barcodes raises a `ValueError`.
3. **Multi-Guide Cells Confound Single-Gene Attribution**:
   - Cells receiving $\ge 2$ distinct sgRNAs exhibit combinatorial or dominant-negative phenotypes. Keep `drop_multi_guide=True` for standard single-gene Perturb-seq screens and inspect `tables/dropped_multi_guide_cells.csv` to monitor MOI.
4. **Retaining Guide Features in `adata.var` Corrupts Normalization**:
   - If input comes from a 10x multi-feature matrix without `var["feature_types"]`, verify that sgRNA IDs are not mixed into `adata.var_names`.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`
