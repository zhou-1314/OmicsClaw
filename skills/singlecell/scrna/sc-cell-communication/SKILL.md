---
name: sc-cell-communication
description: Load when computing cell-cell ligand-receptor communication on an annotated scRNA AnnData
  via builtin scorer, LIANA, CellPhoneDB, CellChat (R), or NicheNet (R). Skip when assigning cell-type
  labels (use sc-cell-annotation); transcription factor → target regulatory networks (use sc-grn).
trigger: cell communication, cell-cell communication, ligand receptor, cellchat, liana, cellphonedb, nichenet
tags:
- singlecell
- scrna
- cell-communication
- ligand-receptor
- liana
- cellphonedb
- cellchat
- nichenet
---

# Single-Cell Ligand-Receptor Cell-Cell Communication (`sc-cell-communication`)

## 1. Method Decision Matrix & When to Use

Choose a ligand-receptor (L-R) inference framework based on species, statistical testing needs, and experimental design:

| Method (`--method` / `method=`) | Upstream Package | Statistical Paradigm | Species Support | Best Used When |
|---|---|---|---|---|
| **LIANA Consensus** (`method="liana"`) | `liana` (`liana.mt.rank_aggregate`) | Robust rank aggregation across CellPhoneDB, Connectome, logFC, NATMI, SingleCellSignalR | Human (`consensus`) & Mouse (`mouseconsensus`) | **Default choice** for single-condition ligand-receptor discovery; balances specificity and magnitude |
| **CellPhoneDB** (`method="cellphonedb"`) | `cellphonedb` (v4/v5 permutation test) | Empirical cluster-label permutation $p$-values (`iterations=1000`) | Human (`hgnc_symbol`) | Heteromeric multi-subunit receptor complexes requiring formal permutation $p$-values |
| **CellChat** (`method="cellchat_r"`) | R `CellChat` | Mass-action law (`triMean`) with cofactor/agonists and pathway-level network centrality | Human & Mouse | Signaling pathway aggregation and sender/receiver/mediator/influencer role analysis |
| **NicheNet** (`method="nichenet_r"`) | R `nichenetr` | Prior-knowledge ligand-to-target signaling network + DE target enrichment in receiver cells | Human & Mouse | Two-condition (`condition_oi` vs. `condition_ref`) prioritization of ligands driving DE in a specific receiver cell type |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: LIANA Consensus Rank Aggregation (`liana-py`)

```python
import liana as li
import pandas as pd

# 1. Ensure log1p-normalized expression is in adata.X and cell-type labels exist in adata.obs
# Use resource_name="consensus" for human, "mouseconsensus" for mouse
li.mt.rank_aggregate(
    adata,
    groupby="cell_type",
    resource_name="consensus",
    expr_prop=0.1,        # Minimum fraction of cells expressing ligand and receptor in each cluster
    min_cells=5,
    use_raw=False,
    seed=1337,
    verbose=True,
)

# 2. Extract and filter high-specificity, high-magnitude interactions
lr_df = adata.uns["liana_res"].copy()
top_lr = lr_df[lr_df["specificity_rank"] <= 0.05].sort_values("magnitude_rank", ascending=True)
```

### Pattern B: Sender-Receiver Interaction Frequency Matrix & Visualization

```python
# Summarize significant L-R interactions per (source, target) cell-type pair
sig_lr = lr_df[lr_df["specificity_rank"] <= 0.05]
sr_matrix = (
    sig_lr.groupby(["source", "target"], observed=True)
    .size()
    .unstack(fill_value=0)
)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-cell-communication")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `communicate(adata, cell_type_key="cell_type", method="liana", resource_name="consensus", expr_prop=0.1, random_state=1337)`
- `sender_receiver_summary(lr_df)`
- `top_interactions(lr_df, n_top=50)`

## 3. Output Format & Verification Contract

- **Primary L-R Interaction Table** (`tables/lr_interactions.csv` and `tables/top_interactions.csv`):
  - Standardized columns: `source` (sender cell type), `target` (receiver cell type), `ligand` (`ligand_complex`), `receptor` (`receptor_complex`), `score` (`lr_means` or `magnitude_rank`), `specificity_rank` / `cellphone_pvals` / `pvalue`.
- **Network Summaries** (`tables/sender_receiver_summary.csv`):
  - Columns: `source`, `target`, `n_interactions`, `mean_score`.
- **Verification Invariants**:
  - Verify `set(lr_df["source"]).issubset(set(adata.obs[cell_type_key].astype(str)))`.
  - Verify `expr_prop >= 0.1` was enforced so artefactual interactions driven by $1\text{--}2$ outlier cells are excluded.

## 4. Gotchas & Troubleshooting

1. **LIANA `specificity_rank` and `magnitude_rank` Are Aggregate Ranks, Not $p$-Values**:
   - In `liana.mt.rank_aggregate`, smaller `specificity_rank` and `magnitude_rank` values indicate stronger rank consensus across methods (values near $0$ are best), not frequentist $p$-values. For individual permutation $p$-values, inspect `cellphone_pvals` in `adata.uns["liana_res"]`.
2. **Input Expression Matrix Must Be Log-Normalized (`log1p`), Never `sc.pp.scale`d**:
   - L-R scoring methods compute geometric/arithmetic means or products of ligand and receptor expression (`expr_prop` checks fraction of cells $> 0$). Passing zero-centered/negative values from `sc.pp.scale` corrupts `expr_prop` and produces negative ligand-receptor products. If `adata.X` was scaled, restore log1p expression from `adata.raw.to_adata()` or `adata.layers["log1p"]`.
3. **Human vs. Mouse Gene Symbol Casing in L-R Databases**:
   - `resource_name="consensus"` expects uppercase HGNC human symbols (`TGFB1`, `TGFBR1`), whereas mouse datasets (`Tgfb1`, `Tgfbr1`) require `resource_name="mouseconsensus"`. Mismatched casing yields zero matched interactions.
4. **Small Cell-Type Clusters Inflate Permutation Variance**:
   - Clusters with $< 10$ cells produce unstable cluster means and noisy permutation distributions in `CellPhoneDB` and `CellChat`. Always set `min_cells=10` (or filter rare clusters) prior to L-R inference.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `cellphonedb`, `liana`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
