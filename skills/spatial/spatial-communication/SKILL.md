---
name: spatial-communication
description: Load when computing ligand-receptor communication on labelled spatial AnnData with LIANA, CellPhoneDB, FastCCC or CellChat. Skip unlabelled data (use spatial-annotate) and non-spatial scRNA analysis (use sc-cell-communication).
trigger: cell communication, ligand receptor, LIANA, CellPhoneDB, FastCCC, CellChat
tags:
- spatial
- communication
- ligand-receptor
---

# Spatial Cell-Cell & Ligand-Receptor Communication (`spatial-communication`)

## 1. Method Decision Matrix & When to Use

Use this playbook to infer ligand-receptor (L-R) interactions and spatially constrained signaling between annotated cell types or spatial domains:

| Method (`--method` / `method=`) | Upstream Package | Spatial Constraint | Best Used When |
|---|---|---|---|
| `method="liana"` (`--method liana`) | `liana` (`li.mt.rank_aggregate` / `li.mt.bivariate`) | Global cluster L-R consensus + spatially weighted bivariate Moran's $R$ | Consensus rank aggregation across CellPhoneDB, NATMI, Connectome, LogFC, SingleCellSignalR |
| `method="squidpy"` (`--method squidpy`) | `squidpy.gr.ligrec` | Cluster permutation test (`sq.gr.ligrec`) paired with `sq.gr.nhood_enrichment` | Fast permutation-based L-R screening on `AnnData` |
| `method="cellchat"` (`--method cellchat`) | R `CellChat` | Spatial distance-weighted mass action | Multi-subunit receptor complexes and signaling pathway-level aggregation |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: LIANA Consensus Ligand-Receptor Inference (`method="liana"`)

```python
import liana as li
import scanpy as sc

# Ensure cell-type / domain labels are categorical and log1p expression is in adata.X
adata.obs["cell_type"] = adata.obs["cell_type"].astype(str).astype("category")

# Run LIANA consensus rank aggregation (resource_name="consensus" for human, "mouseconsensus" for mouse)
li.mt.rank_aggregate(
    adata,
    groupby="cell_type",
    resource_name="consensus",
    expr_prop=0.1,
    use_raw=False,
    verbose=False,
)
lr_df = adata.uns["liana_res"].copy()
# Columns include: source, target, ligand_complex, receptor_complex, magnitude_rank, specificity_rank
```

### Pattern B: Spatially Constrained Bivariate L-R (`liana` + `squidpy`)

```python
import squidpy as sq
import liana as li

# Build spatial neighbor graph from adata.obsm["spatial"]
sq.gr.spatial_neighbors(adata, coord_type="generic", delaunay=True)

# Compute spatially weighted bivariate ligand-receptor scores
li.mt.bivariate(
    adata,
    resource_name="consensus",
    local_name="cosine",
    n_perms=100,
    use_raw=False,
     key_added="local_scores",
)
```

## 3. Output Format & Verification Contract

- **Interaction Table** (`tables/lr_interactions.csv`):
  - Columns: `source`, `target`, `ligand_complex` (or `ligand`), `receptor_complex` (or `receptor`), interaction score/magnitude (`lr_means`, `magnitude_rank`), and specificity/permutation $p$-value (`pvalue`, `specificity_rank`).
- **Verification**:
  - Verify that top-ranked source-target pairs are physically co-localized in tissue space using `sq.gr.nhood_enrichment(adata, cluster_key="cell_type")` so distant, non-interacting regions are not over-interpreted as paracrine signaling.

## 4. Gotchas & Troubleshooting

1. **Species Resource Selection (`consensus` vs. `mouseconsensus`)**:
   - LIANA's default `resource_name="consensus"` uses uppercase human HGNC gene symbols. For mouse datasets (`TitleCase` gene symbols), either pass `resource_name="mouseconsensus"` or convert gene symbols; otherwise 0 ligand-receptor pairs will match.
2. **Scanpy `use_raw` Default in `liana` and `squidpy`**:
   - Both `li.mt.rank_aggregate` and `sq.gr.ligrec` default to `use_raw=True` if `adata.raw` exists. Pass `use_raw=False` explicitly when `adata.X` holds your log1p-normalized expression.

## Dependencies

`anndata`, `cellphonedb`, `fastccc`, `liana`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`, `CellChat`, `Matrix`

The R backend requires an Rscript executable.
