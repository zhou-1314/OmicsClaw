---
name: bulkrna-de
description: >-
  Bulk RNA-seq differential expression analysis using PyDESeq2 with optional edgeR/limma-voom via rpy2.
version: 0.1.0
author: OmicsClaw
license: MIT
tags: [bulkrna, differential-expression, DESeq2, PyDESeq2, edgeR, limma]
metadata:
  omicsclaw:
    domain: bulkrna
    emoji: "🔬"
    trigger_keywords: [bulk differential expression, bulk de, deseq2, bulk rna de, differentially expressed genes]
---

# Bulk RNA-seq Differential Expression

Differential expression analysis for bulk RNA-seq count data. Primary engine is PyDESeq2 (a pure-Python re-implementation of DESeq2); falls back to a scipy Welch's t-test with Benjamini-Hochberg FDR correction when PyDESeq2 is not installed.

## CLI Reference

```bash
python omicsclaw.py run bulkrna-de --demo
python omicsclaw.py run bulkrna-de --input <counts.csv> --output <dir>
python bulkrna_de.py --input counts.csv --output results/ --control-prefix ctrl --treat-prefix treat
python bulkrna_de.py --demo --output /tmp/bulkrna_de_demo
python bulkrna_de.py --input counts.csv --output results/ --method ttest --padj-cutoff 0.01 --lfc-cutoff 1.5
```

## Why This Exists

- **Without it**: Researchers must manually install and configure DESeq2 in R, handle count normalization, dispersion estimation, and multiple-testing correction across thousands of genes.
- **With it**: A single Python command runs the full DESeq2 pipeline (via PyDESeq2), produces publication-ready volcano and MA plots, and exports filtered DE tables ready for downstream enrichment.
- **Why OmicsClaw**: Wraps the gold-standard negative-binomial GLM approach into the OmicsClaw reporting framework with automatic fallback to simpler statistics when dependencies are unavailable.

## Workflow

1. **Load**: Read a genes-by-samples raw count matrix (CSV with a `gene` column and sample columns prefixed by condition).
2. **Partition**: Split sample columns into control and treatment groups by prefix matching.
3. **Model**: Fit a negative-binomial GLM per gene using PyDESeq2 (size-factor normalization, dispersion shrinkage, Wald test). Falls back to Welch's t-test with manual Benjamini-Hochberg FDR if PyDESeq2 is not available.
4. **Filter**: Identify significant genes at the user-specified padj and log2FC cutoffs.
5. **Visualize**: Generate volcano plot, MA plot, and DE summary bar chart.
6. **Report**: Write markdown report, result.json, full and filtered DE tables, and a reproducibility script.

## Example Queries

- "Run differential expression on my bulk RNA-seq counts"
- "Find DEGs between control and treatment using DESeq2"
- "Perform bulk RNA DE analysis with a log2FC cutoff of 2"
- "Run bulk DE with t-test fallback on this count matrix"

## Output Structure

```
output_directory/
├── report.md
├── result.json
├── figures/
│   ├── volcano_plot.png
│   ├── ma_plot.png
│   └── de_barplot.png
├── tables/
│   ├── de_results.csv
│   └── de_significant.csv
└── reproducibility/
    └── commands.sh
```

## Safety

- **Local-first**: All processing runs locally; no data is uploaded to external services.
- **Disclaimer**: Every report includes the standard OmicsClaw disclaimer.
- **Audit trail**: Parameters, method used (including fallback events), and input checksums are recorded in result.json.

## Integration with Orchestrator

**Trigger conditions**:
- Automatically invoked when user intent matches bulk RNA differential expression keywords.

**Chaining partners**:
- `bulkrna-alignment` — Upstream: aligned BAM to count matrix
- `bulkrna-enrichment` — Downstream: pathway/GO enrichment of significant genes

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--method` | `pydeseq2` | `pydeseq2` or `ttest` |
| `--control-prefix` | `ctrl` | Column name prefix for control samples |
| `--treat-prefix` | `treat` | Column name prefix for treatment samples |
| `--padj-cutoff` | `0.05` | Adjusted p-value significance threshold |
| `--lfc-cutoff` | `1.0` | Absolute log2 fold-change threshold |

## Version Compatibility

Reference examples tested with: PyDESeq2 0.4+, scipy 1.11+, pandas 2.0+, numpy 1.24+

## Dependencies

**Required**: numpy, pandas, scipy, matplotlib
**Optional**: pydeseq2 (recommended; pure-Python DESeq2 implementation)

## Citations

- [DESeq2](https://doi.org/10.1186/s13059-014-0550-8) — Love et al., Genome Biology 2014
- [PyDESeq2](https://doi.org/10.1093/bioinformatics/btad547) — Muzellec et al., Bioinformatics 2023
- [Benjamini-Hochberg](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x) — Benjamini & Hochberg, JRSSB 1995

## Related Skills

- `bulkrna-alignment` — Read alignment and counting upstream
- `bulkrna-enrichment` — Pathway enrichment of DE genes downstream
- `bulkrna-coexpression` — Co-expression network analysis
