---
name: sc-enrichment
description: Load when running bulk-style pathway enrichment (ORA / GSEA / GSEA-R / GSVA-R) on a per-group
  ranked DE / marker list against a gene-set library. Skip when computing per-cell pathway scores in-place
  (use sc-pathway-scoring); de-novo gene-program discovery (use sc-gene-programs).
trigger: sc enrichment, single-cell enrichment, GO enrichment, KEGG enrichment, GSEA, ORA, pathway enrichment
tags:
- singlecell
- scrna
- enrichment
- gsea
- ora
- gsva
- decoupler
- pathway-enrichment
---

# Single-Cell Pathway & Gene-Set Enrichment Analysis (`sc-enrichment`)

## 1. Method Decision Matrix & When to Use

Select the enrichment framework based on whether you have a thresholded gene list, a full continuous DE ranking, or group-level expression profiles:

| Method (`--method` / `method=`) | Upstream Package | Statistical Test | Required Input | Best Used When |
|---|---|---|---|---|
| **Preranked GSEA** (`method="gsea"`) | `gseapy.prerank` / R `fgsea` (`clusterProfiler`) | Kolmogorov-Smirnov weighted running-sum statistic (`NES`, permutation `FDR q-val`) | Full ranked gene table (`names`, `scores` or `stat`/`log2FoldChange`) without p-value truncation | **Gold standard** for condition DE or cluster rankings; detects coordinated subtle shifts across entire pathways |
| **Over-Representation Analysis (ORA)** (`method="ora"`) | `gseapy.enrich` / `scipy.stats.hypergeom` | One-sided Fisher's exact / hypergeometric test + BH FDR | Significant upregulated gene list (`padj < 0.05`, `log2FC > 0.25`) + **explicit background universe** | Quick functional annotation of top cluster markers or discrete gene modules |
| **Group-Level GSVA** (`method="gsva"`) | R `GSVA` (`gsva` / `ssgsea`) | Non-parametric kernel estimation of cumulative density across groups | Cluster/group mean log-normalized expression matrix + gene-set dict/GMT | Comparing pathway activity across many clusters or pseudobulk samples simultaneously |

> **Scope Distinction**: Use `sc-enrichment` for **group-level or DE-table** pathway testing ($p$-values, FDR, NES). To compute **per-cell** pathway activity scores stored in `adata.obs`, use `sc-pathway-scoring`.

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Preranked GSEA on Cluster or Pseudobulk DE Rankings (`gseapy.prerank`)

```python
import gseapy as gp
import pandas as pd

# 1. Prepare a deduplicated, finite ranking series sorted descending by test statistic / score
# From sc.get.rank_genes_groups_df(adata, group="0") or pydeseq2 results:
rnk_df = (
    de_df[["names", "scores"]]
    .replace([ float("inf"), float("-inf") ], pd.NA)
    .dropna()
    .assign(_abs=lambda d: d["scores"].abs())
    .sort_values("_abs", ascending=False)
    .drop_duplicates(subset=["names"])
    .sort_values("scores", ascending=False)[["names", "scores"]]
)

# 2. Run Preranked GSEA against MSigDB Hallmark, Reactome, KEGG, or a local GMT file
pre_res = gp.prerank(
    rnk=rnk_df,
    gene_sets="MSigDB_Hallmark_2020",  # Or local path "/path/to/c2.cp.reactome.v2023.1.Hs.symbols.gmt"
    min_size=5,
    max_size=500,
    permutation_num=1000,
    seed=123,
    outdir=None,
    verbose=False,
)
gsea_table = pre_res.res2d.sort_values("FDR q-val", ascending=True)
```

### Pattern B: Hypergeometric Over-Representation Analysis with Explicit Background (`gseapy.enrich`)

```python
import gseapy as gp

sig_genes = de_df.loc[(de_df["pvals_adj"] < 0.05) & (de_df["logfoldchanges"] >= 0.25), "names"].tolist()
background_genes = adata.var_names.tolist()  # Crucial: restrict universe to expressed/tested genes!

enr_res = gp.enrich(
    gene_list=sig_genes,
    gene_sets="GO_Biological_Process_2023",
    background=background_genes,
    cutoff=0.05,
    outdir=None,
)
ora_table = enr_res.results.sort_values("Adjusted P-value", ascending=True)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-enrichment")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `run_ora(gene_list, gene_sets="MSigDB_Hallmark_2020", background=None, cutoff=0.05)`
- `run_prerank_gsea(ranking_df, gene_col="names", score_col="scores", gene_sets="MSigDB_Hallmark_2020", min_size=5, max_size=500, permutation_num=100, seed=123)`
- `enrichment_summary(df, fdr_col="Adjusted P-value", fdr=0.05)`

## 3. Output Format & Verification Contract

- **Enrichment Results Tables** (`tables/enrichment_results.csv`, `tables/enrichment_significant.csv`, `tables/top_terms.csv`):
  - For ORA: `Term`, `Overlap`, `P-value`, `Adjusted P-value`, `Odds Ratio`, `Combined Score`, `Genes`.
  - For Preranked GSEA: `Term`, `ES`, `NES`, `NOM p-val`, `FDR q-val`, `FWER p-val`, `Tag %`, `Lead_genes`.
- **Verification Invariants**:
  - Confirm `len(enrichment_results) > 0` (at least one gene set satisfied `min_size <= overlap <= max_size`).
  - Confirm `Adjusted P-value` / `FDR q-val` values are finite numbers in $[0, 1]$.

## 4. Gotchas & Troubleshooting

1. **Always Supply the Tested Gene Universe as `background` in ORA**:
   - In single-cell RNA-seq, only $\sim 10,000\text{--}15,000$ genes are detected in the tissue. Using the default whole-genome background ($\sim 20,000+$ genes) in hypergeometric ORA artificially inflates significance for generic tissue-expressed pathways. Always pass `background=adata.var_names.tolist()`.
2. **Never Truncate the Gene List Before Running `gseapy.prerank`**:
   - Preranked GSEA requires the **entire tested gene ranking** from most upregulated (`+score`) to most downregulated (`-score`) to calibrate the Kolmogorov-Smirnov null distribution. Passing only `padj < 0.05` genes to `prerank` invalidates `NES` and `FDR q-val`.
3. **Deduplicate and Strip `±inf` / `NaN` Scores Before `prerank`**:
   - Scanpy `rank_genes_groups` log-fold changes often contain `±inf` (when a gene has $0$ expression in the reference group) or duplicate gene symbols. Rank by the Wilcoxon Z-score (`scores`) or Wald statistic (`stat`) rather than raw `logfoldchanges`, and drop `NaN`/duplicates prior to `gp.prerank`.
4. **Offline Execution vs. Remote Enrichr Library Names**:
   - Passing a string library alias like `"MSigDB_Hallmark_2020"` to `gseapy` queries the Enrichr REST API and requires internet access. In air-gapped or offline environments, pass a local `.gmt` file path or a Python `dict[str, list[str]]` directly to `gene_sets=`.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`adjustText`, `anndata`, `gseapy`, `matplotlib`, `networkx`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
