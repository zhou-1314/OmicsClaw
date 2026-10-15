---
name: sc-drug-response
description: Load when scoring drug sensitivity per cluster on an annotated scRNA AnnData via
  drug-target signature correlation or CaDRReS-Sc models (GDSC / PRISM). Skip when the AnnData
  has no cluster labels (use sc-clustering); predicting genetic-perturbation effects (use
  sc-in-silico-perturbation).
tags:
- singlecell
- scrna
- drug-response
- pharmacogenomics
- cadrres
- gdsc
- prism
---

# Single-Cell Drug Target Expression & Pharmacogenomic Scoring (`sc-drug-response`)

## 1. Method Decision Matrix & When to Use

Use this skill to summarize drug-target gene set expression across annotated cell clusters (`simple_correlation` / target-mean scoring) or to apply pretrained pharmacogenomic response models (`CaDRReS-Sc` trained on GDSC / PRISM) when local model weights are available.

| Analytical Goal | Recommended Method | Upstream Library / Implementation | Input Assumptions & Interpretation |
|---|---|---|---|
| **Descriptive drug-target expression screen across clusters** | **Target Gene-Set Mean** (`simple_correlation` / `score_drug_targets`) | `scanpy` / `numpy` group mean over drug-target signatures (`kernel.py`) | Log-normalized `adata.X` with gene symbols and cluster column (`cluster_key`); descriptive expression, **not** IC50 prediction |
| **Control-adjusted per-cell drug signature activity** | **Scanpy Gene-Set Scoring** (`sc.tl.score_genes`) | `scanpy.tl.score_genes` | Log-normalized `adata.X` + curated target/resistance gene list |
| **Pretrained pharmacogenomic IC50 / sensitivity projection** | **CaDRReS-Sc** (`cadrres`) | `omicverse.single.Drug_Response` + local `CaDRReS-Sc` checkout & weights | Log-normalized `adata.X`, local GDSC/PRISM model directory; lower $\log(\text{IC}_{50})$ indicates higher predicted sensitivity |

> **Decision Rule**: Descriptive target expression (`mean_target_expression`) measures whether a cluster expresses the canonical molecular targets of a compound; it does not predict clinical efficacy or distinguish sensitivity from resistance. Never compare `mean_target_expression` directly against `CaDRReS` model `Score` units.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Cluster-Level Drug-Target Signature Scoring (`scanpy` + `pandas`)

```python
import numpy as np
import pandas as pd
from scipy import sparse

# Define drug-target gene sets (gene symbols matching adata.var_names)
drug_targets = {
    "Imatinib": ["ABL1", "KIT", "PDGFRA", "PDGFRB"],
    "Venetoclax": ["BCL2", "BCL2L1", "MCL1"],
    "Osimertinib": ["EGFR", "ERBB2", "MET"],
}

# Compute mean log-normalized expression per (cluster, drug) and gene overlap
rows = []
for group, idx in adata.obs.groupby("leiden", observed=True).groups.items():
    sub = adata[idx]
    for drug, genes in drug_targets.items():
        present = [g for g in genes if g in adata.var_names]
        if not present:
            continue
        mat = sub[:, present].X
        mean_val = float(mat.mean()) if not sparse.issparse(mat) else float(mat.mean())
        rows.append({
            "Cluster": str(group),
            "Drug": drug,
            "mean_target_expression": round(mean_val, 4),
            "TargetGenes": len(present),
            "TotalTargets": len(genes),
            "OverlapPct": round(100.0 * len(present) / len(genes), 1),
        })

rankings_df = pd.DataFrame(rows).sort_values(["Cluster", "mean_target_expression"], ascending=[True, False])
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-drug-response")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `builtin_drug_targets()`
- `score_drug_targets(adata, cluster_key=None, drug_targets=None)`
- `top_drugs(table, n_top=10)`

## 3. Output Format & Verification Contract

- **Drug Rankings Table** (`tables/drug_rankings.csv`):
  - Default target-expression screen columns: `Cluster`, `Drug`, `mean_target_expression`, `TargetGenes`, `TotalTargets`, `OverlapPct`, `MatchedGenes`.
  - CaDRReS model columns: `Cluster`, `Drug`, `Score`, `DrugDB`.
- **Annotated AnnData** (`intermediate/drug_response.h5ad` or `processed.h5ad`):
  - Group-level summary scores mapped onto `adata.obs["drug_score_<drug>"]` for visualization.
- **Verification Invariants**:
  - Assert `len(rankings_df) > 0` and verify `TargetGenes > 0` (if `n_drugs_scored == 0`, check whether `adata.var_names` contains Ensembl IDs instead of HGNC gene symbols).
  - Verify `OverlapPct` before interpreting drugs with only 1 matched gene.

## 4. Gotchas & Troubleshooting

1. **Legacy Name `simple_correlation` Is Mean Expression**:
   - The CLI method name `simple_correlation` computes unweighted mean expression (`mean_target_expression`) across matched genes in each drug's gene set, not a correlation coefficient.
2. **Incompatible Units Between `mean_target_expression` and `CaDRReS` `Score`**:
   - `mean_target_expression` is in log-normalized expression units (higher $=$ more target mRNA), whereas CaDRReS GDSC `Score` estimates $\log(\text{IC}_{50})$ or sensitivity score depending on `drug_db`. Never mix or co-rank the two columns.
3. **Gene Symbol vs. Ensembl ID Mismatch (`n_drugs_scored == 0`)**:
   - Built-in drug target dictionaries use human HGNC symbols (e.g., `EGFR`, `BCL2`). If `adata.var_names` stores Ensembl IDs (`ENSG...`) or mouse Title-case symbols (`Egfr`), map symbols first and inspect `OverlapPct`.
4. **Target Expression Does Not Equal Clinical Sensitivity**:
   - Built-in sets include primary targets and resistance-associated genes without signed weights. Treat output rankings strictly as exploratory research hypotheses.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `omicverse`, `pandas`, `scanpy`, `scipy`, `seaborn`

omicverse is needed only for the model-backed method. The default and notebook
example use local expression means without CaDRReS.
