---
name: spatial-de
description: Load when ranking spatial cluster markers or comparing two spatial groups. Skip when the data is single-cell (use sc-de), bulk (use bulkrna-de), or for spatially variable expression (use spatial-genes).
trigger: differential expression, marker gene, pseudobulk, Wilcoxon, t-test, PyDESeq2, spatial DE
tags:
- spatial
- differential-expression
- markers
- wilcoxon
- t-test
- pydeseq2
- pseudobulk
---

# Spatial Transcriptomics Differential Expression (`spatial-de`)

## 1. Method Decision Matrix & When to Use

Distinguish between **inter-domain / inter-condition differential expression** (this skill: `spatial-de`) and **coordinate-based spatially variable gene discovery** (use `spatial-genes` for Moran's $I$ / SpatialDE / SPARK-X):

| Experimental Design | Replicates / Slides (`sample_key`) | Recommended Method | Upstream Library | Why |
|---|---|---|---|---|
| **Domain / Niche Marker DE** (e.g., Tumor core vs. Stroma on 1 slide) | Single slide | **Wilcoxon Rank-Sum** (`method="wilcoxon"`) | `scanpy.tl.rank_genes_groups` | Fast non-parametric ranking across spatial clusters/domains |
| **Multi-Slide / Multi-Patient Domain DE** (e.g., Treated vs. Control within Domain 2) | $\ge 3$ slides per condition | **Pseudobulk NB-GLM** (`method="pydeseq2"`) | `pydeseq2` (`DeseqDataSet` + `DeseqStats`) | Aggregates spots per `(slide, domain)` to prevent spatial autocorrelation & pseudoreplication |
| **Parametric Quick Screen** | Single slide | **Welch $t$-test** (`method="t-test"`) | `scanpy.tl.rank_genes_groups` | Parametric mean comparison with unequal variance |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Spatial Domain / Niche Marker DE (`scanpy`)

```python
import scanpy as sc
import pandas as pd

# Rank genes across spatial domains (e.g., "spatial_domain" or "leiden")
sc.tl.rank_genes_groups(
    adata,
    groupby="leiden",
    method="wilcoxon",
    pts=True,
    use_raw=False,
)
de_df = sc.get.rank_genes_groups_df(adata, group=None)
sig_df = de_df[(de_df["pvals_adj"] < 0.05) & (de_df["logfoldchanges"].abs() >= 0.5)]
```

### Pattern B: Multi-Slide Pseudobulk DE (`pydeseq2`)

```python
import numpy as np
import pandas as pd
from pydeseq2.dds import DeseqDataSet
from pydeseq2.ds import DeseqStats

# Aggregate raw spot counts in adata.layers["counts"] by (slide_id, condition)
raw_mat = adata.layers["counts"] if "counts" in adata.layers else adata.X
rows, meta = {}, {}
for (slide, cond), idx in adata.obs.groupby(["slide_id", "condition"], observed=True).groups.items():
    if len(idx) < 10:
        continue
    sub = raw_mat[adata.obs.index.get_indexer(idx)]
    rows[str(slide)] = np.asarray(sub.sum(axis=0)).ravel().astype(int)
    meta[str(slide)] = {"condition": str(cond), "n_spots": int(len(idx))}

counts_df = pd.DataFrame.from_dict(rows, orient="index", columns=adata.var_names)
counts_df = counts_df.loc[:, counts_df.sum(axis=0) >= 10]
meta_df = pd.DataFrame.from_dict(meta, orient="index")

dds = DeseqDataSet(counts=counts_df, metadata=meta_df, design_factors="condition", quiet=True)
dds.deseq2()
stats_res = DeseqStats(dds, contrast=["condition", "disease", "healthy"], quiet=True)
stats_res.summary()
de_df = stats_res.results_df.reset_index().rename(columns={"index": "gene"})
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-de")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `spatial_de(adata, groupby="leiden", method="wilcoxon", layer=None)`
- `de_summary(df, fdr=0.05)`

## 3. Output Format & Verification Contract

- **DE Results Table** (`tables/spatial_de_results.csv`):
  - Contains `group` (or contrast), `names`/`gene`, `logfoldchanges`/`log2FoldChange`, `pvals`/`pvalue`, `pvals_adj`/`padj`.
- **Verification**:
  - Verify `adata.obsm["spatial"]` coordinates remain intact when plotting top domain markers in tissue space (`sc.pl.spatial` or `sq.pl.spatial_scatter`).

## 4. Gotchas & Troubleshooting

1. **Spatial Autocorrelation Inflates Spot-Level $p$-Values**:
   - Adjacent Visium spots or Xenium cells share microenvironment signals and mRNA lateral diffusion. Treat single-slide spot-level Wilcoxon $p$-values as **effect-size rankings**, and always use slide-level pseudobulk (`pydeseq2`) when testing across biological samples.
2. **Spot Admixture in Visium (55 $\mu$m Resolution)**:
   - A "T-cell zone vs. B-cell zone" DE on Visium spots frequently surfaces immunoglobulin or stromal transcripts due to multi-cell spot admixture. Cross-check with deconvolved cell-type proportions (`spatial-deconv`) when interpreting cell-type-specific claims.

## Dependencies

`anndata`, `matplotlib`, `numpy`, `pandas`, `pydeseq2`, `scanpy`, `scipy`, `seaborn`
