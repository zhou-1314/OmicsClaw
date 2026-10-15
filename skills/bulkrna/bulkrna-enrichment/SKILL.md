---
name: bulkrna-enrichment
description: Load when running pathway / GO term enrichment on a bulk RNA-seq DE result list. Skip when
  the input is single-cell (use sc-enrichment); the input is spatial (use spatial-enrichment); metabolite
  pathways (use metabolomics-pathway-enrichment).
trigger: bulk enrichment, pathway analysis, GSEA, ORA, GO enrichment, KEGG, bulk pathway
tags:
- bulkrna
- enrichment
- GSEA
- ORA
- GO
- KEGG
- Reactome
- pathway
---

# Bulk RNA-seq Pathway & Gene Set Enrichment (`bulkrna-enrichment`)

## 1. Method Decision Matrix & When to Use

Choose between threshold-free ranked enrichment (GSEA) and threshold-based Over-Representation Analysis (ORA):

| Input Data & Analytical Goal | Method (`method=`) | Upstream Package | Why |
|---|---|---|---|
| **Full ranked DE table** (all expressed genes with `log2FoldChange` and `pvalue`) | Pre-ranked Gene Set Enrichment Analysis (`method="gsea"`) | `gseapy.prerank` | Detects coordinated pathway shifts without arbitrary `padj` / $|\text{LFC}|$ cutoffs |
| **Discrete DEG list** (e.g., significant up/down genes or a WGCNA module) | Over-Representation Analysis (`method="ora"`) | `gseapy.enrich` or `scipy.stats.hypergeom` + BH-FDR | One-sided Fisher's exact / hypergeometric test against the measured transcriptome background |
| **Multi-condition or sample-level pathway activity** | Single-sample GSEA / GSVA | `gseapy.ssgsea` / `decoupler` | Scores pathway activity per individual bulk sample for heatmaps or clinical regression |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `gseapy.prerank` (GSEA) and `gseapy.enrich` (ORA)

```python
import numpy as np
import pandas as pd
import gseapy as gp

# 1. Load full DE results (indexed or columned by gene symbol)
de_df = pd.read_csv("tables/de_results.csv").dropna(subset=["gene", "log2FoldChange", "pvalue"])

# 2. Construct signed ranking metric: sign(LFC) * -log10(pvalue), breaking ties cleanly
p_clipped = de_df["pvalue"].clip(lower=1e-300)
de_df["rank_metric"] = np.sign(de_df["log2FoldChange"]) * (-np.log10(p_clipped))
rnk = de_df.set_index("gene")["rank_metric"].sort_values(ascending=False)

# 3A. Run pre-ranked GSEA against MSigDB Hallmark or local GMT/JSON gene sets
pre_res = gp.prerank(
    rnk=rnk,
    gene_sets="MSigDB_Hallmark_2020",  # or dict[str, list[str]] / path to .gmt
    min_size=10,
    max_size=500,
    permutation_num=1000,
    seed=42,
    outdir=None,
    no_plot=True,
)
gsea_table = pre_res.res2d.sort_values("FDR q-val")

# 3B. Run Over-Representation Analysis (ORA) with explicit expressed-gene background
sig_genes = de_df.loc[(de_df["padj"] < 0.05) & (de_df["log2FoldChange"].abs() >= 1.0), "gene"].tolist()
ora_res = gp.enrich(
    gene_list=sig_genes,
    gene_sets="KEGG_2021_Human",
    background=de_df["gene"].unique().tolist(),
    outdir=None,
)
```

### Pattern B: Offline Hypergeometric ORA via `use_skill("bulkrna-enrichment")`

```python
# Calling use_skill("bulkrna-enrichment") auto-injects kernel.py helpers into the python kernel:
ora_df = hypergeometric_ora(
    query_genes=sig_genes,
    gene_sets=pathway_dict,
    background_genes=de_df["gene"].unique().tolist(),
)
rnk_series = build_prerank_series(de_df)
```

## 3. Output Format & Verification Contract

- **Full Enrichment Table** (`tables/enrichment_results.csv`):
  - For ORA: `term`, `overlap_count`, `term_size`, `fold_enrichment`, `pvalue`, `padj`, `overlap_genes`.
  - For GSEA: `term`, `es`, `nes`, `pvalue`, `padj` (`FDR q-val`), `lead_genes`.
- **Significant Pathways** (`tables/enrichment_significant.csv`):
  - Subset with `padj < 0.05` sorted by `padj` ascending.
- **Verification Invariants**:
  - `0 <= pvalue <= 1` and `0 <= padj <= 1` for all tested terms.
  - Overlap counts never exceed `len(query_genes)` or `term_size`.

## 4. Gotchas & Troubleshooting

1. **Always Supply the Expressed Transcriptome as the ORA Background**:
   - Using the entire human genome (~20,000 genes) instead of the ~12,000 genes actually expressed in your tissue artificially inflates significance for tissue-specific pathways. Pass `background=de_df["gene"].unique().tolist()` to `gseapy.enrich` or `hypergeometric_ora`.
2. **Avoid Ranking GSEA Strictly by `log2FoldChange` Without Shrinkage**:
   - Unshrunken `log2FoldChange` from `DESeq2` places lowly expressed, high-variance genes at the extreme top and bottom of the ranked list. Either use `apeglm`/`ashr` shrunken LFCs or rank by the signed Wald statistic (`stat` column or $\text{sign}(\text{LFC}) \times -\log_{10}(p)$).
3. **Duplicate Gene Symbols in Ranked Lists**:
   - After Ensembl-to-symbol conversion, multiple transcripts may share a gene symbol. Deduplicate by keeping the entry with the largest $|\text{stat}|$ before calling `gseapy.prerank` (`df.sort_values("pvalue").drop_duplicates("gene")`).
4. **Separate Up- and Down-Regulated ORA vs. Combined ORA**:
   - Running ORA separately on up-regulated (`LFC > 1`) and down-regulated (`LFC < -1`) DEGs often reveals directional pathway activation that is diluted when both lists are pooled.

## Dependencies

`numpy`, `pandas`, `scipy`, `matplotlib`, `gseapy`
