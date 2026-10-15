---
name: sc-grn
description: Load when inferring TF→target gene regulatory networks on a normalised scRNA AnnData via
  pySCENIC (GRNBoost2, cisTarget, AUCell) or correlation GRNs. Skip when computing
  ligand-receptor cell-cell signalling (use sc-cell-communication); predicting genetic-KO
  effects (use sc-in-silico-perturbation).
trigger: grn, gene regulatory, scenic, pyscenic, regulon, transcription factor, grnboost
tags:
- singlecell
- scrna
- grn
- gene-regulatory-network
- scenic
- pyscenic
- grnboost2
- aucell
- cistarget
---

# Single-Cell Gene Regulatory Network & Regulon Inference (`sc-grn`)

## 1. Method Decision Matrix & When to Use

Use this skill to infer transcription factor (TF) $\to$ target gene regulatory networks (GRNs), prune co-expression modules using cis-regulatory motif databases, and score per-cell regulon activity via `pySCENIC` (`GRNBoost2` + `cisTarget` + `AUCell`) or rank-based co-expression fallback.

| Workflow Stage & Goal | Recommended Method | Upstream Library | Required Inputs & Assumptions |
|---|---|---|---|
| **Stage 1: TF $\to$ target co-expression adjacencies** | **GRNBoost2** (`grnboost2`) | `arboreto.algo.grnboost2` | Expression matrix (`layers["counts"]` or normalized `X`) + curated TF list (`tfs`) |
| **Stage 2: Direct binding motif pruning** | **cisTarget** (`prune2df` / `df2regulons`) | `pyscenic.prune` | Feather v2 motif ranking databases (`*.genes_vs_motifs.rankings.feather`) + `.tbl` motif annotations |
| **Stage 3: Per-cell regulon activity scoring** | **AUCell** (`aucell`) | `pyscenic.aucell.aucell` | Unscaled expression ranks per cell + pruned `Regulon` objects |
| **Lightweight TF co-expression fallback** (no arboreto/databases) | **Spearman Co-Expression + Mean/Rank Scoring** (`correlation`) | `scipy.stats.spearmanr` (`kernel.py`) | Log-normalized `adata.X` + TF list (co-expression candidates only, not motif-validated) |

> **Decision Rule**: Co-expression edges from `GRNBoost2` or Spearman correlation capture both direct and indirect associations. Only call a module a **validated regulon** after `cisTarget` promoter/enhancer motif pruning (`NES >= 3.0`).

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Full 3-Step pySCENIC Pipeline (`arboreto` + `pyscenic`)

```python
import pandas as pd
from arboreto.algo import grnboost2
from ctxcore.rnkdb import FeatherRankingDatabase
from pyscenic.utils import modules_from_adjacencies
from pyscenic.prune import prune2df, df2regulons
from pyscenic.aucell import aucell

# 1. Build cell-by-gene expression DataFrame and filter TFs present in adata.var_names
ex_matrix = adata.to_df(layer="counts" if "counts" in adata.layers else None)
tf_names = [tf for tf in candidate_tfs if tf in ex_matrix.columns]

# 2. Infer TF -> target importances with GRNBoost2
adjacencies = grnboost2(expression_data=ex_matrix, tf_names=tf_names, seed=42, verbose=False)

# 3. Convert to co-expression modules and prune against cisTarget feather databases
modules = list(modules_from_adjacencies(adjacencies, ex_matrix))
dbs = [FeatherRankingDatabase(fname=p, name=p) for p in feather_db_paths]
motif_df = prune2df(dbs, modules, motif_annotations_fname=motif_tbl_path, num_workers=4)
regulons = df2regulons(motif_df)

# 4. Score per-cell regulon enrichment with AUCell
auc_mtx = aucell(ex_matrix, regulons, auc_threshold=0.05, seed=42, num_workers=4)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-grn")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `infer_adjacencies(adata, tfs, method="correlation", layer=None, n_top=50)`
- `regulons_from_adjacencies(adjacencies, n_top=50)`
- `score_regulons(adata, regulons, layer=None)`

## 3. Output Format & Verification Contract

- **TF-Target Adjacency Table** (`tables/grn_adjacencies.csv`):
  - Columns: `TF`, `target`, `importance` (sorted descending by `importance`), excluding self-edges (`TF != target`).
- **Regulon Summary & Target Tables** (`tables/grn_regulons.csv`, `tables/grn_regulon_targets.csv`):
  - Columns: `regulon`, `tf`, `n_targets`, `targets`.
- **Per-Cell Regulon Activity Matrix** (`tables/grn_auc_matrix.csv`):
  - Indexed by `adata.obs_names`, with one column per regulon (`regulon_<TF>` or `<TF>(+)`).
- **Verification Invariants**:
  - Assert at least 1 supplied TF overlaps `adata.var_names` and `len(adjacencies) > 0`.
  - Assert `auc_mtx.index.equals(adata.obs_names)` and no `NaN` values exist in activity scores.

## 4. Gotchas & Troubleshooting

1. **Co-Expression vs. Direct Regulatory Binding**:
   - `GRNBoost2` and Spearman correlation identify statistical co-variation, which includes indirect cascade targets and co-regulated genes. Without `cisTarget` motif pruning (`prune2df`), treat edges as co-expression modules rather than direct TF binding.
2. **Dask / Multiprocessing Deadlocks in `arboreto`**:
   - `arboreto.algo.grnboost2` spawns a local Dask distributed cluster that can hang inside nested multiprocessing or constrained container environments. Pass an explicit `client_or_address` or fall back to single-node `n_jobs=1..4`.
3. **Species Gene-Symbol Case Mismatch**:
   - Human TFs (`STAT1`, `IRF1`, `RUNX1`) are uppercase, whereas mouse TFs (`Stat1`, `Irf1`, `Runx1`) use Title case. Always intersect `tfs` with `adata.var_names` and verify species match with cisTarget feather databases (`hg38` vs. `mm10`).
4. **CSV Export Drops `DataFrame.attrs` Provenance**:
   - Backend metadata (`scoring_method`: `"aucell"` vs. `"mean"`, `executed_method`: `"grnboost2"` vs. `"correlation"`) stored in `df.attrs` is lost when writing to CSV; record method provenance in `result.json` or step summaries.

## Dependencies

`anndata`, `arboreto`, `dask`, `matplotlib`, `networkx`, `numpy`, `pandas`, `pyscenic`, `scanpy`, `scikit-learn`, `scipy`, `seaborn`
