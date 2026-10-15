---
name: sc-gene-programs
description: Load when extracting gene programs (NMF / cNMF factorisation) and per-cell program usage
  scores from a non-negative scRNA AnnData. Skip when ranking marker genes per cluster (use sc-markers);
  inferring TF → target regulons (use sc-grn).
tags:
- singlecell
- scrna
- gene-programs
- nmf
- cnmf
- factorisation
---

# De-Novo Gene Program Discovery via NMF / Consensus NMF (`sc-gene-programs`)

## 1. Method Decision Matrix & When to Use

Use this skill to decompose a non-negative single-cell expression matrix ($\text{cells} \times \text{genes}$) into $K$ continuous, additive gene expression programs (GEPs) and per-cell usage weights—capturing shared activity programs (e.g., hypoxia, IFN response, cell cycle) that cut across discrete cell-type clusters.

| Analytical Scenario | Recommended Method | Upstream Library | Matrix & Parameter Assumptions |
|---|---|---|---|
| **Publication-grade stable program discovery** | **Consensus NMF** (`cnmf`) | `cnmf.cNMF` | Non-negative `layers["counts"]` (preferred) or non-negative `adata.X`; runs replicates and clusters factors |
| **Fast exploratory factor decomposition** | **Single-Run NMF** (`nmf`) | `sklearn.decomposition.NMF` | Non-negative log-normalized `adata.X` (preferably subsetted to HVGs); deterministic with `random_state` |
| **Supervised scoring of known pathways** | **AUCell / Scanpy Score Genes** | `sc-pathway-scoring` (`sc.tl.score_genes`) | Curated GMT/pathway gene sets (no matrix factorization) |

> **Decision Rule**: NMF requires a **strictly non-negative** input matrix ($X \ge 0$). Never run NMF on `sc.pp.scale`-transformed data without switching to `layer="counts"` or unscaled log-normalized expression.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Fast Exploratory Gene Program Factorization (`scikit-learn` NMF)

```python
import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.decomposition import NMF

# 1. Extract non-negative expression matrix (log-normalized or raw counts)
mat = adata.X.tocsr() if sparse.issparse(adata.X) else np.asarray(adata.X, dtype=float)
if (mat.data.min() if sparse.issparse(mat) else mat.min()) < 0:
    raise ValueError("NMF requires a non-negative matrix; do not pass z-scored adata.X.")

# 2. Fit K=6 non-negative matrix factorization programs (X ≈ W @ H)
k = 6
model = NMF(n_components=k, init="nndsvda", max_iter=400, random_state=0)
usage = model.fit_transform(mat)      # W: (n_obs, K) per-cell program usage
weights = model.components_           # H: (K, n_vars) program-by-gene loadings

prog_names = [f"Program_{i+1}" for i in range(k)]
adata.obsm["X_gene_programs"] = usage
weights_df = pd.DataFrame(weights, index=prog_names, columns=adata.var_names)

# 3. Extract top-N loading genes per program
top_rows = []
for prog in prog_names:
    s = weights_df.loc[prog].nlargest(20)
    for rank, (gene, w) in enumerate(s.items(), 1):
        top_rows.append({"program": prog, "rank": rank, "gene": gene, "weight": float(w)})
top_genes_df = pd.DataFrame(top_rows)
```

### Pattern B: Consensus NMF (`cnmf`)

```python
from cnmf import cNMF

cnmf_obj = cNMF(output_dir="./cnmf_out", name="scrna_gep")
cnmf_obj.prepare(counts_fn="input_counts.h5ad", components=[6, 8, 10], n_iter=20, seed=0, numgenes=2000)
cnmf_obj.factorize(worker_i=0, total_workers=1)
cnmf_obj.combine()
cnmf_obj.consensus(k=6, density_threshold=0.1)
usage_df, spectra_scores, spectra_tpm, top_genes = cnmf_obj.load_results(K=6, density_threshold=0.1)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-gene-programs")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `find_programs(adata, method="nmf", n_programs=6, n_iter=400, layer=None, random_state=0)`
- `program_weights(adata)`
- `top_program_genes(adata, n=30)`

## 3. Output Format & Verification Contract

- **Annotated AnnData** (`intermediate/programs.h5ad` or `processed.h5ad`):
  - `adata.obsm["X_gene_programs"]`: non-negative array of shape `(n_obs, n_programs)`.
  - `adata.uns["gene_programs_weights"]`: program $\times$ gene weight matrix.
- **Tabular Outputs**:
  - `tables/program_usage.csv`: cell $\times$ program usage matrix (`Program_1 .. Program_K`).
  - `tables/program_weights.csv`: program $\times$ gene loading matrix.
  - `tables/top_program_genes.csv`: tidy table with columns `program`, `rank`, `gene`, `weight`.
- **Verification Invariants**:
  - Assert `adata.obsm["X_gene_programs"].shape == (adata.n_obs, n_programs)` and `(adata.obsm["X_gene_programs"] >= 0).all()`.
  - Verify no collapsed/all-zero programs (`adata.obsm["X_gene_programs"].sum(axis=0) > 0`).

## 4. Gotchas & Troubleshooting

1. **Non-Negativity Constraint Violation**:
   - NMF factorizes $X \approx WH$ subject to $W \ge 0, H \ge 0$. Passing centered/scaled matrices (`sc.pp.scale`) or batch-corrected embeddings with negative entries either raises a `ValueError` or distorts loadings when negative values are clipped to zero.
2. **cNMF vs. Standard NMF Matrix Selection**:
   - `cNMF` internally variance-scales genes from raw integer counts (`layers["counts"]`), whereas standard `sklearn.decomposition.NMF` should be run on log-normalized expression (`adata.X`) so high-abundance housekeeping/ribosomal genes do not dominate every factor.
3. **`n_iter` Meaning in `cNMF` vs. `sklearn` NMF**:
   - In `sklearn.decomposition.NMF`, `max_iter` (`n_iter=400`) is the coordinate-descent iteration budget per run; in `cnmf.prepare`, `n_iter` controls the number of consensus factorization replicates.
4. **Collapsed / Degenerate Factors When $K$ Is Too Large**:
   - Setting `n_programs` too high produces near-zero factors or redundant correlated programs. Always inspect program-to-program Pearson correlations and verify each program has non-zero cell usage.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `cnmf`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scikit-learn`, `scipy`
