---
name: sc-in-silico-perturbation
description: Load when predicting in-silico gene knockout effects on a normalised scRNA AnnData via GRN-based
  propagation (Python) or scTenifoldKnk (R). Skip when you have a real Perturb-seq / CRISPR screen (use
  sc-perturb); predicting drug sensitivity (use sc-drug-response).
tags:
- singlecell
- scrna
- in-silico-perturbation
- knockout
- grn
- sctenifoldknk
---

# Single-Cell In-Silico Gene Knockout & Network Perturbation (`sc-in-silico-perturbation`)

## 1. Method Decision Matrix & When to Use

Use this skill to rank genes whose co-expression or manifold alignment shifts when a target gene (`ko_gene`) is virtually ablated in an unperturbed scRNA-seq dataset via `scTenifoldKnk` (R) or fast GRN correlation edge-removal (`grn_ko`).

| Analytical Scenario | Recommended Method | Upstream Library / Implementation | Statistical Output & Requirements |
|---|---|---|---|
| **Manifold-aligned virtual KO with statistical significance** | **scTenifoldKnk** (`sctenifoldknk`) | R `scTenifoldKnk` (PCNet tensor decomposition + manifold alignment) | `FC`, `Z`, `p.value`, `p.adj` (requires `Rscript` + `scTenifoldKnk`) |
| **Fast Python co-expression edge-removal ranking** | **GRN Edge-Removal** (`grn_ko` / `knockout_correlation`) | High-variance gene Pearson network edge ablation (`kernel.py`) | Descriptive `dr_score` and `wt_ko_corr` (no $p$-values; fast Python screen) |
| **Foundation-model embedding perturbation** | **Geneformer / scGPT In-Silico Perturbation** | `geneformer.InSilicoPerturber` | Cosine shift in transformer cell embeddings upon token deletion |
| **Real CRISPR / Perturb-seq screen analysis** | **Mixscape / E-Distance** | `pertpy.tl.Mixscape` (use `sc-perturb`) | Requires experimental sgRNA guide assignments |

> **Decision Rule**: Use `sc-in-silico-perturbation` only for hypothesis generation on unperturbed baseline data. When experimental CRISPR / Perturb-seq data exist, use `sc-perturb` instead. Never fabricate $p$-values from descriptive `grn_ko` correlation edge-removal scores.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Python Co-Expression Edge-Removal Screen (`grn_ko`)

```python
import numpy as np
import pandas as pd
from scipy import sparse

ko_gene = "STAT1"
if ko_gene not in adata.var_names:
    raise ValueError(f"Target ko_gene '{ko_gene}' not found in adata.var_names.")

# 1. Select top 2000 high-variance genes + ensure ko_gene is included
mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
dense = mat.toarray().astype(float) if sparse.issparse(mat) else np.asarray(mat, dtype=float)
variances = np.var(dense, axis=0)
top_idx = list(np.argsort(variances)[::-1][:2000])
ko_full_idx = int(adata.var_names.get_loc(ko_gene))
if ko_full_idx not in top_idx:
    top_idx[-1] = ko_full_idx

sub_genes = adata.var_names[top_idx].astype(str)
sub_mat = dense[:, top_idx]

# 2. Compute WT Pearson correlation matrix and ablate ko_gene row/column
wt_net = np.nan_to_num(np.corrcoef(sub_mat, rowvar=False), nan=0.0)
np.fill_diagonal(wt_net, 0.0)
ko_net = wt_net.copy()
ko_pos = int(np.where(sub_genes == ko_gene)[0][0])
ko_net[ko_pos, :] = 0.0
ko_net[:, ko_pos] = 0.0

# 3. Rank genes by mean absolute edge change (dr_score)
dr_score = np.abs(wt_net - ko_net).mean(axis=1)
wt_ko_corr = wt_net[:, ko_pos]
diff_df = pd.DataFrame({"gene": sub_genes, "dr_score": dr_score, "wt_ko_corr": wt_ko_corr}).sort_values("dr_score", ascending=False)
```

### Pattern B: R `scTenifoldKnk` Virtual Knockout (`Rscript`)

```r
library(scTenifoldKnk)
# countMatrix: genes x cells raw integer count matrix
res <- scTenifoldKnk(
  countMatrix = count_mat,
  gKO = "STAT1",
  qc = FALSE,
  nc_nNet = 10,
  nc_nCells = 500,
  nc_nComp = 3
)
write.csv(res$diffRegulation, "tables/tenifold_diff_regulation.csv", row.names = FALSE)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-in-silico-perturbation")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `knockout_correlation(adata, ko_gene, n_top_genes=2000)`
- `top_perturbed_genes(table, n_top=15)`

## 3. Output Format & Verification Contract

- **Primary Perturbation Ranking Table** (`tables/diff_regulation.csv`):
  - For `grn_ko`: `gene`, `dr_score`, `wt_ko_corr` (sorted descending by `dr_score`).
  - For `sctenifoldknk` (`tables/tenifold_diff_regulation.csv`): `gene`, `distance`, `Z`, `FC`, `p.value`, `p.adj`.
- **Annotated AnnData** (`intermediate/perturbed.h5ad` or `processed.h5ad`):
  - `adata.var["perturbation_dr_score"]`: per-gene perturbation score aligned to `adata.var_names`.
- **Verification Invariants**:
  - Assert `ko_gene in table["gene"].values` and `table["dr_score"].ge(0).all()`.
  - Verify `ko_gene` itself has the largest edge-removal sum (since all of its edges are zeroed simultaneously).

## 4. Gotchas & Troubleshooting

1. **Target Gene Self-Score Asymmetry in `grn_ko`**:
   - When zeroing both the row and column of `ko_gene`, the target gene loses all $N-1$ of its edges, whereas every other gene $g \neq \text{ko\_gene}$ loses only a single edge ($|r_{g,\text{ko}}| / N$). Do not compare the target gene's own `dr_score` magnitude directly to downstream genes.
2. **Descriptive `dr_score` Has No $p$-Value**:
   - The Python `grn_ko` method is a deterministic correlation edge-removal ranking and does not emit `p_value`, `p.adj`, or `z_score`. Use `sctenifoldknk` when chi-squared manifold-alignment $p$-values are required.
3. **Target Gene Filtered Out by HVG Selection**:
   - Transcription factors are often expressed at low-to-moderate levels and may fall outside the top 2,000 HVGs. Always explicitly force `ko_gene` into the selected gene subset before building the network.
4. **Sparsity & Zero-Variance Genes**:
   - Genes expressed in zero or one cell produce `0 / 0` (`NaN`) in `np.corrcoef`. Always sanitize correlation matrices with `np.nan_to_num(..., nan=0.0)` before computing edge differences.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`

The R method additionally requires the scTenifoldKnk R package.
