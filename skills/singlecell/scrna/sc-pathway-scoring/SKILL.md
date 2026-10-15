---
name: sc-pathway-scoring
description: Load when computing per-cell pathway / gene-set scores on a normalised scRNA AnnData via
  AUCell (R or Python) or Scanpy score_genes. Skip when running condition-vs-control bulk-style enrichment
  on top of a DE table (use sc-enrichment); de-novo gene-program discovery (use sc-gene-programs).
trigger: pathway score, pathway scoring, gene set score, module score, pathway activity, signature score
tags:
- singlecell
- scrna
- pathway-scoring
- aucell
- scanpy-score-genes
- gene-sets
- decoupler
---

# Single-Cell Per-Cell Pathway & Signature Scoring (`sc-pathway-scoring`)

## 1. Method Decision Matrix & When to Use

Select a per-cell gene-set activity scoring algorithm based on sensitivity to library-size dropout, background control strategy, and runtime:

| Method (`--method` / `method=`) | Upstream Package | Scoring Mechanism | Input Matrix Required | Best Used When |
|---|---|---|---|---|
| **AUCell (Python / R)** (`method="aucell_py"` / `"aucell_r"`) | `decoupler.run_aucell` / R `AUCell` | Area Under the Recovery Curve across the top $5\%$ (`auc_threshold=0.05`) highest-expressed genes within each cell | Raw counts or log1p-normalized `adata.X` (rank-invariant per cell) | **Gold standard for single-cell signatures**; completely independent of cross-cell normalization or other cells in the dataset |
| **Scanpy Module Score** (`method="score_genes_py"`) | `scanpy.tl.score_genes` (Seurat `AddModuleScore` equivalent) | Mean expression of signature genes minus mean of expression-binned random control genes (`ctrl_size=50`, `n_bins=25`) | Log1p-normalized `adata.X` | Fast, lightweight signature scoring on normalized expression matrices |
| **Univariate Linear Model (ULM)** | `decoupler.run_ulm` | $t$-value of linear regression between cell expression profile and pathway weights | Log1p-normalized `adata.X` | Weighted gene sets / signed PROGENy pathway footprints (`+1` activating, `-1` inhibitory) |

> **Scope Distinction**: Use `sc-pathway-scoring` to assign a **continuous activity score to every individual cell** (`cells × pathways`). For bulk-style ORA/GSEA $p$-values on a DE gene table, use `sc-enrichment`.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Control-Subtracted Module Scoring (`scanpy.tl.score_genes`)

```python
import scanpy as sc
import pandas as pd

# Ensure adata.X contains log1p-normalized expression (use adata.raw.to_adata() if adata.X was scaled)
gene_sets = {
    "T_cell_cytotoxicity": ["PRF1", "GZMB", "GZMA", "GNLY", "NKG7", "IFNG"],
    "IFN_alpha_response": ["ISG15", "IFI6", "IFIT1", "IFIT3", "MX1", "OAS1"],
    "Glycolysis": ["HK2", "PFKP", "ALDOA", "GAPDH", "PKM", "LDHA"],
}

score_cols = {}
for pathway, genes in gene_sets.items():
    matched = [g for g in genes if g in adata.var_names]
    if len(matched) >= 2:
        col = f"enrich__{pathway}"
        sc.tl.score_genes(
            adata,
            gene_list=matched,
            ctrl_size=min(50, max(len(matched), 1)),
            n_bins=25,
            score_name=col,
            random_state=42,
            use_raw=False,
        )
        score_cols[pathway] = adata.obs[col]

scores_df = pd.DataFrame(score_cols, index=adata.obs_names)
group_means = scores_df.groupby(adata.obs["leiden"], observed=True).mean()
```

### Pattern B: Rank-Based AUCell Scoring (`decoupler` or `numpy`)

```python
import numpy as np
import pandas as pd

# Pure-Python AUCell recovery curve over top 5% expressed genes per cell
dense = adata.X.toarray() if hasattr(adata.X, "toarray") else np.asarray(adata.X)
k = max(int(np.ceil(0.05 * adata.n_vars)), 1)
top_ranks = np.argsort(-dense, axis=1)[:, :k]
gene_idx = {g: i for i, g in enumerate(adata.var_names)}

aucell_scores = {}
for pathway, genes in gene_sets.items():
    cols = [gene_idx[g] for g in genes if g in gene_idx]
    if cols:
        hits = np.isin(top_ranks, cols)
        aucell_scores[pathway] = hits.cumsum(axis=1).sum(axis=1) / float(len(cols) * k)

scores_df = pd.DataFrame(aucell_scores, index=adata.obs_names)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-pathway-scoring")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `score_gene_sets(adata, gene_sets, method="score_genes_py", auc_threshold=0.05, ctrl_size=50, random_state=42)`
- `group_scores(adata, scores_df, groupby="leiden")`
- `top_pathways(scores_df, n_top=20)`

## 3. Output Format & Verification Contract

- **Cell-by-Pathway Score Matrix** (`tables/enrichment_scores.csv` or `tables/pathway_scores.csv`):
  - Index matches `adata.obs_names` (`scores_df.index.equals(adata.obs_names)`), columns correspond to scored gene sets.
  - Annotated `AnnData` (`intermediate/processed.h5ad`) stores scores in `adata.obs["enrich__<pathway>"]`.
- **Group & Overlap Summaries** (`tables/group_mean_scores.csv`, `tables/gene_set_overlap.csv`, `tables/top_pathways.csv`):
  - Mean score per cluster/cell-type group and matched gene count per signature.
- **Verification Invariants**:
  - For `aucell_py` / `aucell_r`, all scores are bounded in $[0, 1]$ (`scores.min().min() >= 0.0` and `scores.max().max() <= 1.0`).
  - Confirm at least $1$ gene per retained pathway matched `adata.var_names`.

## 4. Gotchas & Troubleshooting

1. **Do Not Run `score_genes` or `AUCell` on HVG-Subsetted `adata` (`2,000` Genes)**:
   - If `adata` was subsetted in-place to $2,000$ highly variable genes, housekeeping metabolic genes and $70\text{--}80\%$ of pathway members will be missing from `adata.var_names`, severely degrading AUCell rankings and `score_genes` expression bins. Always score against the full filtered transcriptome (or `adata.raw.to_adata()`).
2. **`score_genes` Can Produce Negative Scores (`AUCell` Is Strictly Non-Negative)**:
   - `sc.tl.score_genes` subtracts the mean expression of binned control genes, so cells expressing a pathway below the background bin average receive **negative** scores. By contrast, `AUCell` measures area under the top-$5\%$ recovery curve and is strictly in $[0, 1]$.
3. **Gene Symbol Namespace & Species Casing (`MSigDB` Human vs. Mouse)**:
   - MSigDB GMT files use uppercase human HGNC symbols (`IFNG`, `STAT1`). When scoring mouse `AnnData` (`Ifng`, `Stat1`), convert gene-set symbols to title case or map orthologous symbols before calling `score_gene_sets`, and always inspect `gene_set_overlap` first.
4. **Tie-Breaking in Sparse Zero-Inflated Rankings**:
   - Because $>85\%$ of genes in a single cell have $0$ UMI counts, setting `auc_threshold` higher than the fraction of detected genes per cell ($>0.10$) forces `AUCell` to rank arbitrary zero-count ties. Keep `auc_threshold=0.05` (top $5\%$ of genes).

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `gseapy`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
