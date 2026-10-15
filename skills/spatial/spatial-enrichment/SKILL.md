---
name: spatial-enrichment
description: Load when running pathway or gene-set enrichment per cluster on spatial AnnData with over-representation, preranked GSEA, or ssGSEA group-mean scores. Skip when ranking spatially variable genes (use spatial-genes) or comparing conditions (use spatial-condition).
trigger: pathway enrichment, gene set enrichment, enrichr, GSEA, ssGSEA, GO, Reactome, MSigDB
tags:
- spatial
- enrichment
- pathway
- gsea
- ssgsea
- enrichr
- gene-set
---

# Spatial Domain & Niche Pathway Enrichment (`spatial-enrichment`)

## 1. Method Decision Matrix & When to Use

Use this playbook to functionally characterize spatial domains, niches, or cell types using pathway over-representation analysis (ORA), preranked GSEA, or spatially resolved gene-set activity scoring:

| Method (`method=`) | Upstream Package | Input Signal | Output Unit | Best Used When |
|---|---|---|---|---|
| **Over-Representation Analysis** (`enrichr`) | `gseapy.enrichr` | Top significant marker genes per domain (`pvals_adj < 0.05`, `logfoldchanges > 0.25`) | Calibrated Fisher/Hypergeometric `Adjusted P-value` + `Combined Score` | Fast functional annotation of discrete spatial domain marker lists |
| **Preranked GSEA** (`gsea`) | `gseapy.prerank` | Full genome-wide ranked gene vector (`scores` or `logfoldchanges`) per domain | Normalized Enrichment Score (`NES`) + permutation `FDR q-val` | Detecting coordinated subtle shifts across full pathway gene sets without arbitrary DE cutoffs |
| **Spatial Pathway Activity Scoring** (`score_genes` / `ssgsea`) | `scanpy.tl.score_genes` or `gseapy.ssgsea` | Log-normalized `adata.X` + pathway dictionary | Continuous per-spot or per-domain activity score (not a $p$-value) | Mapping continuous pathway activity (e.g., Hypoxia, EMT, IFN-$\gamma$) directly onto tissue coordinates |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Domain Marker Over-Representation Analysis (`scanpy` + `gseapy.enrichr`)

```python
import pandas as pd
import scanpy as sc
import gseapy as gp

# 1. Rank marker genes per spatial domain
sc.tl.rank_genes_groups(adata, groupby="spatial_domain", method="wilcoxon", use_raw=False)
de_df = sc.get.rank_genes_groups_df(adata, group=None)

# 2. Run ORA per domain using custom panel background
bg_genes = adata.var_names.tolist()
ora_tables = []
for domain, sub in de_df.groupby("group", observed=True):
    markers = sub[(sub["pvals_adj"] < 0.05) & (sub["logfoldchanges"] > 0.25)]["names"].tolist()
    if len(markers) < 5:
        continue
    enr = gp.enrichr(
        gene_list=markers,
        gene_sets="MSigDB_Hallmark_2020",
        background=bg_genes,
        outdir=None,
    )
    res = enr.results.copy()
    res["group"] = str(domain)
    ora_tables.append(res)

enrichment_df = pd.concat(ora_tables, ignore_index=True) if ora_tables else pd.DataFrame()
```

### Pattern B: Preranked GSEA on Domain Rank Statistics (`gseapy.prerank`)

```python
import gseapy as gp

# Extract full ranked gene series for a specific domain (e.g., Tumor Core vs Rest)
dom_df = sc.get.rank_genes_groups_df(adata, group="0").dropna(subset=["names", "scores"])
rnk = dom_df.set_index("names")["scores"].sort_values(ascending=False)

pre_res = gp.prerank(
    rnk=rnk,
    gene_sets="MSigDB_Hallmark_2020",
    min_size=10,
    max_size=500,
    permutation_num=100,
    outdir=None,
    seed=123,
)
gsea_df = pre_res.res2d.copy()
```

### Pattern C: Continuous Spatial Pathway Activity Maps (`scanpy.tl.score_genes`)

```python
import scanpy as sc

hallmark_sets = {
    "Hypoxia": ["VEGFA", "SLC2A1", "PGK1", "LDHA", "ENO1", "CA9", "ADM", "NDRG1"],
    "EMT": ["VIM", "FN1", "SNAI1", "SNAI2", "TWIST1", "ZEB1", "COL1A1", "MMP2"],
}
for name, genes in hallmark_sets.items():
    valid = [g for g in genes if g in adata.var_names]
    if len(valid) >= 3:
        sc.tl.score_genes(adata, gene_list=valid, score_name=f"pathway_{name}", use_raw=False)
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("spatial-enrichment")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `score_spatial_pathways(adata, gene_sets, groupby="leiden")`
- `ora_enrichr(marker_genes, gene_sets="MSigDB_Hallmark_2020", background=None, fdr=0.05)`

## 3. Output Format & Verification Contract

- **Enrichment Results Table** (`tables/enrichment_results.csv`):
  - Columns for ORA: `group`, `Term`, `Overlap`, `P-value`, `Adjusted P-value`, `Combined Score`, `Genes`.
  - Columns for Preranked GSEA: `group`, `Term`, `ES`, `NES`, `NOM p-val`, `FDR q-val`, `Lead_genes`.
- **Verification**:
  - Verify that `Adjusted P-value` / `FDR q-val` values are finite in $[0, 1]$ and that continuous `score_genes` / `ssgsea` scores are reported as activity scores rather than $p$-values.

## 4. Gotchas & Troubleshooting

1. **Targeted Spatial Panels Require Explicit `background` in ORA**:
   - Targeted panels (Xenium, CosMx, MERFISH) are pre-enriched for oncology or immunology genes. Testing 30 domain markers against a $20,000$-gene genome background inflates every immune/cancer pathway. Always pass `background=adata.var_names.tolist()` to `gp.enrichr`.
2. **Offline / Air-Gapped Compute Environments**:
   - Passing a string library name like `gene_sets="MSigDB_Hallmark_2020"` queries the remote Enrichr REST API. In environments without internet access, pass a local Python `dict[str, list[str]]` or path to a `.gmt` file to `gene_sets`.
3. **Human vs. Mouse Gene Symbol Casing**:
   - MSigDB Hallmark and GO gene sets use uppercase human symbols (`VEGFA`). For mouse spatial data (`Vegfa`), convert `rnk.index` / `marker_genes` to uppercase or use mouse GMT collections before running `gseapy`.

## Dependencies

`anndata`, `gseapy`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
