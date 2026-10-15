---
name: bulkrna-geneid-mapping
description: Load when converting Ensembl, Entrez or symbol IDs in a bulk RNA count matrix using an explicit mapping or a small human demo reference. Skip when IDs already match downstream needs; use fetch_mapping explicitly for MyGene lookup.
trigger: gene ID, Ensembl, Entrez, gene symbol, ID mapping, gene annotation, convert IDs
tags:
- bulkrna
- gene-id
- mapping
- Ensembl
- Entrez
- HGNC
- annotation
---

# Bulk RNA-seq Gene Identifier Mapping (`bulkrna-geneid-mapping`)

## 1. Method Decision Matrix & When to Use

Select the identifier conversion approach based on network availability, species, and release reproducibility requirements:

| Mapping Scenario | Method | Upstream Package / Source | Why |
|---|---|---|---|
| **Exact genome-build reproducibility** (matching the GTF used during STAR/Salmon quantification) | Local GTF/GFF3 `gene_id` $\leftrightarrow$ `gene_name` extraction | `pandas` / GTF parser | Guarantees 100% concordance with the exact Ensembl/GENCODE release used to build the count matrix |
| **Cross-namespace conversion** (`ensembl.gene` $\leftrightarrow$ `symbol` $\leftrightarrow$ `entrezgene`) across species | Batch REST query via `MyGene.info` | `mygene.MyGeneInfo().querymany` | Maps Ensembl, NCBI Entrez, HGNC, and MGI identifiers across >20,000 species in a single vectorized call |
| **Offline / air-gapped pipelines** with pre-exported mapping tables | Deterministic table join + duplicate aggregation (`sum` / `max` / `first`) | `bulkrna-geneid-mapping` kernel (`strip_ensembl_version` + `map_and_aggregate_counts`) | Zero network dependency; explicitly handles many-to-one Ensembl-to-symbol collisions |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `mygene` Batch Lookup + Count Matrix Aggregation

```python
import pandas as pd
import mygene

# 1. Load raw count matrix indexed by versioned Ensembl IDs (e.g., ENSG00000141510.17)
counts = pd.read_csv("data/counts.csv", index_col=0)
clean_ids = counts.index.astype(str).str.replace(r"\.\d+$", "", regex=True)
counts.index = clean_ids

# 2. Query MyGene.info in batch mode
mg = mygene.MyGeneInfo()
hits = mg.querymany(
    clean_ids.unique().tolist(),
    scopes="ensembl.gene",
    fields="symbol,entrezgene",
    species="human",
    as_dataframe=True,
    returnall=False,
)
symbol_map = hits["symbol"].dropna().loc[~hits["symbol"].dropna().index.duplicated(keep="first")].to_dict()

# 3. Map row index and aggregate duplicate gene symbols by summing raw integer counts
mapped = counts.copy()
mapped.index = [symbol_map.get(g, g) for g in mapped.index]
mapped = mapped.groupby(level=0).sum()
mapped.to_csv("tables/mapped_counts.csv")
```

### Pattern B: Offline Stripping & Collision Aggregation via `use_skill("bulkrna-geneid-mapping")`

```python
# Calling use_skill("bulkrna-geneid-mapping") auto-injects kernel.py helpers into the python kernel:
mapped_counts, report_df = map_and_aggregate_counts(
    counts,
    mapping=symbol_map,
    on_duplicate="sum",
)
```

## 3. Output Format & Verification Contract

- **Mapped Count Matrix** (`tables/mapped_counts.csv`):
  - Gene-by-sample matrix with unique target identifiers (`mapped_counts.index.is_unique == True`).
- **Identifier Mapping Audit Table** (`tables/mapping_table.csv`):
  - Columns: `original_id`, `stripped_id`, `mapped_id`, `was_mapped` (boolean).
- **Unmapped Identifiers** (`tables/unmapped_genes.csv`, when unmapped rows exist):
  - List of source identifiers that had no target match in the reference.
- **Verification Invariants**:
  - When `on_duplicate="sum"` and unmapped IDs are retained, total library sizes per sample are strictly conserved (`mapped_counts.sum(axis=0).equals(counts.sum(axis=0))`).

## 4. Gotchas & Troubleshooting

1. **Always Strip Ensembl Version Suffixes (`.15`, `_PAR_Y`) Before Lookup**:
   - Databases like `MyGene.info`, `org.Hs.eg.db`, and MSigDB index stable Ensembl IDs (`ENSG00000141510`), whereas GENCODE/Ensembl GTFs append transcript/gene version numbers (`ENSG00000141510.17`). Failing to strip `r"\.\d+$"` causes near-0% mapping rates.
2. **Many-to-One Collisions (`ENSG` $\rightarrow$ HGNC Symbol)**:
   - Multiple Ensembl gene records (e.g., pseudoautosomal regions or readthrough/paralog patches) frequently map to a single gene symbol. Choose `on_duplicate="sum"` for raw read counts, or `on_duplicate="max"` / highest mean expression for pre-normalized log-expression matrices.
3. **Excel Date Corruption of Gene Symbols**:
   - Legacy gene symbols such as `SEPT2` (`SEPTIN2`), `MARCH1` (`MARCHF1`), and `DEC1` (`BHLHE40`) are silently converted into dates (`2-Sep`, `1-Mar`) if opened and saved in spreadsheet software. Always inspect mapped symbols for date-like regexes (`r"^\d{1,2}-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$"`).

## Dependencies

`numpy`, `pandas`, `mygene`, `matplotlib`
