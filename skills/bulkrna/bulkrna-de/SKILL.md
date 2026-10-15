---
name: bulkrna-de
description: Load when comparing gene expression between two conditions in bulk RNA-seq count data. Skip
  when the data is single-cell (use sc-de); spatial (use spatial-de); you need exon-level alternative
  splicing (use bulkrna-splicing).
trigger: differential expression, DE analysis, DESeq2, volcano plot, fold change, DEGs, bulk DE
tags:
- bulkrna
- differential-expression
- DESeq2
- volcano
- MA-plot
- fold-change
---

# Bulk RNA-seq Differential Expression (`bulkrna-de`)

## 1. Method Decision Matrix & When to Use

Select the statistical test according to the input matrix type and sample size per condition:

| Input Matrix & Replicate Structure | Method (`--method` / `method=`) | Upstream Package | Why |
|---|---|---|---|
| **Raw integer gene counts**, $\ge 2$ biological replicates per condition | `method="deseq2"` (`--method deseq2`) | `pydeseq2` (`DeseqDataSet` + `DeseqStats`) or R `DESeq2` | Median-of-ratios size factor normalization, empirical Bayes dispersion shrinkage, and Negative Binomial Wald test |
| **Pre-normalized expression** (TPM / FPKM / log2-CPM) or exploratory screen | `method="ttest"` (`--method ttest`) | `scipy.stats.ttest_ind(..., equal_var=False)` + BH-FDR | Welch's $t$-test on $\log_2(	ext{expr} + 1)$ when raw integer read counts are unavailable |
| **Paired / multi-factor cohort design** (e.g., `~ patient + condition`) | Multi-factor NB-GLM | `pydeseq2` (`design_factors=["patient", "condition"]`) | Controls for baseline inter-individual variation before testing treatment effect |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `pydeseq2` on Raw Integer Counts (`method="deseq2"`)

```python
import numpy as np
import pandas as pd
from pydeseq2.dds import DeseqDataSet
from pydeseq2.ds import DeseqStats

# 1. Load raw count matrix (genes x samples in CSV -> transpose to samples x genes for PyDESeq2)
raw_counts = pd.read_csv("data/counts.csv", index_col=0)
ctrl_cols = [c for c in raw_counts.columns if c.startswith("ctrl")]
treat_cols = [c for c in raw_counts.columns if c.startswith("treat")]
sample_cols = ctrl_cols + treat_cols

# Filter lowly expressed genes (>= 10 total reads across selected samples)
filtered = raw_counts[sample_cols]
filtered = filtered.loc[filtered.sum(axis=1) >= 10].round().astype(int)

# 2. Build sample metadata DataFrame (indexed by sample ID)
metadata = pd.DataFrame(
    {"condition": ["ctrl"] * len(ctrl_cols) + ["treat"] * len(treat_cols)},
    index=sample_cols,
)

# 3. Fit Negative Binomial GLM and run Wald test (treat vs. ctrl)
dds = DeseqDataSet(counts=filtered.T, metadata=metadata, design_factors="condition", quiet=True)
dds.deseq2()
stat_res = DeseqStats(dds, contrast=["condition", "treat", "ctrl"], quiet=True)
stat_res.summary()

de_results = stat_res.results_df.sort_values("padj")
de_sig = de_results[(de_results["padj"] < 0.05) & (de_results["log2FoldChange"].abs() >= 1.0)]
```

### Pattern B: Welch Two-Group Test with BH-FDR (`method="ttest"`)

```python
# Calling use_skill("bulkrna-de") auto-injects kernel.py helpers into the python kernel:
de_results = differential_expression(raw_counts, control_cols=ctrl_cols, treat_cols=treat_cols, min_count=10)
summary = de_summary(de_results, padj_cutoff=0.05, lfc_cutoff=1.0)
```

## 3. Output Format & Verification Contract

- **Full DE Results** (`tables/de_results.csv`):
  - Indexed by `gene`, containing `baseMean` (when `pydeseq2`), `log2FoldChange`, `pvalue`, and `padj` (Benjamini-Hochberg FDR).
- **Significant DEGs** (`tables/de_significant.csv`):
  - Subset passing `padj < 0.05` and `|log2FoldChange| >= 1.0`.
- **Diagnostic Figures**:
  - Volcano plot (`-log10(padj)` vs. `log2FoldChange`) and MA plot (`log2FoldChange` vs. `log10(baseMean + 1)`).

## 4. Gotchas & Troubleshooting

1. **Matrix Orientation in `pydeseq2` (`samples × genes`) vs. R `DESeq2` (`genes × samples`)**:
   - Standard bioinformatics CSV files store **genes as rows and samples as columns**, whereas `pydeseq2.dds.DeseqDataSet` expects **samples as rows and genes as columns** (`counts=filtered.T`). Always check `counts.index.equals(metadata.index)` before fitting `DeseqDataSet`.
2. **Independent Filtering `NaN` in `padj`**:
   - Both `DESeq2` and `pydeseq2` set `padj = NaN` for low-mean or Cook's distance outlier genes. Filter or fill `NaN` values (`de_results["padj"].notna()`) when counting significant genes or plotting volcano plots (`-np.log10(de_results["padj"].clip(lower=1e-300))`).
3. **Never Feed TPM/FPKM to `DeseqDataSet`**:
   - `DESeq2` / `pydeseq2` models raw UMI/fragment counts with a Negative Binomial distribution and estimates library size factors internally. If only pre-normalized TPM/FPKM values exist, use log-scale linear modeling (`limma-trend` / Welch test) rather than rounding TPMs to integers.

## Dependencies

`numpy`, `pandas`, `scipy`, `matplotlib`, `DESeq2`
