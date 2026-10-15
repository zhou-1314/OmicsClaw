---
name: bulkrna-deconvolution
description: Load when estimating cell-type proportions in bulk RNA-seq samples from a single-cell or
  signature-matrix reference. Skip when the data is already single-cell (no deconvolution
  needed); spatial deconvolution (use spatial-deconv); pseudotime placement of bulk samples (use
  bulkrna-trajblend).
trigger: bulk deconvolution, cell type proportion, NNLS, CIBERSORTx, bulk deconv, cell fraction
tags:
- bulkrna
- deconvolution
- NNLS
- cell-type-proportion
---

# Bulk RNA-seq Cell-Type Deconvolution (`bulkrna-deconvolution`)

## 1. Method Decision Matrix & When to Use

Select the deconvolution estimator according to the reference input (pre-built signature matrix vs. multi-subject scRNA-seq atlas) and noise structure:

| Reference Input & Biological Setting | Method | Upstream Package | Why |
|---|---|---|---|
| **Curated cell-type signature matrix** ($G \times K$ marker expression, e.g., LM22) | Non-Negative Least Squares (NNLS) | `scipy.optimize.nnls` | Fast, deterministic non-negative regression $\min_{w \ge 0} \|y - Sw\|_2^2$ normalized so $\sum_k w_k = 1$ |
| **Collinear immune/stromal sub-lineages** or noisy tumor mixtures | $\nu$-Support Vector Regression ($\nu$-SVR / CIBERSORT) | `sklearn.svm.NuSVR` / `CIBERSORTx` | $L_1$-insensitive loss ($\epsilon$-tube) with $L_2$ regularization selects sparse support vectors and resists outlier marker genes |
| **Multi-subject single-cell RNA-seq reference** (`AnnData` with donor annotations) | Multi-subject weighted deconvolution (MuSiC / DWLS) | R `MuSiC` or dampened weighted least squares | Down-weights genes with high cross-subject variance so conserved cell-type markers dominate |
| **Deep generative / autoencoder reference mapping** | Deep-learning bulk deconvolution (`Tape` / `Scaden`) | `scaden` / PyTorch | Learns non-linear gene-to-fraction mappings across simulated pseudobulk mixtures |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `scipy.optimize.nnls` Deconvolution Against a Signature Matrix

```python
import numpy as np
import pandas as pd
from scipy.optimize import nnls

# 1. Load linear-scale bulk TPM/CPM (genes x samples) and reference signature (genes x cell_types)
bulk_tpm = pd.read_csv("data/bulk_tpm.csv", index_col=0)
sig_mat = pd.read_csv("data/signature_matrix.csv", index_col=0)

# 2. Align on shared marker genes in linear (non-log) expression space
shared_genes = bulk_tpm.index.intersection(sig_mat.index)
if len(shared_genes) < 20:
    raise ValueError(f"Insufficient gene overlap ({len(shared_genes)} genes) between bulk and signature.")

Y = bulk_tpm.loc[shared_genes].to_numpy(dtype=float)
S = sig_mat.loc[shared_genes].to_numpy(dtype=float)

# 3. Solve NNLS per sample and normalize weights to sum to 1.0
weights = np.zeros((Y.shape[1], S.shape[1]), dtype=float)
residuals = np.zeros(Y.shape[1], dtype=float)
for j in range(Y.shape[1]):
    w, rnorm = nnls(S, Y[:, j])
    total = w.sum()
    weights[j] = w / total if total > 0 else np.full(S.shape[1], 1.0 / S.shape[1])
    residuals[j] = rnorm

proportions = pd.DataFrame(weights, index=bulk_tpm.columns, columns=sig_mat.columns)
proportions.to_csv("tables/proportions.csv")
```

### Pattern B: Lightweight Micro-Helper via `use_skill("bulkrna-deconvolution")`

```python
# Calling use_skill("bulkrna-deconvolution") auto-injects kernel.py helpers into the python kernel:
proportions_df = nnls_deconvolve(bulk_tpm, signature=sig_mat)
dominant_df = summarize_dominant_types(proportions_df)
```

## 3. Output Format & Verification Contract

- **Cell-Type Proportions Table** (`tables/proportions.csv`):
  - Indexed by `sample` with one column per reference cell type; every entry satisfies $0 \le p_{ik} \le 1$ and row sums satisfy $\sum_k p_{ik} = 1.0 \pm 10^{-6}$.
- **Dominant Cell-Type Summary** (`tables/dominant_types.csv`):
  - Columns: `sample`, `dominant_cell_type`, `max_proportion`, `nnls_residual`, and `pearson_r` (correlation between observed $y_i$ and reconstructed $\hat{y}_i = S w_i$).
- **Verification Invariants**:
  - `proportions.shape == (bulk.shape[1], signature.shape[1])`.
  - No `NaN` or negative proportions exist in `tables/proportions.csv`.

## 4. Gotchas & Troubleshooting

1. **Always Deconvolve in Linear Space (TPM / CPM), Never Log-Transformed Space**:
   - Bulk mixture modeling assumes linear additivity: $y_{\text{bulk}} = \sum_k w_k s_k$. Applying NNLS or SVR to $\log_2(\text{TPM}+1)$ violates linear additivity and severely distorts estimated cell fractions. If either input is log-transformed, exponentiate ($2^x - 1$) back to linear scale first.
2. **Gene Identifier Namespace Mismatch**:
   - If the bulk matrix uses Ensembl IDs (`ENSG00000...`) while the signature matrix uses HGNC gene symbols (`CD3D`, `MS4A1`), `bulk.index.intersection(signature.index)` will be empty. Run `bulkrna-geneid-mapping` prior to deconvolution.
3. **Missing Malignant / Unmodeled Cell Populations**:
   - Standard NNLS forces observed bulk expression onto the columns of $S$ and normalizes $\sum_k w_k = 1$. If a tumor sample contains 70% malignant epithelial cells that are absent from an immune-only reference, NNLS still sums immune fractions to 100% (interpreted as relative immune composition). Inspect `nnls_residual` and reconstructed Pearson $r$ to detect unmodeled cell types.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `scipy`
