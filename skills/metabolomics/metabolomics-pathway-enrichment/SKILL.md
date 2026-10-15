---
name: metabolomics-pathway-enrichment
description: Load when running metabolite-name ORA against an explicit local pathway reference with
  BH-FDR; bundled pathway sets are for explicit demonstrations only. Skip m/z annotation (use
  metabolomics-annotation) and topology analysis or online pathway retrieval (use external
  mummichog / FELLA).
trigger: metabolomics pathway, KEGG, MetaboAnalyst, enrichment, mummichog
tags:
- metabolomics
- pathway
- enrichment
- ora
- fisher
- demo
---

# Metabolomics Pathway Over-Representation Analysis (`metabolomics-pathway-enrichment`)

## 1. Method Decision Matrix & When to Use

Select the metabolomics pathway enrichment algorithm according to identification confidence and input data type:

| Input Data Type | Recommended Algorithm | Upstream Package / Database | Why |
|---|---|---|---|
| **Confirmed / Annotated DE Metabolite List** (HMDB / KEGG / ChEBI IDs) | **Hypergeometric / Fisher Exact ORA** (`method="ora"`) | `scipy.stats.hypergeom.sf` / `MetaboAnalystR` / `RaMP-DB` | Tests whether significant metabolites are over-represented in KEGG/SMPDB/Reactome pathways relative to the **assay-specific metabolome background** |
| **Continuous Metabolite Matrix** (no hard DE cutoff, sample $\times$ metabolite) | **Quantitative Enrichment Analysis (`GlobalTest` / `QEA`)** | `MetaboAnalystR` (`globaltest`) / `sspa` (Python) | Evaluates pathway-level covariance directly across sample groups |
| **Unannotated LC-MS $m/z$ Peak Table** (`mz`, `rt`, `pvalue`, `t_stat`) | **Mummichog** / **GSEA-MS1** | `mummichog` (`MetaboAnalystR` `PerformPSEA`) | Infers pathway activity directly from putative $m/z$ empirical compounds via network permutation without requiring prior MS2 confirmation |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Hypergeometric Over-Representation Analysis (`scipy.stats.hypergeom.sf`) + BH-FDR

```python
import numpy as np
import pandas as pd
from scipy import stats

# 1. Define query significant metabolites and reference pathway dictionary
# pathways = {"TCA Cycle": {"kegg_id": "map00020", "metabolites": ["Citrate", "Succinate", "Malate", "Fumarate", ...]}}
universe = {m.lower() for pw in pathways.values() for m in pw["metabolites"]}
query = {m.lower() for m in sig_metabolites} & universe
M = len(universe)  # Total background metabolites in reference library
N = len(query)     # Total query hits in universe

# 2. Compute hypergeometric p-value: P(X >= k) = hypergeom.sf(k - 1, M, n, N)
for pw_name, info in pathways.items():
    pw_members = {m.lower() for m in info["metabolites"]}
    k = len(query & pw_members)
    n = len(pw_members)
    pval = stats.hypergeom.sf(k - 1, M, n, N) if k > 0 else 1.0
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-pathway-enrichment")`)

```python
# Calling use_skill("metabolomics-pathway-enrichment") auto-injects kernel.py helpers into the python kernel:
res_df = enrich(sig_metabolites, pathways=custom_pathways, method="ora")
summary = enrichment_summary(res_df, alpha=0.05)
```

## 3. Output Format & Verification Contract

- **Pathway Enrichment Table** (`tables/pathway_enrichment.csv`):
  - Contains `pathway`, `kegg_id`, `hits` ($k$), `pathway_size` ($n$), `expected`, `fold_enrichment`, `pvalue` (hypergeometric survival function `sf(k - 1, M, n, N)`), `fdr` (BH-adjusted across pathways with $\ge 1$ hit), and `matched_metabolites`.
- **Verification Invariants**:
  - `pathways` dictionary must be explicitly provided; missing reference databases raise `ValueError`.
  - Matching is case-insensitive exact metabolite identifier/name equality, deduplicating repeated query entries.

## 4. Gotchas & Troubleshooting

1. **Synonym & Identifier Fragmentation (`L-Lactate` vs. `Lactic acid` vs. `HMDB0000190` vs. `C00186`)**:
   - Chemical name string matching frequently misses 30–60% of true overlaps because search engines emit `(+/-)-Lactic acid` while KEGG lists `L-Lactate` (`C00186`). Always standardize identifiers to **KEGG Compound IDs (`Cxxxxx`)**, **HMDB 7-digit IDs (`HMDBxxxxxxx`)**, or **InChIKey first-block connectivity hashes** (`inchikey.split("-")[0]`) before running ORA.
2. **Assay Coverage Bias (RP-LC-MS vs. HILIC vs. GC-MS)**:
   - Reversed-phase C18 LC-MS primarily captures semi-polar and non-polar lipids/bile acids, whereas HILIC captures polar central carbon metabolites (TCA cycle, glycolysis, amino acids). Using the entire KEGG global metabolic network (including plant/microbial secondary metabolites `map01110`) as the background $M$ severely inflates pathway significance. Restrict `pathways` to organism-specific (`hsa` / `mmu`) and platform-detectable pathways.
3. **Never Use Tiny Demo Pathway Dictionaries as Biological Evidence**:
   - Illustrative 9-pathway demo sets are strictly for unit testing; always pass a curated KEGG/SMPDB/RaMP-DB pathway mapping in production analyses.

## Dependencies

`numpy`, `pandas`, `scipy`, `matplotlib`
