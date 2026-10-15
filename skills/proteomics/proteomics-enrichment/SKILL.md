---
name: proteomics-enrichment
description: Load for Fisher over-representation analysis of protein identifiers against caller-supplied pathways. Skip rank-based GSEA (use bulkrna-enrichment) or protein differential testing (use proteomics-de).
trigger: proteomics enrichment, pathway analysis, ORA
tags:
- proteomics
- enrichment
---

# Proteomics Pathway & GO Over-Representation Analysis (`proteomics-enrichment`)

## 1. Method Decision Matrix & When to Use

Select the functional enrichment framework according to whether a hard DE cutoff or a full ranked protein list is tested:

| Input Protein List | Recommended Method | Upstream Package | Why |
|---|---|---|---|
| **Significant DE protein set** (`fdr < 0.05`, `|log2fc| >= 1.0`) + **measured MS background** | **Over-Representation Analysis (ORA)** via one-sided Fisher's exact / hypergeometric test | `gseapy.enrichr` / `scipy.stats.fisher_exact` + BH-FDR | Tests whether regulated proteins are enriched in GO/Reactome/KEGG pathways relative to the **detected MS proteome** |
| **Full proteome ranked by `log2fc` or signed `-log10(p)`** | **Pre-ranked GSEA** (`gseapy.prerank`) | `gseapy` (`prerank`) / `fgsea` | Detects coordinated subtle shifts across whole protein complexes (e.g., 26S proteasome, OXPHOS) without arbitrary cutoffs |
| **Phosphosite / PTM-specific motif & kinase enrichment** | **PTM-SEA / Kinase-Substrate Enrichment (KSEA)** | `ssGSEA2` / `PhosR` / `KSEAapp` | Maps flanking $\pm 7$ aa sequence windows or site IDs to PhosphoSitePlus kinase-substrate regulons |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Measured-Proteome Background ORA with `scipy.stats.fisher_exact` / `gseapy`

```python
import pandas as pd
from scipy import stats

# 1. Define query DE genes AND the custom background of all proteins actually quantified by MS
de_genes = set(sig_df["gene_name"].dropna().str.upper())
ms_background = set(all_quantified_df["gene_name"].dropna().str.upper())

# 2. Run one-sided Fisher's exact test against pathway gene sets restricted to ms_background
rows = []
for pathway, members in pathway_db.items():
    pw_set = {g.upper() for g in members} & ms_background
    hits = sorted(de_genes & pw_set)
    if not hits:
        continue
    a = len(hits)
    b = len(de_genes - pw_set)
    c = len(pw_set - de_genes)
    d = max(len(ms_background) - a - b - c, 0)
    odds, pval = stats.fisher_exact([[a, b], [c, d]], alternative="greater")
    rows.append({"pathway": pathway, "overlap": a, "pathway_size": len(pw_set), "odds_ratio": odds, "pvalue": pval})
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-enrichment")`)

```python
# Calling use_skill("proteomics-enrichment") auto-injects kernel.py helpers into the python kernel:
res_df = enrich(genes=["TP53", "BRCA1", "ATM"], pathway_db=pathway_dict, background_size=4500)
summary = enrichment_summary(res_df, alpha=0.05)
```

## 3. Output Format & Verification Contract

- **Enrichment Results Table** (`tables/enrichment_results.csv`):
  - Contains `pathway`, `overlap` (hit count $k$), `pathway_size` ($M$), `odds_ratio`, `pvalue` (one-sided Fisher/hypergeometric), `fdr` (BH-adjusted), and `matched_genes` (`;`-joined).
- **Verification Invariants**:
  - `0 <= overlap <= pathway_size <= background_size`.
  - `0 <= pvalue <= fdr <= 1.0`, sorted ascending by `fdr` then `pvalue`.

## 4. Gotchas & Troubleshooting

1. **Whole-Genome Background Bias in Mass Spectrometry ORA**:
   - Standard LC-MS/MS detects 3,000–9,000 high- and medium-abundance proteins (heavily enriched for ribosomes, mitochondria, metabolic enzymes, and cytoskeleton). Testing DE proteins against all ~20,000 human protein-coding genes falsely flags "ribosome" and "metabolic process" as enriched even on random draws. **Always set the background to the set of proteins actually quantified in the MS experiment**.
2. **UniProt Accession vs. HGNC Gene Symbol Mismatch**:
   - Pathway libraries (`MSigDB`, `Reactome`, `GO`) are typically indexed by official HGNC gene symbols (`MAPK1`) or Entrez IDs, while MaxQuant/DIA-NN outputs UniProt accessions (`P28482`) or isoform-suffixed IDs (`P28482-2`). Strip isoform suffixes (`-2`) and map to canonical gene symbols before running ORA.
3. **Redundant Protein Group Isoforms Double-Count Pathway Hits**:
   - Deduplicate `genes` (`set(genes)`) after mapping protein groups to gene symbols so two splice isoforms of the same gene do not count as two independent hits in a pathway.

## Dependencies

`numpy`, `pandas`, `matplotlib`, `scipy`
