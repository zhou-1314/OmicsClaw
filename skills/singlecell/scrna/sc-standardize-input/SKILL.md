---
name: sc-standardize-input
description: Load when an external single-cell h5ad/h5/loom/mtx needs to be canonicalised onto the OmicsClaw
  AnnData contract before downstream scRNA skills run. Skip when data already came from sc-count (already
  canonical); bulk RNA-seq (use bulkrna-qc); spatial (use spatial-preprocess).
trigger: standardize AnnData, fix scRNA input, canonicalize single-cell input, prepare AnnData, input contract
tags:
- singlecell
- scrna
- input
- standardization
- anndata
---

# Single-Cell AnnData Input Canonicalization (`sc-standardize-input`)

## 1. Method Decision Matrix & When to Use

Use this playbook to inspect, repair, and canonicalize external single-cell files (`.h5ad`, `10x` `.h5` / `.mtx`, `.loom`, `.csv`) so that downstream single-cell skills receive a guaranteed matrix and metadata contract.

| Input Source / Issue | Inspection Check | Canonicalization Action | Upstream Function |
|---|---|---|---|
| **Raw counts buried in `.raw` or `.layers`** | `adata.X` has non-integers (`log1p` or scaled z-scores), while integer counts live in `adata.layers["counts"]` or `adata.raw.X` | Copy true integer counts to `adata.layers["counts"]` and `adata.X` (or preserve log1p in `adata.X` with verified `layers["counts"]`) | `scipy.sparse` / `anndata` |
| **Ensembl IDs in `adata.var_names`** | `adata.var_names[0].startswith(("ENSG", "ENSMUSG"))` while gene symbols sit in `adata.var["gene_symbols"]` or `feature_name` | Promote gene symbol column to `adata.var_names` and call `adata.var_names_make_unique()` | `adata.var_names_make_unique()` |
| **Duplicate Barcodes or Gene Symbols** | `not adata.obs_names.is_unique` or `not adata.var_names.is_unique` | Deduplicate with `-1`, `-2` suffixes via `obs_names_make_unique()` / `var_names_make_unique()` | `AnnData.var_names_make_unique` |
| **Dense Float64 Matrix Bloat** | `isinstance(adata.X, np.ndarray)` on a sparse count matrix | Convert to `scipy.sparse.csr_matrix` to cut memory by $10\text{--}20\times$ | `scipy.sparse.csr_matrix` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Auditing & Canonicalizing an External `.h5ad` (`anndata` + `scipy.sparse`)

```python
import numpy as np
import scipy.sparse as sp
import scanpy as sc

def matrix_is_integer_counts(mat) -> bool:
    data = mat.data if sp.issparse(mat) else np.asarray(mat).ravel()
    if len(data) == 0:
        return False
    sample = data[:min(len(data), 20000)]
    return bool(np.all(sample >= 0) and np.allclose(sample, np.round(sample)))

# 1. Locate raw integer counts across layers["counts"], adata.raw, or adata.X
if "counts" in adata.layers and matrix_is_integer_counts(adata.layers["counts"]):
    counts_source = "layers['counts']"
elif adata.raw is not None and adata.raw.shape == adata.shape and matrix_is_integer_counts(adata.raw.X):
    adata.layers["counts"] = adata.raw.X.copy()
    counts_source = "raw"
elif matrix_is_integer_counts(adata.X):
    adata.layers["counts"] = adata.X.copy()
    counts_source = "X"
else:
    raise ValueError("No non-negative integer count matrix found in layers['counts'], raw, or X!")

# 2. Harmonize Ensembl IDs -> gene symbols if a symbol column exists in adata.var
for col in ("gene_symbols", "gene_symbol", "feature_name", "gene_short_name"):
    if adata.var_names[0].startswith(("ENSG", "ENSMUSG")) and col in adata.var.columns:
        adata.var["ensembl_id"] = adata.var_names.astype(str)
        adata.var_names = adata.var[col].astype(str).values
        break

# 3. Enforce unique identifiers and sparse CSR storage
adata.var_names_make_unique()
adata.obs_names_make_unique()
if not sp.issparse(adata.layers["counts"]):
    adata.layers["counts"] = sp.csr_matrix(adata.layers["counts"])
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-standardize-input")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `standardize(adata, species="auto", make_var_unique=True)`
- `infer_species(adata)`
- `contract_summary(adata)`

## 3. Output Format & Verification Contract

- **Canonicalized AnnData** (`intermediate/processed.h5ad`):
  - `adata.layers["counts"]`: guaranteed non-negative count matrix aligned with `(adata.n_obs, adata.n_vars)`.
  - `adata.var_names` and `adata.obs_names`: unique string identifiers (`adata.var_names.is_unique` and `adata.obs_names.is_unique`).
  - `adata.uns["omicsclaw_matrix_contract"]`: provenance dictionary recording `counts_source` and inferred `species` (`"human"` or `"mouse"`).
- **Verification Invariants**:
  - Confirm `contract_summary(adata)["has_counts_layer"] == True` and `adata.var_names.is_unique`.

## 4. Gotchas & Troubleshooting

1. **CELLxGENE & Public Atlas `.h5ad` Files Store Raw Counts in `adata.raw.X`**:
   - Public `.h5ad` files downloaded from CZ CELLxGENE Discover or the Human Cell Atlas typically store log-normalized expression in `adata.X`,Ensembl IDs in `adata.var_names`, gene symbols in `adata.var["feature_name"]`, and raw integer counts inside `adata.raw.X`. Always check `adata.raw.X` and `adata.var["feature_name"]` before running QC or DE.
2. **Shape Mismatch Between `adata.raw` and `adata`**:
   - If an external `.h5ad` was subsetted to $2,000$ HVGs in `adata.X` while `adata.raw` still holds $30,000$ genes, `adata.raw.X` cannot be assigned directly to `adata.layers["counts"]` due to shape mismatch (`ValueError`). Reconstruct `adata = adata.raw.to_adata()` first to recover the full transcriptome.
3. **Species Inference Fails on Ensembl IDs (`ENSG` / `ENSMUSG`)**:
   - Gene-case heuristics (`UPPER` $\rightarrow$ human, `Title` $\rightarrow$ mouse) fail if `adata.var_names` are Ensembl IDs (`ENSG00000...` and `ENSMUSG00000...` are both uppercase!). Always map Ensembl IDs to gene symbols before inferring species or matching `MT-`/`mt-` mitochondrial prefixes.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`
